package hybrid

import (
	"context"
	"errors"
	"math"
	"net"
	"path/filepath"
	"testing"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

func TestRRFRanksOverlapAndTies(t *testing.T) {
	hits, err := RRF([]Hit{{"a", 100}, {"b", 99}}, []Hit{{"b", -10}, {"c", -20}}, 50)
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) != 3 || hits[0].ID != "b" || hits[1].ID != "a" || hits[2].ID != "c" {
		t.Fatalf("wrong rank fusion: %+v", hits)
	}
	if math.Abs(hits[0].Score-(1.0/62+1.0/61)) > 1e-15 {
		t.Fatalf("not one-based k=60: %+v", hits[0])
	}
	ties, err := RRF([]Hit{{"z", 1}}, []Hit{{"a", 500}}, 50)
	if err != nil || ties[0].ID != "a" {
		t.Fatal("ties are not deterministic", ties, err)
	}
}

func TestRRFRejectsCorruptOrDuplicateHits(t *testing.T) {
	for _, hits := range [][]Hit{{{"a", 1}, {"a", 2}}, {{"", 1}}, {{"a", math.NaN()}}, {{"a", math.Inf(1)}}} {
		if _, err := RRF(hits, nil, 50); err == nil {
			t.Fatalf("accepted invalid hits: %+v", hits)
		}
	}
	if _, err := RRF(nil, nil, 51); err == nil {
		t.Fatal("unbounded candidate limit")
	}
}

func TestRemoteFailureAndCancellationAreExplicit(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "sparse")
	w, err := index.NewWriter(dir, index.WriterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if err = w.Add(context.Background(), model.SECChunk{ID: "unit-test", Ticker: "AAPL", CIK: "320193", FormType: model.Form10K, FilingYear: 2023, SectionItem: "test", Text: "revenue risk"}); err != nil {
		t.Fatal(err)
	}
	if err = w.Close(); err != nil {
		t.Fatal(err)
	}
	ix, err := index.Open(dir, index.Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer ix.Close()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close() // A real closed endpoint; no simulated model response.
	e, err := New(ix, Options{SidecarURL: "http://" + address, MaxConcurrent: 1, Timeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = e.Search(context.Background(), "revenue", false); err == nil {
		t.Fatal("silently fell back to sparse")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err = e.Search(ctx, "revenue", true); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation lost: %v", err)
	}
	if len(e.slots) != 0 {
		t.Fatal("admission slot leaked")
	}
}
