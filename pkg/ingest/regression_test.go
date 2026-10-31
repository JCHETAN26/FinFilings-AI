package ingest

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

// A controlled transport keeps the first headers pending until another caller
// is waiting. The 429 must postpone that caller as well as its own retry.
type cooldownTransport struct {
	mu             sync.Mutex
	starts         []time.Time
	first, release chan struct{}
}

func (r *cooldownTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	r.mu.Lock()
	r.starts = append(r.starts, time.Now())
	n := len(r.starts)
	r.mu.Unlock()
	status := http.StatusOK
	h := http.Header{}
	if n == 1 {
		close(r.first)
		<-r.release
		status = http.StatusTooManyRequests
		h.Set("Retry-After", "1")
	}
	return &http.Response{StatusCode: status, Status: http.StatusText(status), Header: h, Body: io.NopCloser(strings.NewReader("ok")), Request: req}, nil
}
func TestConcurrentCallersHonorRetryAfter(t *testing.T) {
	rt := &cooldownTransport{first: make(chan struct{}), release: make(chan struct{})}
	c, err := NewClient(ClientOptions{UserAgent: testUserAgent, RequestsPerSecond: 10, Transport: rt})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ref := FilingRef{CIK: "320193", Accession: "000032019323000106", PrimaryDocument: "test.htm"}
	done := make(chan error, 2)
	download := func() { _, err := c.Download(ctx, ref, io.Discard); done <- err }
	go download()
	<-rt.first
	go download()
	// Give the concurrent caller time to enter admission while headers are pending.
	time.Sleep(150 * time.Millisecond)
	released := time.Now()
	close(rt.release)
	for range 2 {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	rt.mu.Lock()
	defer rt.mu.Unlock()
	if len(rt.starts) != 3 {
		t.Fatal(rt.starts)
	}
	for _, start := range rt.starts[1:] {
		if start.Sub(released) < time.Second {
			t.Fatalf("request bypassed shared Retry-After: %v", start.Sub(released))
		}
	}
	// A cancelled caller must not wait for the admission gate.
	c.dispatch <- struct{}{}
	cancelled, stop := context.WithCancel(context.Background())
	stop()
	_, err = c.Download(cancelled, ref, io.Discard)
	<-c.dispatch
	if err == nil {
		t.Fatal("cancelled caller admitted")
	}
}

func TestParserLargeLinesWithinBound(t *testing.T) {
	for _, format := range []Format{FormatHTML, FormatText} {
		t.Run(string(format), func(t *testing.T) {
			var doc strings.Builder
			for _, item := range []string{"1A", "7", "8"} {
				title := strings.ReplaceAll(fixtureTitles[item], "&#8217;", "’")
				if format == FormatHTML {
					fmt.Fprintf(&doc, "<h2>Item %s. %s</h2><p>%s</p>", item, title, strings.Repeat("revenue ", 12000))
				} else {
					fmt.Fprintf(&doc, "Item %s. %s\n%s\n", item, title, strings.Repeat("revenue ", 12000))
				}
			}
			chunks, report, err := parseFixture(t, doc.String(), format, ParserOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if len(chunks) == 0 || len(report.Sections) != 3 {
				t.Fatal(report)
			}
			for _, c := range chunks {
				if len(c.Text) > maxChunkTextBytes {
					t.Fatal("unbounded chunk")
				}
			}
		})
	}
	if _, _, err := parseFixture(t, "<p>"+strings.Repeat("x", maxLineBytes+1)+"</p>", FormatHTML, ParserOptions{}); err == nil {
		t.Fatal("oversized HTML accepted")
	}
	if _, _, err := parseFixture(t, strings.Repeat("x", maxLineBytes+1), FormatText, ParserOptions{}); err == nil {
		t.Fatal("oversized text accepted")
	}
}

type exhaustedTransport struct{ starts []time.Time }

func (r *exhaustedTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	r.starts = append(r.starts, time.Now())
	status := http.StatusOK
	h := http.Header{}
	if len(r.starts) <= 2 {
		status = http.StatusTooManyRequests
		h.Set("Retry-After", "1")
	}
	return &http.Response{StatusCode: status, Status: http.StatusText(status), Header: h, Body: io.NopCloser(strings.NewReader("ok")), Request: req}, nil
}
func TestExhaustedRetriesPublishCooldown(t *testing.T) {
	rt := &exhaustedTransport{}
	c, err := NewClient(ClientOptions{UserAgent: testUserAgent, RequestsPerSecond: 10, MaxRetries: 1, Transport: rt})
	if err != nil {
		t.Fatal(err)
	}
	ref := FilingRef{CIK: "320193", Accession: "000032019323000106", PrimaryDocument: "test.htm"}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := c.Download(ctx, ref, io.Discard); err == nil {
		t.Fatal("exhausted retry should fail")
	}
	if _, err := c.Download(ctx, ref, io.Discard); err != nil {
		t.Fatal(err)
	}
	if len(rt.starts) != 3 || rt.starts[2].Sub(rt.starts[1]) < time.Second {
		t.Fatal("next caller bypassed final Retry-After", rt.starts)
	}
}
