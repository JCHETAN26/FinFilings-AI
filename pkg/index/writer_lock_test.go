package index

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func segFiles(t *testing.T, dir string) []string {
	t.Helper()
	files, err := abandonedFiles(dir)
	if err != nil {
		t.Fatal(err)
	}
	return files
}

func TestCompetingWritersAreExcluded(t *testing.T) {
	dir := t.TempDir()
	w1, err := NewWriter(dir, WriterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := NewWriter(dir, WriterOptions{}); !errors.Is(err, ErrWriterLocked) {
		t.Fatalf("second writer: err %v, want ErrWriterLocked", err)
	}
	if _, err := CleanAbandoned(dir); !errors.Is(err, ErrWriterLocked) {
		t.Fatalf("CleanAbandoned during write: err %v, want ErrWriterLocked", err)
	}
	if err := w1.Add(context.Background(), newFixtureGen().chunk(0)); err != nil {
		t.Fatal(err)
	}
	if err := w1.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := NewWriter(dir, WriterOptions{}); !errors.Is(err, ErrIndexExists) {
		t.Fatalf("after publish: err %v, want ErrIndexExists", err)
	}
	if _, err := CleanAbandoned(dir); !errors.Is(err, ErrIndexExists) {
		t.Fatalf("CleanAbandoned on published index: err %v", err)
	}
}

func TestConcurrentNewWriterExactlyOneWins(t *testing.T) {
	dir := t.TempDir()
	const n = 16
	var wg sync.WaitGroup
	writers := make(chan *Writer, n)
	errs := make(chan error, n)
	start := make(chan struct{})
	for range n {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			w, err := NewWriter(dir, WriterOptions{})
			if err != nil {
				errs <- err
				return
			}
			writers <- w
		}()
	}
	close(start)
	wg.Wait()
	close(writers)
	close(errs)
	if len(writers) != 1 {
		t.Fatalf("%d writers acquired the lock, want 1", len(writers))
	}
	for err := range errs {
		if !errors.Is(err, ErrWriterLocked) {
			t.Fatalf("loser err %v, want ErrWriterLocked", err)
		}
	}
	if err := (<-writers).Abort(); err != nil {
		t.Fatal(err)
	}
	w, err := NewWriter(dir, WriterOptions{})
	if err != nil {
		t.Fatalf("lock not released by Abort: %v", err)
	}
	_ = w.Abort()
}

// TestFailedCloseCleansUpAndReleasesLock: a writer that flushed segments
// and then failed must remove them and release the lock in Close.
func TestFailedCloseCleansUpAndReleasesLock(t *testing.T) {
	dir := t.TempDir()
	w, err := NewWriter(dir, WriterOptions{MaxBufferBytes: 64 << 10})
	if err != nil {
		t.Fatal(err)
	}
	g := newFixtureGen()
	for i := range 60 {
		if err := w.Add(context.Background(), g.chunk(i)); err != nil {
			t.Fatal(err)
		}
	}
	if len(segFiles(t, dir)) == 0 {
		t.Fatal("test setup: expected flushed segments")
	}
	bad := g.chunk(99)
	bad.CIK = "x"
	if err := w.Add(context.Background(), bad); err == nil {
		t.Fatal("invalid chunk accepted")
	}
	if err := w.Close(); !errors.Is(err, ErrWriterFailed) {
		t.Fatalf("Close: %v", err)
	}
	if left := segFiles(t, dir); len(left) != 0 {
		t.Fatalf("failed Close left %v", left)
	}
	if _, err := os.Stat(filepath.Join(dir, ManifestName)); err == nil {
		t.Fatal("failed writer published a manifest")
	}
	if err := w.Abort(); err != nil {
		t.Fatalf("Abort after Close: %v", err)
	}
	w2, err := NewWriter(dir, WriterOptions{})
	if err != nil {
		t.Fatalf("lock or files left behind: %v", err)
	}
	_ = w2.Abort()
}

