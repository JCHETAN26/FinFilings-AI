package ingest

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// SEC fair-access policy: at most 10 requests per second per user, with a
// User-Agent that declares who is making the requests and a contact email.
const (
	MaxRequestsPerSecond     = 10
	DefaultRequestsPerSecond = 5
	DefaultDataBaseURL       = "https://data.sec.gov"
	DefaultArchivesBaseURL   = "https://www.sec.gov"
)

// errHTTPStatus wraps a non-retryable or retry-exhausted HTTP status.
var errHTTPStatus = errors.New("ingest: unexpected EDGAR HTTP status")

var (
	userAgentPattern = regexp.MustCompile(`^[\x21-\x7e][\x20-\x7e]*\s[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$`)
	cikPattern       = regexp.MustCompile(`^\d{1,10}$`)
	accessionPattern = regexp.MustCompile(`^\d{10}-\d{2}-\d{6}$`)
	documentPattern  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	tickerPattern    = regexp.MustCompile(`^[A-Z0-9.\-]{1,16}$`)
)

// ClientOptions configures a Client. Zero values select the defaults.
type ClientOptions struct {
	// UserAgent is required and must be "<organization or name> <contact
	// email>", e.g. "FinFilingsResearch ops@example.org". It is sent on
	// every request.
	UserAgent string
	// RequestsPerSecond paces request starts for this Client, including
	// retries. Must be in (0, MaxRequestsPerSecond]; default 5.
	RequestsPerSecond float64
	// Timeout bounds each HTTP attempt (headers and body). Default 60s.
	Timeout time.Duration
	// MaxDocumentBytes bounds one downloaded filing document. Default
	// 128 MiB, max 512 MiB.
	MaxDocumentBytes int64
	// MaxIndexBytes bounds one submissions JSON document. Default 32 MiB.
	MaxIndexBytes int64
	// MaxRetries bounds retries after 429/5xx or transport errors that occur
	// before any body byte is consumed. Default 2, max 4.
	MaxRetries int
	// MaxRetryAfter rejects Retry-After delays longer than this. Default 60s.
	MaxRetryAfter time.Duration
	// DataBaseURL and ArchivesBaseURL default to the SEC hosts; tests point
	// them at a local server. Requests to any other host are refused.
	DataBaseURL     string
	ArchivesBaseURL string
	// Transport overrides the HTTP transport (default http.DefaultTransport).
	Transport http.RoundTripper
}

// Client fetches EDGAR submissions indexes and filing documents.
//
// Rate limiting: every attempt (first try, retry) reserves a start time at
// least 1/RequestsPerSecond + 1ms after the previous reservation, so no
// one-second window contains more than RequestsPerSecond starts; 429/503
// Retry-After pushes the next reservation further out. The limiter is per
// Client: share one Client for all EDGAR traffic in a process. It cannot see
// traffic from other processes or machines using the same identity.
//
// Redirects are not followed (they would bypass pacing and host checks); a
// 3xx response is an error.
type Client struct {
	opts      ClientOptions
	http      *http.Client
	data      *url.URL
	archives  *url.URL
	interval  time.Duration
	mu        sync.Mutex
	nextStart time.Time
}

// NewClient validates opts and returns a Client.
func NewClient(opts ClientOptions) (*Client, error) {
	if !userAgentPattern.MatchString(opts.UserAgent) || len(opts.UserAgent) > 256 {
		return nil, errors.New("ingest: UserAgent must be '<name> <contact email>' in printable ASCII")
	}
	if opts.RequestsPerSecond == 0 {
		opts.RequestsPerSecond = DefaultRequestsPerSecond
	}
	if opts.Timeout == 0 {
		opts.Timeout = 60 * time.Second
	}
	if opts.MaxDocumentBytes == 0 {
		opts.MaxDocumentBytes = 128 << 20
	}
	if opts.MaxIndexBytes == 0 {
		opts.MaxIndexBytes = 32 << 20
	}
	if opts.MaxRetries == 0 {
		opts.MaxRetries = 2
	}
	if opts.MaxRetryAfter == 0 {
		opts.MaxRetryAfter = 60 * time.Second
	}
	if opts.DataBaseURL == "" {
		opts.DataBaseURL = DefaultDataBaseURL
	}
	if opts.ArchivesBaseURL == "" {
		opts.ArchivesBaseURL = DefaultArchivesBaseURL
	}
	if !(opts.RequestsPerSecond > 0 && opts.RequestsPerSecond <= MaxRequestsPerSecond) || opts.Timeout <= 0 || opts.Timeout > 10*time.Minute ||
		opts.MaxDocumentBytes < 1 || opts.MaxDocumentBytes > 512<<20 || opts.MaxIndexBytes < 1 || opts.MaxIndexBytes > 128<<20 ||
		opts.MaxRetries < 0 || opts.MaxRetries > 4 || opts.MaxRetryAfter <= 0 || opts.MaxRetryAfter > 10*time.Minute {
		return nil, errors.New("ingest: invalid EDGAR client rate/timeout/size/retry limits")
	}
	data, err := parseBase(opts.DataBaseURL)
	if err != nil {
		return nil, err
	}
	archives, err := parseBase(opts.ArchivesBaseURL)
	if err != nil {
		return nil, err
	}
	transport := opts.Transport
	if transport == nil {
		transport = http.DefaultTransport
	}
	return &Client{
		opts:     opts,
		data:     data,
		archives: archives,
		interval: time.Duration(float64(time.Second)/opts.RequestsPerSecond) + time.Millisecond,
		http: &http.Client{
			Transport:     transport,
			Timeout:       opts.Timeout,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}, nil
}

func parseBase(raw string) (*url.URL, error) {
	u, err := url.Parse(strings.TrimRight(raw, "/"))
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, fmt.Errorf("ingest: invalid EDGAR base URL %q", raw)
	}
	return u, nil
}

