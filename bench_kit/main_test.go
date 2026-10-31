package main

import (
	"bytes"
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

func TestMeasuredWorkerPoolAndFailureReporting(t *testing.T) {
	dir := t.TempDir()
	corpus := filepath.Join(dir, "input.jsonl")
	f, err := os.Create(corpus)
	if err != nil {
		t.Fatal(err)
	}
	encoder := json.NewEncoder(f)
	// Explicit unit-test inputs only. The executable has no generator or fixture fallback.
	for i := 0; i < 8; i++ {
		c := model.SECChunk{ID: strconv.Itoa(i), Ticker: "AAPL", CIK: "320193", FormType: model.Form10K, FilingYear: 2023, SectionItem: "test", Text: "revenue growth cash debt risk risk"}
		if err = encoder.Encode(c); err != nil {
			t.Fatal(err)
		}
	}
	if err = f.Close(); err != nil {
		t.Fatal(err)
	}
	c := config{Workers: 50, Requests: 1200, IngestWorkers: 2, BufferMiB: 1, SegmentDocs: 3, TopK: 3, Timeout: time.Second}
	built, err := build(context.Background(), corpus, filepath.Join(dir, "idx"), c)
	if err != nil {
		t.Fatal(err)
	}
	if built.Docs != 8 || built.TextBytes == 0 || built.DocsPerSecond <= 0 {
		t.Fatalf("invalid measured build: %+v", built)
	}
	ix, err := index.Open(filepath.Join(dir, "idx"), index.Options{MaxConcurrentSearches: 50})
	if err != nil {
		t.Fatal(err)
	}
	defer ix.Close()
	qs := []query{{"revenue growth", true}, {"zzabsent", false}}
	baseline := make([][]index.Hit, len(qs))
	for i, q := range qs {
		baseline[i], err = ix.Search(context.Background(), q.Text, 3)
		if err != nil {
			t.Fatal(err)
		}
	}
	var raw bytes.Buffer
	r, err := load(context.Background(), ix, qs, baseline, c, &raw)
	if err != nil {
		t.Fatal(err)
	}
	if r.Completed != 1200 || r.Errors+r.Mismatches+r.UnexpectedEmpty+r.UnexpectedHits != 0 || r.PeakActive > 50 || r.Service.Count != 1200 || r.Counts[0] != 600 || r.Counts[1] != 600 {
		t.Fatalf("incorrect load accounting: %+v", r)
	}
	records, err := csv.NewReader(&raw).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1201 {
		t.Fatalf("CSV rows: %d", len(records))
	}
	seen := make([]bool, 1200)
	for _, row := range records[1:] {
		id, e := strconv.Atoi(row[0])
		if e != nil || id < 0 || id >= 1200 || seen[id] {
			t.Fatalf("invalid/repeated request ID: %v", row)
		}
		seen[id] = true
		ns, e := strconv.ParseInt(row[2], 10, 64)
		if e != nil || ns <= 0 || row[4] != "ok" {
			t.Fatalf("invalid observation: %v", row)
		}
	}
	// A wrong baseline must produce real mismatch observations, not a passing report.
	baseline[0] = nil
	c.Requests = 100
	r, err = load(context.Background(), ix, qs, baseline, c, &bytes.Buffer{})
	if err != nil || r.Mismatches != 50 {
		t.Fatalf("baseline mismatch hidden: %v %+v", err, r)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r, err = load(ctx, ix, qs, baseline, c, &bytes.Buffer{})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled load reported success: %v %+v", err, r)
	}
}

func TestBuildRejectsMalformedInput(t *testing.T) {
	dir := t.TempDir()
	corpus := filepath.Join(dir, "invalid.jsonl")
	if err := os.WriteFile(corpus, []byte("{not valid JSON}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	_, err := build(context.Background(), corpus, filepath.Join(dir, "idx"), config{IngestWorkers: 2, BufferMiB: 1, SegmentDocs: 10})
	if err == nil {
		t.Fatal("malformed corpus accepted")
	}
	if _, err = os.Stat(filepath.Join(dir, "idx", index.ManifestName)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("invalid build published: %v", err)
	}
}

func TestPercentileUnitsAndTails(t *testing.T) {
	values := make([]int64, 100)
	for i := range values {
		values[i] = int64(100-i) * 1000
	}
	d := summarize(values)
	if d.Min != 1 || d.P50 != 50 || d.P95 != 95 || d.P99 != 99 || d.Max != 100 || d.Mean != 50.5 {
		t.Fatalf("wrong nearest-rank microseconds: %+v", d)
	}
}
