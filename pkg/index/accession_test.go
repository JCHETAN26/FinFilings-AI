package index

import (
	"context"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
	"path/filepath"
	"testing"
)

func TestAccessionFilterBothIDLayouts(t *testing.T) {
	const acc = "000032019323000106"
	root := filepath.Join(t.TempDir(), "shards")
	w, err := NewShardedWriter(root, ShardedWriterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Abort()
	ids := []string{acc + "#passage-000001", acc + "#item1A#passage-000001", "000032019324000106#item1A#passage-000001", acc + "0#passage-000001"}
	for i, id := range ids {
		c := model.SECChunk{ID: id, Ticker: "AAPL", CIK: "320193", FormType: model.Form10K, FilingYear: 2023, SectionItem: "Item 1A", Text: "revenue risk"}
		if err := w.Add(context.Background(), ShardKey{2023, 1 + i%2}, c); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	r, err := OpenShardRouter(root, ShardRouterOptions{Workers: 2, MaxConcurrentSearches: 2})
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	hits, err := r.SearchFiltered(context.Background(), "risk", 10, model.FilingFilter{Accession: acc})
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) != 2 {
		t.Fatalf("expected both layouts, got %+v", hits)
	}
	for _, h := range hits {
		if !(model.FilingFilter{Accession: acc}).Matches(h.Chunk) {
			t.Fatal(h)
		}
	}
}
