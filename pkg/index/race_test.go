package index

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"sync"
	"sync/atomic"
	"testing"
)

// These tests are most meaningful under `go test -race`.

func expectedResults(t *testing.T, ix *Index, queries []string, k int) [][]Hit {
	t.Helper()
	out := make([][]Hit, len(queries))
	for i, q := range queries {
		hits, err := ix.Search(context.Background(), q, k)
		if err != nil {
			t.Fatal(err)
		}
		out[i] = hits
	}
	return out
}

// TestConcurrentSearchesAreConsistent runs many goroutines sharing one Index
// (and therefore its scratch pool) and requires every result to equal the
// sequential result for that query.
func TestConcurrentSearchesAreConsistent(t *testing.T) {
	ix, _ := openFixtureIndex(t, WriterOptions{MaxDocsPerSegment: 300}, fixtureCount)
	queries := fixtureQueries()
	for i := range 20 {
		queries = append(queries, fmt.Sprintf("marker%04d revenue", i*37))
	}
	want := expectedResults(t, ix, queries, 25)

	const goroutines = 16
	var wg sync.WaitGroup
	errs := make(chan error, goroutines)
	for g := range goroutines {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for iter := range 30 {
				qi := (g*7 + iter) % len(queries)
				hits, err := ix.Search(context.Background(), queries[qi], 25)
				if err != nil {
					errs <- err
					return
				}
				if !slices.Equal(hits, want[qi]) {
					errs <- fmt.Errorf("goroutine %d query %q: result differs from sequential", g, queries[qi])
					return
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
}

// TestCloseDuringConcurrentSearches closes the Index while searches are in
// flight. Every search must either complete with the correct result or
// return ErrClosed; none may observe unmapped memory (which would fault).
func TestCloseDuringConcurrentSearches(t *testing.T) {
	ix, _ := openFixtureIndex(t, WriterOptions{MaxDocsPerSegment: 250}, fixtureCount)
	queries := fixtureQueries()
	want := expectedResults(t, ix, queries, 50)

	var completed, closedSeen atomic.Int64
	started := make(chan struct{})
	var startOnce sync.Once
	var wg sync.WaitGroup
	errs := make(chan error, 32)
	for g := range 12 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for iter := 0; ; iter++ {
				qi := (g + iter) % len(queries)
				hits, err := ix.Search(context.Background(), queries[qi], 50)
				if errors.Is(err, ErrClosed) {
					closedSeen.Add(1)
					return
				}
				if err != nil {
					errs <- err
					return
				}
				if !slices.Equal(hits, want[qi]) {
					errs <- fmt.Errorf("query %q: wrong result during close", queries[qi])
					return
				}
				if completed.Add(1) == 200 {
					startOnce.Do(func() { close(started) })
				}
			}
		}()
	}
	allDone := make(chan struct{})
	go func() { wg.Wait(); close(allDone) }()
	select {
	case <-started:
	case <-allDone: // searchers failed early; errs reports why
	}
	if err := ix.Close(); err != nil {
		t.Fatal(err)
	}
	<-allDone
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	if closedSeen.Load() != 12 {
		t.Fatalf("%d goroutines saw ErrClosed, want 12", closedSeen.Load())
	}
	// Hits returned before Close stay valid: they hold copies, not mmap views.
	if want[0][0].Chunk.ID == "" {
		t.Fatal("pre-close hit lost its data")
	}
	if _, err := ix.Search(context.Background(), "revenue", 5); !errors.Is(err, ErrClosed) {
		t.Fatalf("Search after Close: %v", err)
	}
	if _, err := ix.Stats(); !errors.Is(err, ErrClosed) {
		t.Fatalf("Stats after Close: %v", err)
	}
	if err := ix.Close(); err != nil {
		t.Fatalf("second Close: %v", err)
	}
}

// TestConcurrentWriterCalls issues Add from many goroutines; the Writer must
// serialize them and index every document exactly once.
func TestConcurrentWriterCalls(t *testing.T) {
	dir := t.TempDir()
	w, err := NewWriter(dir, WriterOptions{MaxBufferBytes: 64 << 10})
	if err != nil {
		t.Fatal(err)
	}
	g := newFixtureGen()
	var wg sync.WaitGroup
	const per = 50
	for p := range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := p * per; i < (p+1)*per; i++ {
				if err := w.Add(context.Background(), g.chunk(i)); err != nil {
					t.Error(err)
					return
				}
			}
		}()
	}
	wg.Wait()
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	ix, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer ix.Close()
	for i := range 8 * per {
		hits, err := ix.Search(context.Background(), fmt.Sprintf("marker%04d", i), 3)
		if err != nil || len(hits) != 1 || hits[0].Chunk.ID != g.chunk(i).ID {
			t.Fatalf("doc %d: %v %v", i, hits, err)
		}
	}
}

// TestParallelIngestUnderRace drives the worker pool with many workers and a
// tiny queue to maximize interleavings of dispatch, analysis, reordering,
// and flushing.
func TestParallelIngestUnderRace(t *testing.T) {
	ix, _ := openFixtureIndex(t, WriterOptions{Workers: 16, QueueDepth: 1, MaxBufferBytes: 40 << 10}, 500)
	st, err := ix.Stats()
	if err != nil {
		t.Fatal(err)
	}
	if st.Docs != 500 || st.Segments < 2 {
		t.Fatalf("stats %+v", st)
	}
}
