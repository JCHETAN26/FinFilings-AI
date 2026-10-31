package ingest

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// All servers here are local httptest fakes; no request leaves the machine.
const testUserAgent = "FinFilingsResearch admin@finfilings.ai"

// recordingTransport records the wall-clock start of every attempt.
type recordingTransport struct {
	mu     sync.Mutex
	starts []time.Time
}

func (r *recordingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	r.mu.Lock()
	r.starts = append(r.starts, time.Now())
	r.mu.Unlock()
	return http.DefaultTransport.RoundTrip(req)
}

func (r *recordingTransport) sorted() []time.Time {
	r.mu.Lock()
	defer r.mu.Unlock()
	s := slices.Clone(r.starts)
	slices.SortFunc(s, func(a, b time.Time) int { return a.Compare(b) })
	return s
}

// maxInWindow returns the largest number of starts within any window.
func maxInWindow(starts []time.Time, window time.Duration) int {
	best := 0
	for i := range starts {
		j := i
		for j < len(starts) && starts[j].Sub(starts[i]) < window {
			j++
		}
		best = max(best, j-i)
	}
	return best
}

func testClient(t *testing.T, srv *httptest.Server, opts ClientOptions) (*Client, *recordingTransport) {
	t.Helper()
	rt := &recordingTransport{}
	if opts.UserAgent == "" {
		opts.UserAgent = testUserAgent
	}
	opts.DataBaseURL, opts.ArchivesBaseURL, opts.Transport = srv.URL, srv.URL, rt
	c, err := NewClient(opts)
	if err != nil {
		t.Fatal(err)
	}
	return c, rt
}

func TestClientOptionValidation(t *testing.T) {
	for _, ua := range []string{"", "FinFilings", "admin@finfilings.ai", "Fin Filings no-email", "Bad\tAgent a@b.co", "FinFilings a@b"} {
		if _, err := NewClient(ClientOptions{UserAgent: ua}); err == nil {
			t.Errorf("user agent %q accepted", ua)
		}
	}
	for _, rate := range []float64{-1, 10.01, 50} {
		if _, err := NewClient(ClientOptions{UserAgent: testUserAgent, RequestsPerSecond: rate}); err == nil {
			t.Errorf("rate %v accepted", rate)
		}
	}
	for _, base := range []string{"ftp://www.sec.gov", "https://user:pw@www.sec.gov", "https://www.sec.gov?x=1", "not a url"} {
		if _, err := NewClient(ClientOptions{UserAgent: testUserAgent, ArchivesBaseURL: base}); err == nil {
			t.Errorf("base %q accepted", base)
		}
	}
	c, err := NewClient(ClientOptions{UserAgent: testUserAgent})
	if err != nil || c.opts.RequestsPerSecond != DefaultRequestsPerSecond || c.data.Host != "data.sec.gov" || c.archives.Host != "www.sec.gov" {
		t.Fatal(err, c)
	}
	u, err := c.DocumentURL(FilingRef{CIK: "320193", Accession: "000032019323000106", PrimaryDocument: "aapl-20230930.htm"})
	if err != nil || u.String() != "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm" {
		t.Fatal(u, err)
	}
	for _, doc := range []string{"../x.htm", "a/b.htm", ""} {
		if _, err := c.DocumentURL(FilingRef{CIK: "320193", Accession: "000032019323000106", PrimaryDocument: doc}); err == nil {
			t.Errorf("document %q accepted", doc)
		}
	}
}

