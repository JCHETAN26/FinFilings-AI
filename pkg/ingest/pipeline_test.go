package ingest

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/hybrid"
	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// fakeFiling is one filing served by fakeEDGAR.
type fakeFiling struct {
	Accession string // dashed, e.g. 0000320193-24-000123
	Date      string
	Form      string
	Document  string
	Body      []byte
}

type fakeCompany struct {
	CIK     string
	Ticker  string
	Filings []fakeFiling // newest first, as EDGAR lists them
}

// fakeEDGAR is a local stand-in for data.sec.gov submissions and
// www.sec.gov Archives. It records every request and rejects any without
// the expected User-Agent.
type fakeEDGAR struct {
	t         *testing.T
	companies []fakeCompany
	mu        sync.Mutex
	paths     []string
	starts    []time.Time
}

func (f *fakeEDGAR) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.paths = append(f.paths, r.URL.Path)
	f.starts = append(f.starts, time.Now())
	f.mu.Unlock()
	if r.Header.Get("User-Agent") != testUserAgent {
		f.t.Errorf("request %s without required User-Agent: %q", r.URL.Path, r.Header.Get("User-Agent"))
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	for _, c := range f.companies {
		if r.URL.Path == fmt.Sprintf("/submissions/CIK%010s.json", c.CIK) {
			var s submissions
			s.CIK, s.Tickers = c.CIK, []string{c.Ticker}
			rec := &s.Filings.Recent
			for _, fl := range c.Filings {
				rec.AccessionNumber = append(rec.AccessionNumber, fl.Accession)
				rec.FilingDate = append(rec.FilingDate, fl.Date)
				rec.ReportDate = append(rec.ReportDate, "")
				rec.Form = append(rec.Form, fl.Form)
				rec.PrimaryDocument = append(rec.PrimaryDocument, fl.Document)
			}
			json.NewEncoder(w).Encode(s)
			return
		}
		for _, fl := range c.Filings {
			if r.URL.Path == fmt.Sprintf("/Archives/edgar/data/%s/%s/%s", c.CIK, strings.ReplaceAll(fl.Accession, "-", ""), fl.Document) {
				w.Write(fl.Body)
				return
			}
		}
	}
	http.NotFound(w, r)
}

func readJSONL[T any](t *testing.T, path string) []T {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var out []T
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64<<10), 1<<20)
	for sc.Scan() {
		var v T
		if err := json.Unmarshal(sc.Bytes(), &v); err != nil {
			t.Fatal(err)
		}
		out = append(out, v)
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}

// singleIndexScores rebuilds the passage store as one unsharded index and
// returns every document's BM25 score for query, as the oracle for the
// router's union-statistics scoring.
func singleIndexScores(t *testing.T, passages []model.SECChunk, query string) map[string]float64 {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "single")
	w, err := index.NewWriter(dir, index.WriterOptions{Workers: 1})
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range passages {
		if err := w.Add(context.Background(), c); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	ix, err := index.Open(dir, index.Options{MaxTopK: 65536})
	if err != nil {
		t.Fatal(err)
	}
	defer ix.Close()
	hits, err := ix.Search(context.Background(), query, len(passages))
	if err != nil {
		t.Fatal(err)
	}
	scores := map[string]float64{}
	for _, h := range hits {
		scores[h.Chunk.ID] = h.Score
	}
	return scores
}

