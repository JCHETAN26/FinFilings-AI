package index

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// All corpora here are the synthetic fixtures from fixtures_test.go; nothing
// measured on them is SEC retrieval quality or production latency.

var fixtureShardKeys = [...]ShardKey{{2023, 3}, {2023, 4}, {2024, 1}, {2024, 2}}

// fixtureShardKey assigns contiguous document ranges to chronological
// quarters, so shard order equals global document order.
func fixtureShardKey(i, n int) ShardKey { return fixtureShardKeys[i*len(fixtureShardKeys)/n] }

func buildFixtureShards(tb testing.TB, root string, n int, opts ShardedWriterOptions) {
	tb.Helper()
	g := newFixtureGen()
	w, err := NewShardedWriter(root, opts)
	if err != nil {
		tb.Fatal(err)
	}
	for i := range n {
		if err := w.Add(context.Background(), fixtureShardKey(i, n), g.chunk(i)); err != nil {
			tb.Fatal(errors.Join(err, w.Abort()))
		}
	}
	if err := w.Close(); err != nil {
		tb.Fatal(err)
	}
}

func TestShardKey(t *testing.T) {
	for date, want := range map[string]string{"2024-01-01": "2024_Q1", "2024-03-31": "2024_Q1", "2024-04-01": "2024_Q2", "2023-12-31": "2023_Q4"} {
		d, _ := time.Parse(time.DateOnly, date)
		if got := ShardKeyForDate(d).String(); got != want {
			t.Errorf("%s: got %s want %s", date, got, want)
		}
		if k, err := ParseShardKey(want); err != nil || k.String() != want {
			t.Errorf("parse %s: %v %v", want, k, err)
		}
	}
	for _, bad := range []string{"2024_Q0", "2024_Q5", "1900_Q1", "2024Q1", "24_Q1", "2024_Q1/..", ""} {
		if _, err := ParseShardKey(bad); !errors.Is(err, ErrInvalidArgument) {
			t.Errorf("%q accepted: %v", bad, err)
		}
	}
}

// The router must reproduce a single index over the same documents exactly:
// same IDs, same order, bit-identical scores.
func TestShardRouterMatchesSingleIndex(t *testing.T) {
	const n = 800
	wopts := WriterOptions{MaxDocsPerSegment: 64, Workers: 1}
	single, _ := openFixtureIndex(t, wopts, n)
	defer single.Close()
	root := filepath.Join(t.TempDir(), "segments")
	buildFixtureShards(t, root, n, ShardedWriterOptions{Writer: wopts})

	for _, workers := range []int{1, 3, 8} {
		r, err := OpenShardRouter(root, ShardRouterOptions{Workers: workers, MaxConcurrentSearches: 2})
		if err != nil {
			t.Fatal(err)
		}
		if got := len(r.Shards()); got != len(fixtureShardKeys) {
			t.Fatalf("shards=%d", got)
		}
		filters := []model.FilingFilter{{}, {Section: "Item 7"}, {Ticker: "MSFT", Section: "Item 1A"}}
		for _, q := range append(fixtureQueries(), "zzzunknown", "") {
			for _, f := range filters {
				for _, k := range []int{1, 10, 100} {
					want, err := single.SearchFiltered(context.Background(), q, k, f)
					if err != nil {
						t.Fatal(err)
					}
					got, err := r.SearchFiltered(context.Background(), q, k, f)
					if err != nil {
						t.Fatal(err)
					}
					if len(got) != len(want) {
						t.Fatalf("workers=%d q=%q f=%+v k=%d: %d hits, want %d", workers, q, f, k, len(got), len(want))
					}
					for i := range got {
						if got[i] != want[i] {
							t.Fatalf("workers=%d q=%q f=%+v k=%d rank %d: %+v want %+v", workers, q, f, k, i, got[i], want[i])
						}
					}
				}
			}
		}
		rs, err := r.Stats()
		if err != nil {
			t.Fatal(err)
		}
		ss, _ := single.Stats()
		if rs.Docs != ss.Docs || rs.Tokens != ss.Tokens || rs.Postings != ss.Postings {
			t.Fatalf("stats %+v vs %+v", rs, ss)
		}
		if err := r.Close(); err != nil {
			t.Fatal(err)
		}
		if _, err := r.Search(context.Background(), "revenue", 10); !errors.Is(err, ErrClosed) {
			t.Fatal(err)
		}
		if err := r.Close(); err != nil {
			t.Fatal("second Close", err)
		}
	}
	maps, err := os.ReadFile("/proc/self/maps")
	if err == nil && strings.Contains(string(maps), root) {
		t.Fatal("router left segment mappings after Close")
	}
}