// reserve returns the earliest start time this attempt may use and advances
// the shared schedule.
func (c *Client) reserve() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	start := time.Now()
	if c.nextStart.After(start) {
		start = c.nextStart
	}
	c.nextStart = start.Add(c.interval)
	return start
}

func (c *Client) deferUntil(t time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if t.After(c.nextStart) {
		c.nextStart = t
	}
}

func (c *Client) pace(ctx context.Context) error {
	delay := time.Until(c.reserve())
	if delay <= 0 {
		return ctx.Err()
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err() // the reserved slot is forfeited, never reused early
	}
}

func retryable(status int) bool {
	return status == http.StatusTooManyRequests || status == http.StatusInternalServerError || status == http.StatusBadGateway ||
		status == http.StatusServiceUnavailable || status == http.StatusGatewayTimeout
}

// get performs a paced GET with bounded retries. On success the caller owns
// the response body.
func (c *Client) get(ctx context.Context, u *url.URL) (*http.Response, error) {
	for attempt := 0; ; attempt++ {
		if err := c.pace(ctx); err != nil {
			return nil, err
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("User-Agent", c.opts.UserAgent)
		resp, err := c.http.Do(req)
		if err != nil {
			if ctx.Err() != nil || attempt >= c.opts.MaxRetries {
				return nil, fmt.Errorf("ingest: GET %s: %w", u, err)
			}
			c.deferUntil(time.Now().Add(time.Second << attempt))
			continue
		}
		if resp.StatusCode == http.StatusOK {
			return resp, nil
		}
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		resp.Body.Close()
		if !retryable(resp.StatusCode) || attempt >= c.opts.MaxRetries {
			return nil, fmt.Errorf("%w: GET %s: %s", errHTTPStatus, u, resp.Status)
		}
		wait := time.Second << attempt
		if ra := resp.Header.Get("Retry-After"); ra != "" {
			if secs, err := strconv.Atoi(ra); err == nil && secs >= 0 {
				wait = time.Duration(secs) * time.Second
			} else if at, err := http.ParseTime(ra); err == nil {
				wait = time.Until(at)
			}
		}
		if wait > c.opts.MaxRetryAfter {
			return nil, fmt.Errorf("%w: GET %s: Retry-After %v exceeds limit", errHTTPStatus, u, wait)
		}
		c.deferUntil(time.Now().Add(wait))
	}
}

// FilingRef identifies one filing document listed by EDGAR.
type FilingRef struct {
	CIK             string // decimal, no leading zeros
	Ticker          string // first listed exchange ticker
	Accession       string // 18 digits, dashes removed
	Form            model.FormType
	FilingDate      time.Time
	ReportDate      string // period of report as listed (may be empty)
	PrimaryDocument string
}

type submissions struct {
	CIK     string   `json:"cik"`
	Tickers []string `json:"tickers"`
	Filings struct {
		Recent struct {
			AccessionNumber []string `json:"accessionNumber"`
			FilingDate      []string `json:"filingDate"`
			ReportDate      []string `json:"reportDate"`
			Form            []string `json:"form"`
			PrimaryDocument []string `json:"primaryDocument"`
		} `json:"recent"`
	} `json:"filings"`
}

// RecentFilings lists the filings in the submissions API "recent" block
// (https://data.sec.gov/submissions/CIK##########.json) whose form is one of
// forms (default 10-K and 10-Q), in the order EDGAR lists them (newest
// first). Older filings paged into the "files" array are not fetched.
func (c *Client) RecentFilings(ctx context.Context, cik string, forms ...model.FormType) ([]FilingRef, error) {
	if !cikPattern.MatchString(cik) {
		return nil, fmt.Errorf("ingest: invalid CIK %q", cik)
	}
	n, _ := strconv.ParseUint(cik, 10, 64)
	if len(forms) == 0 {
		forms = []model.FormType{model.Form10K, model.Form10Q}
	}
	u := c.data.JoinPath("submissions", fmt.Sprintf("CIK%010d.json", n))
	resp, err := c.get(ctx, u)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, c.opts.MaxIndexBytes+1))
	if err != nil {
		return nil, fmt.Errorf("ingest: read submissions: %w", err)
	}
	if int64(len(body)) > c.opts.MaxIndexBytes {
		return nil, fmt.Errorf("ingest: submissions JSON exceeds %d bytes", c.opts.MaxIndexBytes)
	}
	var s submissions
	if err := json.Unmarshal(body, &s); err != nil {
		return nil, fmt.Errorf("ingest: decode submissions: %w", err)
	}
	if got, _ := strconv.ParseUint(strings.TrimLeft(s.CIK, "0"), 10, 64); got != n {
		return nil, fmt.Errorf("ingest: submissions CIK %q does not match %s", s.CIK, cik)
	}
	r := s.Filings.Recent
	count := len(r.AccessionNumber)
	if len(r.FilingDate) != count || len(r.Form) != count || len(r.PrimaryDocument) != count || (len(r.ReportDate) != 0 && len(r.ReportDate) != count) {
		return nil, errors.New("ingest: submissions columns have different lengths")
	}
	ticker := ""
	if len(s.Tickers) > 0 {
		ticker = strings.ToUpper(s.Tickers[0])
	}
	var refs []FilingRef
	for i := range count {
		form := model.FormType(r.Form[i])
		if !containsForm(forms, form) {
			continue
		}
		if !tickerPattern.MatchString(ticker) {
			return nil, fmt.Errorf("ingest: CIK %s has no valid ticker", cik)
		}
		date, err := time.Parse(time.DateOnly, r.FilingDate[i])
		if err != nil || !accessionPattern.MatchString(r.AccessionNumber[i]) || !documentPattern.MatchString(r.PrimaryDocument[i]) {
			return nil, fmt.Errorf("ingest: invalid submissions row %d", i)
		}
		ref := FilingRef{CIK: strconv.FormatUint(n, 10), Ticker: ticker, Accession: strings.ReplaceAll(r.AccessionNumber[i], "-", ""), Form: form, FilingDate: date, PrimaryDocument: r.PrimaryDocument[i]}
		if len(r.ReportDate) != 0 {
			ref.ReportDate = r.ReportDate[i]
		}
		refs = append(refs, ref)
	}
	return refs, nil
}

