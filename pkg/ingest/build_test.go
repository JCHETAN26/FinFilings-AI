package ingest

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
	"io"
	"os"
	"path/filepath"
	"testing"
)

// Synthetic transport/index fixtures only; never reported as SEC performance.
func fixture(n int) []byte {
	var b bytes.Buffer
	for i := 0; i < n; i++ {
		c := model.SECChunk{ID: fmt.Sprintf("000032019323000106#item7#passage-%06d", i), Ticker: "AAPL", CIK: "320193", FormType: model.Form10K, FilingYear: 2023, SectionItem: "Item 7", Text: fmt.Sprintf("revenue operations liquidity unique%d", i)}
		if err := json.NewEncoder(&b).Encode(c); err != nil {
			panic(err)
		}
	}
	return b.Bytes()
}

func TestShardsMmapCompatibleAndImmutable(t *testing.T) {
	input := fixture(12)
	output := filepath.Join(t.TempDir(), "built")
	catalog, err := Build(context.Background(), bytes.NewReader(input), output, Options{DocsPerShard: 5, DocsPerSegment: 2, SegmentBytes: 65536})
	if err != nil {
		t.Fatal(err)
	}
	if catalog.Documents != 12 || len(catalog.Shards) != 3 {
		t.Fatal(catalog)
	}
	stored, err := os.ReadFile(filepath.Join(output, "chunks.jsonl"))
	if err != nil || !bytes.Equal(stored, input) {
		t.Fatal("passage spool differs", err)
	}
	segments := 0
	for _, shard := range catalog.Shards {
		segments += shard.Segments
		dir := filepath.Join(output, shard.Path)
		before, err := os.ReadFile(filepath.Join(dir, index.ManifestName))
		if err != nil {
			t.Fatal(err)
		}
		ix, err := index.Open(dir, index.Options{MaxConcurrentSearches: 1})
		if err != nil {
			t.Fatal(err)
		}
		hits, err := ix.SearchFiltered(context.Background(), "revenue", 10, model.FilingFilter{Ticker: "AAPL", Year: 2023, Section: "Item 7"})
		if err != nil || len(hits) != shard.Documents {
			t.Fatal(hits, err)
		}
		if err = ix.Close(); err != nil {
			t.Fatal(err)
		}
		after, err := os.ReadFile(filepath.Join(dir, index.ManifestName))
		if err != nil || !bytes.Equal(before, after) {
			t.Fatal("immutable manifest changed", err)
		}
		writer, err := index.NewWriter(dir, index.WriterOptions{})
		if err == nil {
			writer.Abort()
			t.Fatal("published shard overwritten")
		}
		if !errors.Is(err, index.ErrIndexExists) {
			t.Fatal(err)
		}
	}
	if segments != 7 {
		t.Fatal("segment boundaries not honored", segments)
	}
	if _, err = Build(context.Background(), bytes.NewReader(input), output, Options{}); err == nil {
		t.Fatal("existing root overwritten")
	}
	if _, err = os.Stat(filepath.Join(output, "catalog.json")); err != nil {
		t.Fatal("existing catalog damaged", err)
	}
}

type failingReader struct{}

func (failingReader) Read(p []byte) (int, error) { return 0, io.ErrUnexpectedEOF }
func TestFailureCleanupAndCancellation(t *testing.T) {
	bad := append(fixture(2), []byte("{invalid\n")...)
	for _, source := range []io.Reader{bytes.NewReader(bad), failingReader{}, bytes.NewReader(nil), bytes.NewReader(append(fixture(1), bytes.Repeat([]byte{'x'}, 1<<20)...))} {
		output := filepath.Join(t.TempDir(), "failed")
		if _, err := Build(context.Background(), source, output, Options{DocsPerShard: 2, DocsPerSegment: 1}); err == nil {
			t.Fatal("invalid stream accepted")
		}
		if _, err := os.Stat(output); !os.IsNotExist(err) {
			t.Fatal("failed root remains", err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	output := filepath.Join(t.TempDir(), "cancelled")
	if _, err := Build(ctx, bytes.NewReader(fixture(1)), output, Options{}); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	output = filepath.Join(t.TempDir(), "limit")
	if _, err := Build(context.Background(), bytes.NewReader(fixture(3)), output, Options{DocsPerShard: 1, DocsPerSegment: 1, MaxShards: 2}); err == nil {
		t.Fatal("catalog limit ignored")
	}
}

// Fixture-only durable-build allocation accounting; not SEC throughput.
func BenchmarkBuildFixtures(b *testing.B) {
	data := fixture(12)
	base := b.TempDir()
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		directory := filepath.Join(base, fmt.Sprint(i))
		if _, err := Build(context.Background(), bytes.NewReader(data), directory, Options{DocsPerShard: 5, DocsPerSegment: 2, SegmentBytes: 65536}); err != nil {
			b.Fatal(err)
		}
		if err := os.RemoveAll(directory); err != nil {
			b.Fatal(err)
		}
	}
}
