package index

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// TestFixtureIndexCompression indexes the 1,000 synthetic fixture chunks and
// checks the compressed posting size against the raw 8-bytes-per-posting
// baseline. The ratio is a fixture measurement, not an SEC corpus figure.
func TestFixtureIndexCompression(t *testing.T) {
	ix, _ := openFixtureIndex(t, WriterOptions{}, fixtureCount)
	st, err := ix.Stats()
	if err != nil {
		t.Fatal(err)
	}
	ref := newRefIndex(t, fixtureCount)
	var wantPostings uint64
	for _, m := range ref.tfs {
		wantPostings += uint64(len(m))
	}
	if st.Docs != fixtureCount || st.Segments != 1 || st.Postings != wantPostings || st.Tokens != ref.tokens {
		t.Fatalf("stats %+v; want docs=%d segments=1 postings=%d tokens=%d", st, fixtureCount, wantPostings, ref.tokens)
	}
	if st.Terms != uint64(len(ref.df)) {
		t.Fatalf("terms = %d, want %d", st.Terms, len(ref.df))
	}
	ratio := float64(st.PostingBytes) / float64(st.RawPostingBytes())
	t.Logf("fixture postings: %d postings, %d raw bytes, %d compressed bytes, ratio %.4f (%.2f bits/posting), segment file %d bytes",
		st.Postings, st.RawPostingBytes(), st.PostingBytes, ratio, 8*float64(st.PostingBytes)/float64(st.Postings), st.FileBytes)
	if ratio >= 0.5 {
		t.Fatalf("compression ratio %.4f, want < 0.5 on fixtures", ratio)
	}
}

// TestFixtureRetrievalExactMarkers queries each fixture's unique marker term
// and requires that exact chunk, with its stored metadata, as the only hit.
func TestFixtureRetrievalExactMarkers(t *testing.T) {
	ix, _ := openFixtureIndex(t, WriterOptions{}, fixtureCount)
	g := newFixtureGen()
	ctx := context.Background()
	for i := range fixtureCount {
		hits, err := ix.Search(ctx, fmt.Sprintf("MARKER%04d", i), 5)
		if err != nil {
			t.Fatal(err)
		}
		c := g.chunk(i)
		if len(hits) != 1 || hits[0].Chunk != c.Meta() {
			t.Fatalf("marker %d: hits %+v, want exactly %+v", i, hits, c.Meta())
		}
	}
}

// TestFixtureRetrievalMatchesReference compares rankings and scores with a
// brute-force BM25 oracle, for a single segment and for many small segments
// (which exercises global IDF/avgdl across segments).
func TestFixtureRetrievalMatchesReference(t *testing.T) {
	ref := newRefIndex(t, fixtureCount)
	for _, tc := range []struct {
		name string
		opts WriterOptions
	}{
		{"single-segment", WriterOptions{}},
		{"multi-segment-memory-limit", WriterOptions{MaxBufferBytes: 48 << 10, Workers: 4}},
		{"multi-segment-doc-limit", WriterOptions{MaxDocsPerSegment: 97, Workers: 3, QueueDepth: 1}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ix, _ := openFixtureIndex(t, tc.opts, fixtureCount)
			st, err := ix.Stats()
			if err != nil {
				t.Fatal(err)
			}
			t.Logf("segments=%d", st.Segments)
			if tc.name != "single-segment" && st.Segments < 3 {
				t.Fatalf("expected several segments, got %d", st.Segments)
			}
			for _, q := range fixtureQueries() {
				for _, k := range []int{1, 10, 1000} {
					hits, err := ix.Search(context.Background(), q, k)
					if err != nil {
						t.Fatal(err)
					}
					want := ref.search(q, k, BM25Params{K1: 1.2, B: 0.75})
					if len(hits) != len(want) {
						t.Fatalf("%q k=%d: %d hits, want %d", q, k, len(hits), len(want))
					}
					for i := range want {
						if hits[i].Chunk.ID != want[i].id || math.Abs(hits[i].Score-want[i].score) > 1e-9 {
							t.Fatalf("%q k=%d rank %d: got %s %.12f, want %s %.12f",
								q, k, i, hits[i].Chunk.ID, hits[i].Score, want[i].id, want[i].score)
						}
					}
				}
			}
		})
	}
}

