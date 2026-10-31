package index

import (
	"context"
	"errors"
	"path/filepath"
	"slices"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func openWithOptions(t *testing.T, wopts WriterOptions, n int, opts Options) *Index {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "idx")
	buildFixtureIndex(t, dir, wopts, n)
	ix, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ix.Close() })
	return ix
}

// TestSearchAdmissionBoundsConcurrency starts 24 searches against 3 slots,
// holds admitted searches until the 3rd is admitted, and requires that
// exactly 3 were ever admitted at once and all searches succeed.
func TestSearchAdmissionBoundsConcurrency(t *testing.T) {
	const slots, callers = 3, 24
	ix := openWithOptions(t, WriterOptions{}, 200, Options{MaxConcurrentSearches: slots})
	want, err := ix.Search(context.Background(), "revenue growth", 10)
	if err != nil {
		t.Fatal(err)
	}
	var active, peak atomic.Int64
	gate := make(chan struct{})
	var gateOnce sync.Once
	ix.hooks = searchHooks{
		admitted: func() {
			n := active.Add(1)
			for p := peak.Load(); n > p && !peak.CompareAndSwap(p, n); p = peak.Load() {
			}
			if n == slots {
				gateOnce.Do(func() { close(gate) })
			}
			<-gate
		},
		released: func() { active.Add(-1) },
	}

	var wg sync.WaitGroup
	errs := make(chan error, callers)
	for range callers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			hits, err := ix.Search(context.Background(), "revenue growth", 10)
			if err == nil && !slices.Equal(hits, want) {
				err = errors.New("wrong result")
			}
			if err != nil {
				errs <- err
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	if got := peak.Load(); got != slots {
		t.Fatalf("peak admitted searches %d, want exactly %d", got, slots)
	}
	if len(ix.slots) != 0 {
		t.Fatalf("%d slots leaked", len(ix.slots))
	}
}

// TestQueuedSearchCancellation fills every slot and checks that queued
// callers leave on deadline or cancellation without leaking a slot.
func TestQueuedSearchCancellation(t *testing.T) {
	ix := openWithOptions(t, WriterOptions{}, 50, Options{MaxConcurrentSearches: 2})
	ix.slots <- struct{}{} // simulate two long-running searches
	ix.slots <- struct{}{}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if _, err := ix.Search(ctx, "revenue", 5); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("deadline while queued: err %v", err)
	}

	cctx, ccancel := context.WithCancel(context.Background())
	const queued = 8
	results := make(chan error, queued)
	for range queued {
		go func() {
			_, err := ix.Search(cctx, "revenue", 5)
			results <- err
		}()
	}
	time.Sleep(20 * time.Millisecond) // let them queue; correctness does not depend on it
	ccancel()
	for range queued {
		select {
		case err := <-results:
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("canceled while queued: err %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("queued search did not observe cancellation")
		}
	}
	if len(ix.slots) != 2 {
		t.Fatalf("cancelled callers changed slot occupancy to %d", len(ix.slots))
	}
	<-ix.slots
	if hits, err := ix.Search(context.Background(), "marker0003", 5); err != nil || len(hits) != 1 {
		t.Fatalf("search after a slot frees: %v %v", hits, err)
	}
	<-ix.slots
}

// TestCloseWakesQueuedSearches: with all slots taken, queued callers must be
// woken by Close with ErrClosed, and Close must not wait for slots.
func TestCloseWakesQueuedSearches(t *testing.T) {
	ix := openWithOptions(t, WriterOptions{}, 50, Options{MaxConcurrentSearches: 1})
	ix.slots <- struct{}{}
	const queued = 8
	results := make(chan error, queued)
	for range queued {
		go func() {
			_, err := ix.Search(context.Background(), "revenue", 5)
			results <- err
		}()
	}
	time.Sleep(20 * time.Millisecond)
	closed := make(chan error, 1)
	go func() { closed <- ix.Close() }()
	select {
	case err := <-closed:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Close deadlocked with queued searches")
	}
	for range queued {
		select {
		case err := <-results:
			if !errors.Is(err, ErrClosed) {
				t.Fatalf("queued search after Close: err %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("queued search not woken by Close")
		}
	}
	<-ix.slots
	if _, err := ix.Search(context.Background(), "revenue", 5); !errors.Is(err, ErrClosed) {
		t.Fatalf("after Close: %v", err)
	}
}

// TestCloseDuringQueuedAndAdmittedSearches mixes admitted and queued callers
// (2 slots, 16 goroutines) with a concurrent Close. Every call must return a
// correct result or ErrClosed, and Close must return.
func TestCloseDuringQueuedAndAdmittedSearches(t *testing.T) {
	ix := openWithOptions(t, WriterOptions{MaxDocsPerSegment: 200}, fixtureCount, Options{MaxConcurrentSearches: 2})
	queries := fixtureQueries()
	want := expectedResults(t, ix, queries, 20)
	var done atomic.Int64
	var wg sync.WaitGroup
	errs := make(chan error, 16)
	for g := range 16 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; ; i++ {
				q := (g + i) % len(queries)
				hits, err := ix.Search(context.Background(), queries[q], 20)
				if errors.Is(err, ErrClosed) {
					return
				}
				if err == nil && !slices.Equal(hits, want[q]) {
					err = errors.New("wrong result")
				}
				if err != nil {
					errs <- err
					return
				}
				done.Add(1)
			}
		}()
	}
	for done.Load() < 100 {
		time.Sleep(time.Millisecond)
	}
	closed := make(chan error, 1)
	go func() { closed <- ix.Close() }()
	select {
	case err := <-closed:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Close did not return")
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
}
