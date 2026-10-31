package ingest

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// Opt-in real SEC corpus replay. No generated filing text enters these metrics.
func TestRealFilingRepeatedIngestion(t *testing.T) {
	path := os.Getenv("FINFILINGS_REAL_CHUNKS")
	if path == "" {
		t.Skip("set FINFILINGS_REAL_CHUNKS to parsed real SEC JSONL")
	}
	base := t.TempDir()
	descriptors := func() int {
		entries, err := os.ReadDir("/proc/self/fd")
		if err != nil {
			t.Fatal(err)
		}
		return len(entries)
	}
	rss := func() uint64 {
		data, err := os.ReadFile("/proc/self/statm")
		if err != nil {
			t.Fatal(err)
		}
		var virtual, resident uint64
		if _, err = fmt.Sscan(string(data), &virtual, &resident); err != nil {
			t.Fatal(err)
		}
		return resident * uint64(os.Getpagesize())
	}
	runtime.GC()
	runtime.GC()
	initialFDs := descriptors()
	baselineGoroutines := runtime.NumGoroutine()
	var observations []map[string]any
	var firstHeap uint64
	var referenceHash string
	for round := 0; round < 6; round++ {
		input, err := os.Open(path)
		if err != nil {
			t.Fatal(err)
		}
		directory := filepath.Join(base, fmt.Sprint(round))
		catalog, err := Build(context.Background(), input, directory, Options{DocsPerShard: 32, DocsPerSegment: 16, SegmentBytes: 1 << 20})
		input.Close()
		if err != nil {
			t.Fatal(err)
		}
		if catalog.Documents < 100 || len(catalog.Shards) < 4 {
			t.Fatal("real fixture insufficient to test sharding", catalog.Documents)
		}
		if round == 0 {
			referenceHash = catalog.CorpusSHA256
		} else if referenceHash != catalog.CorpusSHA256 {
			t.Fatal("nondeterministic corpus fingerprint")
		}
		documents := uint64(0)
		sections := map[string]bool{}
		for _, shard := range catalog.Shards {
			ix, err := index.Open(filepath.Join(directory, shard.Path), index.Options{MaxConcurrentSearches: 1})
			if err != nil {
				t.Fatal(err)
			}
			stats, err := ix.Stats()
			if err != nil {
				ix.Close()
				t.Fatal(err)
			}
			documents += stats.Docs
			for item, query := range map[string]string{"Item 1A": "risk", "Item 7": "revenue", "Item 8": "financial"} {
				hits, err := ix.SearchFiltered(context.Background(), query, 10, model.FilingFilter{Ticker: "MSFT", Year: 2024, Section: item})
				if err != nil {
					ix.Close()
					t.Fatal(err)
				}
				for _, hit := range hits {
					if hit.Chunk.SectionItem != item {
						t.Fatal("wrong section")
					}
					sections[item] = true
				}
			}
			if err = ix.Close(); err != nil {
				t.Fatal(err)
			}
		}
		if int(documents) != catalog.Documents || len(sections) != 3 {
			t.Fatal("mmap retrieval/count verification failed", documents, sections)
		}
		maps, err := os.ReadFile("/proc/self/maps")
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(maps), directory) {
			t.Fatal("segment mapping leaked")
		}
		runtime.GC()
		runtime.GC()
		var memory runtime.MemStats
		runtime.ReadMemStats(&memory)
		if round == 0 {
			firstHeap = memory.HeapAlloc
		}
		if memory.HeapAlloc > firstHeap+(8<<20) {
			t.Fatal("retained heap grew beyond 8 MiB tolerance")
		}
		if descriptors() != initialFDs {
			t.Fatal("file descriptors leaked", initialFDs, descriptors())
		}
		if runtime.NumGoroutine() > baselineGoroutines+1 {
			t.Fatal("goroutines leaked")
		}
		observations = append(observations, map[string]any{"round": round + 1, "documents": catalog.Documents, "shards": len(catalog.Shards), "heap_alloc_bytes": memory.HeapAlloc, "rss_bytes": rss(), "file_descriptors": descriptors(), "goroutines": runtime.NumGoroutine(), "unclosed_mappings": 0})
	}
	report := map[string]any{"real_sec_input": path, "rounds": observations, "corpus_sha256": referenceHash, "scope": "Six finite rebuild/open/search/close/GC rounds; no observed FD, mapping or goroutine leaks; retained heap checked within 8 MiB. Not a proof of indefinite leak freedom."}
	data, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if out := os.Getenv("FINFILINGS_MEMORY_REPORT"); out != "" {
		if err = os.WriteFile(out, append(data, '\n'), 0644); err != nil {
			t.Fatal(err)
		}
	}
	t.Log(string(data))
}
