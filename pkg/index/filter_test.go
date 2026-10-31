package index

import (
	"context"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
	"testing"
)

func TestFilterBeforeTopKAndRetainsGlobalScores(t *testing.T) {
	ix, _ := openFixtureIndex(t, WriterOptions{}, 1000)
	all, err := ix.Search(context.Background(), "revenue risk", 1000)
	if err != nil {
		t.Fatal(err)
	}
	filter := model.FilingFilter{Ticker: "NVDA", Year: 2020, Section: "Item 7"}
	// Derive exact expected ranking from the unfiltered scored collection.
	var expected []Hit
	for _, h := range all {
		if filter.Matches(h.Chunk) {
			expected = append(expected, h)
		}
	}
	if len(expected) < 2 {
		t.Fatal("fixture needs matching documents")
	}
	got, err := ix.SearchFiltered(context.Background(), "revenue risk", 2, filter)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("got %d filtered results", len(got))
	}
	for i, h := range got {
		if h != expected[i] {
			t.Fatalf("filter ranking mismatch: %+v != %+v", h, expected[i])
		}
	}
	absent, err := ix.SearchFiltered(context.Background(), "revenue", 2, model.FilingFilter{Ticker: "ABSENT"})
	if err != nil || len(absent) != 0 {
		t.Fatal(absent, err)
	}
	if _, err := ix.SearchFiltered(context.Background(), "revenue", 2, model.FilingFilter{Year: 1}); err == nil {
		t.Fatal("accepted invalid year")
	}
}

func TestMappedFilterMatchesMetadataWithoutAllocating(t *testing.T) {
	ix, _ := openFixtureIndex(t, WriterOptions{}, 100)
	filter := model.FilingFilter{Ticker: "NVDA", Year: 2020, Section: "Item 7"}
	for _, segment := range ix.segs {
		for doc := uint32(0); doc < segment.docCount; doc++ {
			meta, err := segment.meta(doc)
			if err != nil {
				t.Fatal(err)
			}
			for _, f := range []model.FilingFilter{filter, {}, {Accession: "000032019323000106"}, {Ticker: meta.Ticker, Year: meta.FilingYear, Section: meta.SectionItem}} {
				matched, err := segment.matches(doc, f)
				if err != nil || matched != f.Matches(meta) {
					t.Fatal(meta, f, matched, err)
				}
			}
		}
	}
	segment := ix.segs[0]
	if allocs := testing.AllocsPerRun(100, func() { _, _ = segment.matches(0, filter) }); allocs != 0 {
		t.Fatalf("filter allocates %v per candidate", allocs)
	}
}

// Synthetic fixtures only: report allocation cost, not real SEC quality/latency.
func BenchmarkFilteredSearchFixtures(b *testing.B) {
	ix, _ := openFixtureIndex(b, WriterOptions{}, 1000)
	ctx := context.Background()
	filter := model.FilingFilter{Ticker: "NVDA", Year: 2020, Section: "Item 7"}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := ix.SearchFiltered(ctx, "revenue risk", 10, filter); err != nil {
			b.Fatal(err)
		}
	}
}