// End-to-end over SYNTHETIC fixture filings served by a local fake EDGAR:
// list -> paced download -> section parse -> quarter shards -> mmap router
// -> sparse and RRF-fused queries. Verifies plumbing and tagging only.
func TestPipelineEndToEndSyntheticEDGAR(t *testing.T) {
	doc := func(company string, spec fixtureSpec) []byte {
		spec.Company = company
		return []byte(fixture10K(spec))
	}
	edgar := &fakeEDGAR{t: t, companies: []fakeCompany{
		{CIK: "320193", Ticker: "AAPL", Filings: []fakeFiling{
			{"0000320193-24-000123", "2024-11-01", "10-K", "aapl-20240928.htm", doc("aapl2024", fixtureSpec{})},
			{"0000320193-24-000081", "2024-08-02", "10-Q", "aapl-20240629.htm", []byte("10-Q must not be fetched")},
			{"0000320193-23-000106", "2023-11-03", "10-K", "aapl-20230930.htm", doc("aapl2023", fixtureSpec{Item8ByReference: true})},
		}},
		{CIK: "789019", Ticker: "MSFT", Filings: []fakeFiling{
			{"0000950170-24-087843", "2024-07-30", "10-K", "msft-20240630.htm", doc("msft2024", fixtureSpec{Words: map[string]int{"7": 900}})},
			{"0000950170-23-035122", "2023-07-27", "10-K", "msft-20230630.htm", doc("msft2023", fixtureSpec{})},
			{"0000950170-22-016066", "2022-07-28", "10-K", "msft-20220630.htm", doc("msft2022", fixtureSpec{})},
		}},
		{CIK: "1000", Ticker: "BADCO", Filings: []fakeFiling{
			{"0000001000-24-000001", "2024-03-01", "10-K", "bad.htm", doc("badco", fixtureSpec{Items: []string{"1", "1A", "8"}})},
		}},
	}}
	srv := httptest.NewServer(edgar)
	defer srv.Close()
	client, rt := testClient(t, srv, ClientOptions{RequestsPerSecond: MaxRequestsPerSecond})
	ciks := []string{"320193", "789019", "1000"}
	opts := PipelineOptions{
		MaxFilingsPerCompany: 2,
		Parser:               ParserOptions{WordsPerChunk: 64},
		Shards:               index.ShardedWriterOptions{Writer: index.WriterOptions{MaxDocsPerSegment: 8}},
	}

	// Fail-fast mode: the unparseable filing aborts and the output vanishes.
	failed := filepath.Join(t.TempDir(), "failed")
	if _, err := RunPipeline(context.Background(), client, ciks, failed, opts); !errors.Is(err, ErrSections) {
		t.Fatal("fail-fast", err)
	}
	if _, err := os.Stat(failed); !os.IsNotExist(err) {
		t.Fatal("failed run left output", err)
	}

	var logs strings.Builder
	opts.SkipUnparseable = true
	opts.Logger = slog.New(slog.NewJSONHandler(&logs, nil))
	out := filepath.Join(t.TempDir(), "edgar")
	report, err := RunPipeline(context.Background(), client, ciks, out, opts)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := RunPipeline(context.Background(), client, ciks, out, opts); err == nil {
		t.Fatal("published output overwritten")
	}

	// Request discipline.
	for _, p := range edgar.paths {
		if strings.Contains(p, "aapl-20240629") || strings.Contains(p, "msft-20220630") {
			t.Fatalf("fetched excluded filing %s", p)
		}
	}
	if n := maxInWindow(rt.sorted(), time.Second); n > MaxRequestsPerSecond {
		t.Fatalf("%d requests in one second", n)
	}

	// Report and on-disk layout.
	wantShards := []string{"2024_Q4", "2023_Q4", "2024_Q3", "2023_Q3"}
	if len(report.Filings) != 4 || len(report.Skipped) != 1 || report.Skipped[0].Ref.Ticker != "BADCO" || !slices.Equal(report.Shards, wantShards) {
		t.Fatalf("report filings=%d skipped=%+v shards=%v", len(report.Filings), report.Skipped, report.Shards)
	}
	for _, name := range append(wantShards, index.ShardManifestName) {
		if _, err := os.Stat(filepath.Join(out, SegmentsDirName, name)); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{".work", "segments/2024_Q1"} {
		if _, err := os.Stat(filepath.Join(out, name)); !os.IsNotExist(err) {
			t.Fatalf("%s should not exist: %v", name, err)
		}
	}
	var onDisk PipelineReport
	data, err := os.ReadFile(filepath.Join(out, ReportName))
	if err != nil || json.Unmarshal(data, &onDisk) != nil || onDisk.Chunks != report.Chunks {
		t.Fatal("report.json", err)
	}
	if len(readJSONL[FilingReport](t, filepath.Join(out, ProvenanceName))) != 4 {
		t.Fatal("provenance records")
	}
	if !strings.Contains(logs.String(), `"msg":"filing_skipped"`) || !strings.Contains(logs.String(), `"msg":"pipeline_complete"`) {
		t.Fatal("structured logs missing")
	}

	// Every stored passage is tagged with exactly one verified section.
	passages := readJSONL[model.SECChunk](t, filepath.Join(out, PassagesName))
	if len(passages) != report.Chunks {
		t.Fatalf("passage store %d, report %d", len(passages), report.Chunks)
	}
	ids := map[string]bool{}
	for _, c := range passages {
		if c.SectionItem != "Item 1A" && c.SectionItem != "Item 7" && c.SectionItem != "Item 8" {
			t.Fatalf("untagged/unsupported section %q in %s", c.SectionItem, c.ID)
		}
		if ids[c.ID] {
			t.Fatal("duplicate passage ID", c.ID)
		}
		ids[c.ID] = true
	}

	// Mmap router over all quarter shards.
	r, err := index.OpenShardRouter(filepath.Join(out, SegmentsDirName), index.ShardRouterOptions{Workers: 3, MaxConcurrentSearches: 4})
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	var keys []string
	for _, s := range r.Shards() {
		keys = append(keys, s.Key.String())
	}
	if !slices.Equal(keys, []string{"2023_Q3", "2023_Q4", "2024_Q3", "2024_Q4"}) {
		t.Fatal("router shard order", keys)
	}
	stats, err := r.Stats()
	if err != nil || stats.Docs != uint64(len(passages)) {
		t.Fatal(stats, err)
	}
	ctx := context.Background()
	for _, company := range []struct {
		prefix, ticker string
		year           int
	}{{"aapl2024", "AAPL", 2024}, {"aapl2023", "AAPL", 2023}, {"msft2024", "MSFT", 2024}, {"msft2023", "MSFT", 2023}} {
		for _, item := range []string{"1A", "7", "8"} {
			marker := fixtureMarker(company.prefix, item)
			want := 0
			for _, c := range passages {
				if strings.Contains(c.Text, marker) {
					want++
				}
			}
			if company.prefix == "aapl2023" && item == "8" {
				if want != 0 {
					t.Fatal("by-reference Item 8 has body markers")
				}
				continue
			}
			hits, err := r.Search(ctx, marker, 100)
			if err != nil {
				t.Fatal(err)
			}
			if want == 0 || len(hits) != want {
				t.Fatalf("%s: %d hits, %d passages contain it", marker, len(hits), want)
			}
			for _, h := range hits {
				if h.Chunk.SectionItem != "Item "+item || h.Chunk.Ticker != company.ticker || h.Chunk.FilingYear != company.year {
					t.Fatalf("%s hit mis-tagged: %+v", marker, h.Chunk)
				}
			}
		}
		// Text outside Items 1A/7/8 and hidden/script content is unsearchable.
		for _, absent := range []string{fixtureMarker(company.prefix, "1"), fixtureMarker(company.prefix, "1b"), fixtureMarker(company.prefix, "7a"), fixtureMarker(company.prefix, "9"), "hiddenxbrlword", "scriptword", "signatureword", "commentword"} {
			if hits, err := r.Search(ctx, absent, 10); err != nil || len(hits) != 0 {
				t.Fatalf("unclassified text %q indexed: %d hits %v", absent, len(hits), err)
			}
		}
	}
	if hits, err := r.Search(ctx, fixtureMarker("badco", "1a"), 10); err != nil || len(hits) != 0 {
		t.Fatal("skipped filing indexed", hits, err)
	}

	// Filtered sparse retrieval across shards, scored with union statistics:
	// every score equals the score from one unsharded index of the store.
	query := "revenue liquidity risk"
	oracle := singleIndexScores(t, passages, query)
	for _, f := range []model.FilingFilter{{}, {Section: "Item 8"}, {Ticker: "MSFT", Year: 2024, Section: "Item 7"}} {
		hits, err := r.SearchFiltered(ctx, query, 50, f)
		if err != nil || len(hits) == 0 {
			t.Fatal(f, err)
		}
		for i, h := range hits {
			if !f.Matches(h.Chunk) {
				t.Fatalf("filter %+v violated by %+v", f, h.Chunk)
			}
			if oracle[h.Chunk.ID] != h.Score {
				t.Fatalf("%s: router score %v, single-index score %v", h.Chunk.ID, h.Score, oracle[h.Chunk.ID])
			}
			if i > 0 && hits[i-1].Score < h.Score {
				t.Fatal("hits not ranked")
			}
		}
	}

	// Hybrid fusion plumbing: RRF (k=60) over two router rankings. The second
	// branch is another BM25 query standing in for the dense branch; no
	// embeddings are involved, so this checks fusion over sharded IDs only.
	sparse, _ := r.SearchFiltered(ctx, "liquidity capital", 50, model.FilingFilter{Section: "Item 7"})
	other, _ := r.SearchFiltered(ctx, "cash flow interest", 50, model.FilingFilter{Section: "Item 7"})
	toHits := func(hs []index.Hit) []hybrid.Hit {
		out := make([]hybrid.Hit, len(hs))
		for i, h := range hs {
			out[i] = hybrid.Hit{ID: h.Chunk.ID, Score: h.Score}
		}
		return out
	}
	fused, err := hybrid.RRF(toHits(sparse), toHits(other), 10)
	if err != nil || len(fused) != 10 {
		t.Fatal(fused, err)
	}
	for _, h := range fused {
		if !ids[h.ID] || !strings.Contains(h.ID, "#item7#") {
			t.Fatalf("fused hit %s not an Item 7 passage", h.ID)
		}
	}
	t.Logf("synthetic fixture pipeline: filings=%d skipped=%d chunks=%d shards=%v sections=%v requests=%d", len(report.Filings), len(report.Skipped), report.Chunks, report.Shards, report.Sections, len(edgar.paths))
}