func containsForm(forms []model.FormType, f model.FormType) bool {
	for _, x := range forms {
		if x == f {
			return true
		}
	}
	return false
}

// DocumentURL returns the Archives URL of ref's primary document.
func (c *Client) DocumentURL(ref FilingRef) (*url.URL, error) {
	if !cikPattern.MatchString(ref.CIK) || len(ref.Accession) != 18 || strings.Trim(ref.Accession, "0123456789") != "" || !documentPattern.MatchString(ref.PrimaryDocument) {
		return nil, errors.New("ingest: invalid filing reference")
	}
	return c.archives.JoinPath("Archives", "edgar", "data", ref.CIK, ref.Accession, ref.PrimaryDocument), nil
}

// Download describes one streamed document.
type Download struct {
	URL    string `json:"url"`
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}

// Download streams ref's primary document into dst (never buffering the
// whole body), failing once it exceeds MaxDocumentBytes. Retries happen only
// before the body is read; a failure mid-body returns an error and dst may
// hold a partial copy the caller must discard.
func (c *Client) Download(ctx context.Context, ref FilingRef, dst io.Writer) (Download, error) {
	u, err := c.DocumentURL(ref)
	if err != nil {
		return Download{}, err
	}
	resp, err := c.get(ctx, u)
	if err != nil {
		return Download{}, err
	}
	defer resp.Body.Close()
	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(dst, h), io.LimitReader(resp.Body, c.opts.MaxDocumentBytes+1))
	if err != nil {
		return Download{}, fmt.Errorf("ingest: download %s: %w", u, err)
	}
	if n > c.opts.MaxDocumentBytes {
		return Download{}, fmt.Errorf("ingest: %s exceeds %d bytes", u, c.opts.MaxDocumentBytes)
	}
	return Download{URL: u.String(), Bytes: n, SHA256: hex.EncodeToString(h.Sum(nil))}, nil
}