func TestShardedWriterLimitsAndCleanup(t *testing.T) {
	g := newFixtureGen()
	base := t.TempDir()
	root := filepath.Join(base, "limited")
	w, err := NewShardedWriter(root, ShardedWriterOptions{MaxOpenShards: 2})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	for i, k := range []ShardKey{{2024, 1}, {2024, 2}} {
		if err := w.Add(ctx, k, g.chunk(i)); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Add(ctx, ShardKey{2024, 3}, g.chunk(3)); !errors.Is(err, ErrInvalidArgument) {
		t.Fatal("MaxOpenShards ignored", err)
	}
	if err := w.Add(ctx, ShardKey{2024, 1}, g.chunk(4)); !errors.Is(err, ErrWriterFailed) {
		t.Fatal("error not sticky", err)
	}
	if err := w.Close(); !errors.Is(err, ErrWriterFailed) {
		t.Fatal(err)
	}
	if _, err := os.Stat(root); !os.IsNotExist(err) {
		t.Fatal("failed root remains", err)
	}

	root = filepath.Join(base, "aborted")
	if w, err = NewShardedWriter(root, ShardedWriterOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := w.Add(ctx, ShardKey{2024, 1}, g.chunk(0)); err != nil {
		t.Fatal(err)
	}
	if err := w.Add(ctx, ShardKey{1800, 1}, g.chunk(1)); !errors.Is(err, ErrInvalidArgument) {
		t.Fatal("invalid key accepted", err)
	}
	if err := w.Abort(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(root); !os.IsNotExist(err) {
		t.Fatal("aborted root remains", err)
	}

	root = filepath.Join(base, "published")
	buildFixtureShards(t, root, 40, ShardedWriterOptions{})
	if _, err := NewShardedWriter(root, ShardedWriterOptions{}); !errors.Is(err, ErrIndexExists) {
		t.Fatal("published root reused", err)
	}
	if w, err = NewShardedWriter(filepath.Join(base, "empty"), ShardedWriterOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); !errors.Is(err, ErrInvalidArgument) {
		t.Fatal("empty sharded index published", err)
	}
}

func TestShardRouterRejectsCorruptRoots(t *testing.T) {
	src := filepath.Join(t.TempDir(), "segments")
	buildFixtureShards(t, src, 80, ShardedWriterOptions{})
	good, err := os.ReadFile(filepath.Join(src, ShardManifestName))
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string]string{
		"truncated":    string(good[:len(good)/2]),
		"trailing":     string(good) + "{}",
		"unknown":      strings.Replace(string(good), `"version"`, `"extra": 1, "version"`, 1),
		"version":      strings.Replace(string(good), `"version": 1`, `"version": 2`, 1),
		"docs":         strings.Replace(string(good), `"docs": 20`, `"docs": 21`, 1),
		"order":        strings.Replace(strings.Replace(string(good), "2023_Q3", "TMP", 1), "2023_Q4", "2023_Q3", 1),
		"traversal":    strings.Replace(string(good), "2023_Q3", "../2023_Q3", 1),
		"maxtermlen":   strings.Replace(string(good), `"max_term_len": 64`, `"max_term_len": 32`, 1),
		"missingshard": strings.Replace(string(good), "2023_Q3", "2022_Q1", 1),
	}
	for name, body := range cases {
		if body == string(good) {
			t.Fatalf("%s: corruption did not apply", name)
		}
		if err := os.WriteFile(filepath.Join(src, ShardManifestName), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		r, err := OpenShardRouter(src, ShardRouterOptions{})
		if err == nil {
			r.Close()
			t.Fatalf("%s: corrupt root opened", name)
		}
		if name != "missingshard" && !errors.Is(err, ErrCorrupt) {
			t.Fatalf("%s: %v", name, err)
		}
	}
	if err := os.Remove(filepath.Join(src, ShardManifestName)); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenShardRouter(src, ShardRouterOptions{}); err == nil {
		t.Fatal("unpublished root opened")
	}
	if err := os.WriteFile(filepath.Join(src, ShardManifestName), good, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenShardRouter(src, ShardRouterOptions{MaxShards: 2}); !errors.Is(err, ErrInvalidArgument) {
		t.Fatal("MaxShards ignored", err)
	}
	r, err := OpenShardRouter(src, ShardRouterOptions{})
	if err != nil {
		t.Fatal("restored manifest", err)
	}
	r.Close()
}

func TestShardRouterConcurrentSearchAndClose(t *testing.T) {
	root := filepath.Join(t.TempDir(), "segments")
	buildFixtureShards(t, root, 400, ShardedWriterOptions{Writer: WriterOptions{MaxDocsPerSegment: 50}})
	r, err := OpenShardRouter(root, ShardRouterOptions{MaxConcurrentSearches: 3, Workers: 2})
	if err != nil {
		t.Fatal(err)
	}
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := r.Search(cancelled, "revenue", 5); !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled search", err)
	}
	if _, err := r.Search(context.Background(), "revenue", 0); !errors.Is(err, ErrInvalidArgument) {
		t.Fatal("k=0 accepted", err)
	}
	queries := fixtureQueries()
	var wg sync.WaitGroup
	start := make(chan struct{})
	for g := range 16 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			for i := 0; ; i++ {
				hits, err := r.Search(context.Background(), queries[(g+i)%len(queries)], 10)
				if errors.Is(err, ErrClosed) {
					return
				}
				if err != nil {
					t.Error(err)
					return
				}
				for _, h := range hits {
					_ = h.Chunk.ID + h.Chunk.SectionItem // copied out of mappings
				}
			}
		}()
	}
	close(start)
	time.Sleep(50 * time.Millisecond)
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
	wg.Wait()
}

// Synthetic-fixture micro-benchmark for allocation tracking only.
func BenchmarkShardRouterSearchFixtures(b *testing.B) {
	root := filepath.Join(b.TempDir(), "segments")
	buildFixtureShards(b, root, fixtureCount, ShardedWriterOptions{Writer: WriterOptions{MaxDocsPerSegment: 128}})
	r, err := OpenShardRouter(root, ShardRouterOptions{})
	if err != nil {
		b.Fatal(err)
	}
	defer r.Close()
	queries := fixtureQueries()
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := r.Search(context.Background(), queries[i%len(queries)], 10); err != nil {
			b.Fatal(err)
		}
	}
}