func TestClientRateLimitUnderConcurrency(t *testing.T) {
	var badAgent atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("User-Agent") != testUserAgent {
			badAgent.Add(1)
		}
		fmt.Fprint(w, "ok")
	}))
	defer srv.Close()
	c, rt := testClient(t, srv, ClientOptions{RequestsPerSecond: MaxRequestsPerSecond})
	const n = 14
	var wg sync.WaitGroup
	began := time.Now()
	for i := range n {
		wg.Add(1)
		go func() {
			defer wg.Done()
			var buf bytes.Buffer
			if _, err := c.Download(context.Background(), FilingRef{CIK: "1", Accession: fmt.Sprintf("%018d", i), PrimaryDocument: "d.htm"}, &buf); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	elapsed := time.Since(began)
	starts := rt.sorted()
	if len(starts) != n || badAgent.Load() != 0 {
		t.Fatalf("%d attempts, %d without User-Agent", len(starts), badAgent.Load())
	}
	if w := maxInWindow(starts, time.Second); w > MaxRequestsPerSecond {
		t.Fatalf("%d request starts within one second", w)
	}
	if min := time.Duration(n-1) * 101 * time.Millisecond; elapsed < min {
		t.Fatalf("%d requests finished in %v (< %v)", n, elapsed, min)
	}
	t.Logf("%d concurrent requests at %d rps: elapsed %v, max starts in any 1s window %d", n, MaxRequestsPerSecond, elapsed, maxInWindow(starts, time.Second))
}

func TestClientRetriesAndLimits(t *testing.T) {
	var hits sync.Map
	count := func(path string) int32 {
		v, _ := hits.LoadOrStore(path, new(atomic.Int32))
		return v.(*atomic.Int32).Add(1)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := count(r.URL.Path)
		switch {
		case strings.HasSuffix(r.URL.Path, "/throttled.htm") && n == 1:
			w.Header().Set("Retry-After", "1")
			w.WriteHeader(http.StatusTooManyRequests)
		case strings.HasSuffix(r.URL.Path, "/flaky.htm") && n <= 2:
			w.WriteHeader(http.StatusServiceUnavailable)
		case strings.HasSuffix(r.URL.Path, "/missing.htm"):
			http.NotFound(w, r)
		case strings.HasSuffix(r.URL.Path, "/moved.htm"):
			http.Redirect(w, r, "https://elsewhere.example/x", http.StatusFound)
		case strings.HasSuffix(r.URL.Path, "/huge.htm"):
			w.Write(bytes.Repeat([]byte("x"), 4096))
		case strings.HasSuffix(r.URL.Path, "/slowban.htm"):
			w.Header().Set("Retry-After", "3600")
			w.WriteHeader(http.StatusTooManyRequests)
		default:
			fmt.Fprint(w, "<html>body</html>")
		}
	}))
	defer srv.Close()
	c, rt := testClient(t, srv, ClientOptions{RequestsPerSecond: MaxRequestsPerSecond, MaxDocumentBytes: 1024})
	ref := func(doc string) FilingRef {
		return FilingRef{CIK: "320193", Accession: "000032019323000106", PrimaryDocument: doc}
	}
	ctx := context.Background()
	var buf bytes.Buffer
	before := len(rt.sorted())
	dl, err := c.Download(ctx, ref("throttled.htm"), &buf)
	if err != nil || buf.String() != "<html>body</html>" || dl.Bytes != int64(buf.Len()) || len(dl.SHA256) != 64 {
		t.Fatal(dl, err)
	}
	s := rt.sorted()[before:]
	if len(s) != 2 || s[1].Sub(s[0]) < time.Second {
		t.Fatalf("Retry-After not honored: %v", s)
	}
	if _, err := c.Download(ctx, ref("flaky.htm"), &bytes.Buffer{}); err != nil {
		t.Fatal("bounded 503 retries", err)
	}
	for _, doc := range []string{"missing.htm", "moved.htm", "slowban.htm"} {
		before := len(rt.sorted())
		if _, err := c.Download(ctx, ref(doc), &bytes.Buffer{}); !errors.Is(err, errHTTPStatus) {
			t.Fatalf("%s: %v", doc, err)
		}
		if attempts := len(rt.sorted()) - before; attempts != 1 {
			t.Fatalf("%s retried %d times", doc, attempts)
		}
	}
	if _, err := c.Download(ctx, ref("huge.htm"), &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "exceeds") {
		t.Fatal("size limit", err)
	}
	cancelled, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := c.Download(cancelled, ref("ok.htm"), &bytes.Buffer{}); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestClientRecentFilings(t *testing.T) {
	bodies := map[string]string{
		"/submissions/CIK0000320193.json": `{"cik":"320193","tickers":["AAPL"],"filings":{"recent":{
			"accessionNumber":["0000320193-24-000123","0000320193-24-000081","0000320193-24-000010","0000320193-23-000106"],
			"filingDate":["2024-11-01","2024-08-02","2024-02-02","2023-11-03"],
			"reportDate":["2024-09-28","2024-06-29","2023-12-30","2023-09-30"],
			"form":["10-K","10-Q","8-K","10-K"],
			"primaryDocument":["aapl-20240928.htm","aapl-20240629.htm","ex99.htm","aapl-20230930.htm"]}}}`,
		"/submissions/CIK0000000001.json": `{"cik":"2","tickers":["X"],"filings":{"recent":{"accessionNumber":[],"filingDate":[],"form":[],"primaryDocument":[]}}}`,
		"/submissions/CIK0000000003.json": `{"cik":"3","tickers":["X"],"filings":{"recent":{"accessionNumber":["0000000003-24-000001"],"filingDate":[],"form":["10-K"],"primaryDocument":["a.htm"]}}}`,
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, ok := bodies[r.URL.Path]
		if !ok {
			http.NotFound(w, r)
			return
		}
		fmt.Fprint(w, body)
	}))
	defer srv.Close()
	c, _ := testClient(t, srv, ClientOptions{RequestsPerSecond: MaxRequestsPerSecond})
	refs, err := c.RecentFilings(context.Background(), "0000320193")
	if err != nil {
		t.Fatal(err)
	}
	if len(refs) != 3 || refs[0].Form != model.Form10K || refs[1].Form != model.Form10Q || refs[2].Accession != "000032019323000106" || refs[0].Ticker != "AAPL" || refs[0].CIK != "320193" {
		t.Fatalf("%+v", refs)
	}
	if refs[0].FilingDate.Format(time.DateOnly) != "2024-11-01" || refs[2].PrimaryDocument != "aapl-20230930.htm" {
		t.Fatalf("%+v", refs[0])
	}
	only10K, err := c.RecentFilings(context.Background(), "320193", model.Form10K)
	if err != nil || len(only10K) != 2 {
		t.Fatal(only10K, err)
	}
	for _, cik := range []string{"1", "3", "abc", "12345678901"} {
		if _, err := c.RecentFilings(context.Background(), cik); err == nil {
			t.Errorf("CIK %s accepted", cik)
		}
	}
}
