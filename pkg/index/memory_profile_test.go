package index

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// TestFixtureMemoryFootprint is an opt-in diagnostic, not a capacity or
// performance assertion. RSS includes the Go runtime, fixture construction,
// and shared pages; heap accounting excludes mmap pages. Run in isolation:
// FINFILINGS_MEMORY_PROFILE=1 go test -count=1 -run '^TestFixtureMemoryFootprint$' -v ./pkg/index
func TestFixtureMemoryFootprint(t *testing.T) {
	if os.Getenv("FINFILINGS_MEMORY_PROFILE") != "1" {
		t.Skip("opt-in fixture memory diagnostic")
	}
	dir := filepath.Join(t.TempDir(), "idx")
	buildFixtureIndex(t, dir, WriterOptions{}, fixtureCount)
	runtime.GC()
	sampleFixtureMemory(t, "after_fixture_build_before_open")
	ix, err := Open(dir, Options{MaxConcurrentSearches: 2})
	if err != nil {
		t.Fatal(err)
	}
	defer ix.Close()
	st, err := ix.Stats()
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("SYNTHETIC FIXTURE ONLY: docs=%d segments=%d mapped_file_bytes=%d search_slots=2", st.Docs, st.Segments, st.FileBytes)
	sampleFixtureMemory(t, "after_mmap_open")
	for range 100 {
		if _, err := ix.Search(context.Background(), "revenue growth", 10); err != nil {
			t.Fatal(err)
		}
	}
	// Do not GC here: it may evict sync.Pool scratch being measured.
	sampleFixtureMemory(t, "after_100_sequential_searches_no_forced_gc")
	if err := ix.Close(); err != nil {
		t.Fatal(err)
	}
	runtime.GC()
	sampleFixtureMemory(t, "after_close_and_gc")
}

func sampleFixtureMemory(t *testing.T, stage string) {
	t.Helper()
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	t.Logf("%s heap_alloc_bytes=%d heap_inuse_bytes=%d total_alloc_bytes=%d num_gc=%d", stage, m.HeapAlloc, m.HeapInuse, m.TotalAlloc, m.NumGC)
	status, err := os.ReadFile("/proc/self/status")
	if err != nil {
		t.Logf("process RSS unavailable on this platform: %v", err)
		return
	}
	for _, line := range strings.Split(string(status), "\n") {
		if strings.HasPrefix(line, "VmRSS:") || strings.HasPrefix(line, "VmHWM:") {
			t.Logf("%s %s", stage, line)
		}
	}
}