func TestFixtureTopicPrecision(t *testing.T) {
	ix, _ := openFixtureIndex(t, WriterOptions{}, fixtureCount)
	hits, err := ix.Search(context.Background(), "cybersecurity", 1000)
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) != fixtureCount/10 {
		t.Fatalf("got %d hits, want %d", len(hits), fixtureCount/10)
	}
	for i, h := range hits {
		var n int
		if _, err := fmt.Sscanf(h.Chunk.ID[len(h.Chunk.ID)-4:], "%d", &n); err != nil || n%10 != 0 {
			t.Fatalf("hit %d %s does not contain the planted term", i, h.Chunk.ID)
		}
		if i > 0 && h.Score > hits[i-1].Score {
			t.Fatalf("hits not sorted at %d", i)
		}
	}
}

func TestTokensTakePrecedenceOverText(t *testing.T) {
	ix, _ := openFixtureIndex(t, WriterOptions{}, 50)
	hits, err := ix.Search(context.Background(), "ignoredtextsentinel", 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) != 0 {
		t.Fatalf("Text was indexed for a chunk with Tokens: %+v", hits)
	}
	hits, err = ix.Search(context.Background(), "marker0007", 10) // 7 uses Tokens
	if err != nil || len(hits) != 1 {
		t.Fatalf("pre-tokenized chunk not retrievable: %v %+v", err, hits)
	}
}

// TestTokensAreAnalyzerNormalized pins the documented Tokens contract: each
// element is normalized by the query analyzer, so every indexed term is
// reachable by a query, and elements are not stored as verbatim terms.
func TestTokensAreAnalyzerNormalized(t *testing.T) {
	dir := t.TempDir()
	w, err := NewWriter(dir, WriterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	c := newFixtureGen().chunk(1)
	c.Text, c.Tokens = "", []string{"Revenue", "10-K", "Item-7A", "---"}
	if err := w.Add(context.Background(), c); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	ix, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer ix.Close()
	for _, q := range []string{"revenue", "REVENUE", "10-K", "10 k", "item 7a", "Item-7A"} {
		if hits, err := ix.Search(context.Background(), q, 5); err != nil || len(hits) != 1 {
			t.Fatalf("query %q: %v %v", q, hits, err)
		}
	}
	seg := ix.segs[0]
	if _, ok := seg.lookup([]byte("10-k")); ok {
		t.Fatal("token element indexed verbatim")
	}
	var terms []string
	for i := range int(seg.termCount) {
		terms = append(terms, string(seg.term(i)))
	}
	if strings.Join(terms, ",") != "10,7a,item,k,revenue" {
		t.Fatalf("indexed terms %v", terms)
	}
	if seg.docLen(0) != 5 { // "---" yields no terms
		t.Fatalf("doc length %d, want 5", seg.docLen(0))
	}
}

func TestSearchArgumentsAndEmptyQueries(t *testing.T) {
	ix, _ := openFixtureIndex(t, WriterOptions{}, 20)
	ctx := context.Background()
	for _, k := range []int{0, -1, 1001} {
		if _, err := ix.Search(ctx, "revenue", k); !errors.Is(err, ErrInvalidArgument) {
			t.Fatalf("k=%d: err %v, want ErrInvalidArgument", k, err)
		}
	}
	for _, q := range []string{"", "   ,;- ", "zzzznotindexed", strings.Repeat("x", 200)} {
		hits, err := ix.Search(ctx, q, 10)
		if err != nil || len(hits) != 0 {
			t.Fatalf("%q: hits %v err %v, want none", q, hits, err)
		}
	}
	var long strings.Builder
	for i := range 65 {
		fmt.Fprintf(&long, " q%d", i)
	}
	if _, err := ix.Search(ctx, long.String(), 10); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("65 unique terms: err %v, want ErrInvalidArgument", err)
	}
	cctx, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := ix.Search(cctx, "revenue", 10); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled ctx: err %v", err)
	}
}