func TestNewWriterErrorReleasesLock(t *testing.T) {
	_, dir := openFixtureIndex(t, WriterOptions{}, 5)
	for range 3 { // a leaked lock would turn later attempts into ErrWriterLocked
		if _, err := NewWriter(dir, WriterOptions{}); !errors.Is(err, ErrIndexExists) {
			t.Fatalf("err %v, want ErrIndexExists", err)
		}
	}
}

// TestCrashLeftoversAreRefusedThenCleaned simulates a process crash after a
// flush: the kernel drops the flock but the segment and a temp file remain.
// A new Writer must refuse the directory rather than overwrite them, and
// CleanAbandoned must make it usable again.
func TestCrashLeftoversAreRefusedThenCleaned(t *testing.T) {
	dir := t.TempDir()
	w, err := NewWriter(dir, WriterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if err := w.Add(context.Background(), newFixtureGen().chunk(0)); err != nil {
		t.Fatal(err)
	}
	if err := w.Flush(); err != nil {
		t.Fatal(err)
	}
	must(t, os.WriteFile(filepath.Join(dir, ".seg-123.tmp"), []byte("partial"), 0o644))
	must(t, w.lock.unlock()) // "crash": lock released, no cleanup
	original, err := os.ReadFile(filepath.Join(dir, segmentFileName(0)))
	if err != nil {
		t.Fatal(err)
	}

	for range 2 {
		_, err := NewWriter(dir, WriterOptions{})
		if !errors.Is(err, ErrAbandonedBuild) || !strings.Contains(err.Error(), "2 file(s)") {
			t.Fatalf("err %v, want ErrAbandonedBuild for 2 files", err)
		}
	}
	if now, _ := os.ReadFile(filepath.Join(dir, segmentFileName(0))); string(now) != string(original) {
		t.Fatal("abandoned segment was modified")
	}
	removed, err := CleanAbandoned(dir)
	if err != nil || len(removed) != 2 {
		t.Fatalf("CleanAbandoned removed %v, err %v", removed, err)
	}
	if _, err := os.Stat(filepath.Join(dir, WriterLockName)); err != nil {
		t.Fatalf("lock file should remain (harmless): %v", err)
	}
	w2, err := NewWriter(dir, WriterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if err := w2.Add(context.Background(), newFixtureGen().chunk(1)); err != nil {
		t.Fatal(err)
	}
	if err := w2.Close(); err != nil {
		t.Fatal(err)
	}
	ix, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer ix.Close()
	if hits, err := ix.Search(context.Background(), "marker0001", 3); err != nil || len(hits) != 1 {
		t.Fatalf("rebuilt index: %v %v", hits, err)
	}
}

// TestFlushNeverOverwritesSegment: a segment name appearing behind the
// writer's back (outside the locking protocol) fails the flush instead of
// replacing a file a reader may have mapped.
func TestFlushNeverOverwritesSegment(t *testing.T) {
	dir := t.TempDir()
	w, err := NewWriter(dir, WriterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if err := w.Add(context.Background(), newFixtureGen().chunk(0)); err != nil {
		t.Fatal(err)
	}
	foreign := filepath.Join(dir, segmentFileName(0))
	must(t, os.WriteFile(foreign, []byte("someone else's segment"), 0o644))
	if err := w.Flush(); !errors.Is(err, ErrAbandonedBuild) {
		t.Fatalf("err %v, want ErrAbandonedBuild", err)
	}
	if got, _ := os.ReadFile(foreign); string(got) != "someone else's segment" {
		t.Fatal("existing segment overwritten")
	}
	if err := w.Close(); !errors.Is(err, ErrWriterFailed) {
		t.Fatalf("Close: %v", err)
	}
	if got, _ := os.ReadFile(foreign); string(got) != "someone else's segment" {
		t.Fatal("cleanup removed a segment this writer did not create")
	}
}