func TestOptionsValidation(t *testing.T) {
	dir := t.TempDir()
	bad := []WriterOptions{
		{MaxBufferBytes: 100},
		{MaxDocsPerSegment: -1},
		{MaxDocsPerSegment: MaxDocsPerSegmentLimit + 1},
		{Workers: -2},
		{QueueDepth: -1},
		{MaxTermLen: MaxTermLenLimit + 1},
	}
	for _, o := range bad {
		if _, err := NewWriter(dir, o); !errors.Is(err, ErrInvalidArgument) {
			t.Fatalf("%+v: err %v", o, err)
		}
	}
	for _, o := range []Options{{BM25: BM25Params{K1: -1}}, {BM25: BM25Params{K1: 1, B: 2}}, {MaxTopK: -5}, {MaxQueryTerms: 1 << 20}} {
		if _, err := Open(dir, o); !errors.Is(err, ErrInvalidArgument) {
			t.Fatalf("%+v: err %v", o, err)
		}
	}
}

// TestWriterMemoryLimitIsEnforced checks the accounted buffer never exceeds
// MaxBufferBytes and that flushes happen instead.
func TestWriterMemoryLimitIsEnforced(t *testing.T) {
	const limit = 32 << 10
	w, err := NewWriter(t.TempDir(), WriterOptions{MaxBufferBytes: limit})
	if err != nil {
		t.Fatal(err)
	}
	g := newFixtureGen()
	for i := range 300 {
		if err := w.Add(context.Background(), g.chunk(i)); err != nil {
			t.Fatal(err)
		}
		if w.buf.bytes > limit {
			t.Fatalf("after doc %d buffer accounts %d bytes > limit %d", i, w.buf.bytes, limit)
		}
	}
	if len(w.segments) < 5 {
		t.Fatalf("expected flushes, got %d segments", len(w.segments))
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestWriterDocTooLargeIsStickyAndUnpublished(t *testing.T) {
	dir := t.TempDir()
	w, err := NewWriter(dir, WriterOptions{MaxBufferBytes: 16 << 10})
	if err != nil {
		t.Fatal(err)
	}
	g := newFixtureGen()
	if err := w.Add(context.Background(), g.chunk(1)); err != nil {
		t.Fatal(err)
	}
	var sb strings.Builder
	for i := range 2000 {
		fmt.Fprintf(&sb, "uniq%d ", i)
	}
	big := g.chunk(2)
	big.Tokens, big.Text = nil, sb.String()
	if err := w.Add(context.Background(), big); !errors.Is(err, ErrDocTooLarge) {
		t.Fatalf("err %v, want ErrDocTooLarge", err)
	}
	if err := w.Add(context.Background(), g.chunk(3)); !errors.Is(err, ErrWriterFailed) {
		t.Fatalf("after failure: err %v, want ErrWriterFailed", err)
	}
	if err := w.Close(); !errors.Is(err, ErrWriterFailed) {
		t.Fatalf("Close: err %v, want ErrWriterFailed", err)
	}
	if _, err := os.Stat(filepath.Join(dir, ManifestName)); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("manifest published after failure: %v", err)
	}
	if err := w.Abort(); err != nil {
		t.Fatal(err)
	}
	if left, _ := filepath.Glob(filepath.Join(dir, "seg-*")); len(left) != 0 {
		t.Fatalf("Abort left segments: %v", left)
	}
}

func TestIngestPropagatesValidationErrors(t *testing.T) {
	w, err := NewWriter(t.TempDir(), WriterOptions{Workers: 4})
	if err != nil {
		t.Fatal(err)
	}
	g := newFixtureGen()
	src := make(chan model.SECChunk)
	go func() {
		defer close(src)
		for i := range 200 {
			c := g.chunk(i)
			if i == 120 {
				c.CIK = "12AB"
			}
			src <- c
		}
	}()
	err = w.Ingest(context.Background(), src)
	var ve *model.ValidationError
	if !errors.As(err, &ve) || ve.Field != "CIK" {
		t.Fatalf("err %v, want CIK validation error", err)
	}
	if err := w.Close(); !errors.Is(err, ErrWriterFailed) {
		t.Fatalf("Close: %v", err)
	}
	// The producer must not be stuck: Ingest stopped reading, so drain.
	for range src {
	}
}

func TestIngestEmptyDocumentRejected(t *testing.T) {
	w, err := NewWriter(t.TempDir(), WriterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	c := newFixtureGen().chunk(1)
	c.Text = " ,,, ;; "
	if err := w.Add(context.Background(), c); !errors.Is(err, ErrEmptyDocument) {
		t.Fatalf("err %v, want ErrEmptyDocument", err)
	}
}

func TestIngestCancellation(t *testing.T) {
	w, err := NewWriter(t.TempDir(), WriterOptions{Workers: 2})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	src := make(chan model.SECChunk) // never closed
	g := newFixtureGen()
	go func() {
		for i := range 10 {
			select {
			case src <- g.chunk(i):
			case <-ctx.Done():
				return
			}
		}
		cancel()
	}()
	done := make(chan error, 1)
	go func() { done <- w.Ingest(ctx, src) }()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("err %v, want context.Canceled", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Ingest did not return after cancellation")
	}
}

func TestWriterRefusesExistingIndex(t *testing.T) {
	_, dir := openFixtureIndex(t, WriterOptions{}, 10)
	if _, err := NewWriter(dir, WriterOptions{}); !errors.Is(err, ErrIndexExists) {
		t.Fatalf("err %v, want ErrIndexExists", err)
	}
}

func TestWriterCloseSemantics(t *testing.T) {
	dir := t.TempDir()
	w, err := NewWriter(dir, WriterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); !errors.Is(err, ErrClosed) {
		t.Fatalf("second Close: %v", err)
	}
	if err := w.Add(context.Background(), newFixtureGen().chunk(0)); !errors.Is(err, ErrClosed) {
		t.Fatalf("Add after Close: %v", err)
	}
	ix, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer ix.Close()
	if hits, err := ix.Search(context.Background(), "revenue", 10); err != nil || hits != nil {
		t.Fatalf("empty index: %v %v", hits, err)
	}
}

// TestIngestIsDeterministic checks that the parallel worker pool produces
// byte-identical segments to sequential Add.
func TestIngestIsDeterministic(t *testing.T) {
	seqDir, parDir := t.TempDir(), filepath.Join(t.TempDir(), "par")
	opts := WriterOptions{MaxBufferBytes: 64 << 10}
	w, err := NewWriter(seqDir, opts)
	if err != nil {
		t.Fatal(err)
	}
	g := newFixtureGen()
	for i := range 400 {
		if err := w.Add(context.Background(), g.chunk(i)); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	opts.Workers, opts.QueueDepth = 8, 3
	buildFixtureIndex(t, parDir, opts, 400)
	for _, name := range []string{ManifestName, segmentFileName(0), segmentFileName(1)} {
		a, errA := os.ReadFile(filepath.Join(seqDir, name))
		b, errB := os.ReadFile(filepath.Join(parDir, name))
		if errA != nil || errB != nil || !bytes.Equal(a, b) {
			t.Fatalf("%s differs between sequential and parallel ingest (%v, %v)", name, errA, errB)
		}
	}
}

func TestChunkValidation(t *testing.T) {
	good := newFixtureGen().chunk(3)
	if err := good.Validate(); err != nil {
		t.Fatal(err)
	}
	for field, mutate := range map[string]func(*model.SECChunk){
		"ID":          func(c *model.SECChunk) { c.ID = "" },
		"Ticker":      func(c *model.SECChunk) { c.Ticker = strings.Repeat("A", model.MaxTickerLen+1) },
		"CIK":         func(c *model.SECChunk) { c.CIK = "0000-1" },
		"FormType":    func(c *model.SECChunk) { c.FormType = "8-K" },
		"FilingYear":  func(c *model.SECChunk) { c.FilingYear = 1980 },
		"SectionItem": func(c *model.SECChunk) { c.SectionItem = "\xff" },
		"Text":        func(c *model.SECChunk) { c.Text, c.Tokens = "", nil },
	} {
		c := good
		mutate(&c)
		var ve *model.ValidationError
		if err := c.Validate(); !errors.As(err, &ve) || ve.Field != field {
			t.Fatalf("%s: err %v", field, err)
		}
	}
}

func TestTokenizer(t *testing.T) {
	tok := newTokenizer(8)
	var got []string
	in := "Net-Revenue 10-K ÉLAN  toolongtoken  $1,234 straße"
	for tk, pos := tok.next(in, 0); tk != nil; tk, pos = tok.next(in, pos) {
		got = append(got, string(tk))
	}
	want := []string{"net", "revenue", "10", "k", "élan", "1", "234", "straße"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("got %q, want %q", got, want)
	}
}
