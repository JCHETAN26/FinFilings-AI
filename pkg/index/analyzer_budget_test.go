package index

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

func distinctTermsText(n int) string {
	var sb strings.Builder
	for i := range n {
		fmt.Fprintf(&sb, "term%d ", i)
	}
	return sb.String()
}

// TestAnalyzerRejectsManyDistinctTermsEarly is the regression for unbounded
// per-document analysis memory: a document with 200,000 distinct terms must
// be rejected while tokenizing, after materializing only as many terms as
// the budget allows, not after building all 200,000.
func TestAnalyzerRejectsManyDistinctTermsEarly(t *testing.T) {
	const budget = 1 << 20
	c := newFixtureGen().chunk(1)
	c.Tokens, c.Text = nil, distinctTermsText(200_000)
	a := newAnalyzer(64, budget)
	d := new(analyzedDoc)
	err := a.analyze(&c, d)
	if !errors.Is(err, ErrDocTooLarge) {
		t.Fatalf("err %v, want ErrDocTooLarge", err)
	}
	maxTerms := budget / analyzerTermBytes
	if len(d.terms) == 0 || len(d.terms) > maxTerms {
		t.Fatalf("materialized %d terms before rejecting; budget allows at most %d", len(d.terms), maxTerms)
	}
	t.Logf("rejected after %d of 200000 distinct terms (budget %d B)", len(d.terms), budget)
	if len(a.idx) != 0 || a.used != 0 {
		t.Fatalf("analyzer not reset after rejection: %d map entries, used %d", len(a.idx), a.used)
	}

	// The same through the pre-split Tokens path.
	c.Text, c.Tokens = "", strings.Fields(distinctTermsText(200_000))
	d = new(analyzedDoc)
	if err := a.analyze(&c, d); !errors.Is(err, ErrDocTooLarge) || len(d.terms) > maxTerms {
		t.Fatalf("Tokens path: err %v after %d terms", err, len(d.terms))
	}
}

// TestAnalyzerBudgetIsPerDistinctTerm checks that long documents with few
// distinct terms are not rejected: the budget tracks memory, not length.
func TestAnalyzerBudgetIsPerDistinctTerm(t *testing.T) {
	c := newFixtureGen().chunk(1)
	c.Tokens, c.Text = nil, strings.Repeat("revenue growth ", 200_000)
	a := newAnalyzer(64, 16<<10)
	d := new(analyzedDoc)
	if err := a.analyze(&c, d); err != nil {
		t.Fatal(err)
	}
	if len(d.terms) != 2 || d.tfs[0] != 200_000 || d.length != 400_000 {
		t.Fatalf("terms %v tfs %v length %d", d.terms, d.tfs, d.length)
	}
}

// TestAnalyzerChargeCoversCommitCost: any document that passes analysis
// must fit an empty segment buffer, so the analyzer's charge has to be at
// least memSegment.cost for the same document.
func TestAnalyzerChargeCoversCommitCost(t *testing.T) {
	g := newFixtureGen()
	a := newAnalyzer(64, 1<<30)
	for i := range 200 {
		c := g.chunk(i)
		d := new(analyzedDoc)
		if err := a.analyze(&c, d); err != nil {
			t.Fatal(err)
		}
		charge := int64(docOverheadBytes + len(d.stored))
		for _, term := range d.terms {
			charge += int64(analyzerTermBytes + 2*len(term))
		}
		if commit := newMemSegment().cost(d); commit > charge {
			t.Fatalf("doc %d: commit cost %d exceeds analyzer charge %d", i, commit, charge)
		}
	}
}

func TestWriterRejectsManyDistinctTerms(t *testing.T) {
	g := newFixtureGen()
	big := g.chunk(5)
	big.Tokens, big.Text = nil, distinctTermsText(100_000)

	t.Run("Add", func(t *testing.T) {
		w, err := NewWriter(t.TempDir(), WriterOptions{MaxBufferBytes: 1 << 20})
		if err != nil {
			t.Fatal(err)
		}
		defer w.Abort()
		if err := w.Add(context.Background(), g.chunk(1)); err != nil {
			t.Fatal(err)
		}
		before := w.buf.bytes
		if err := w.Add(context.Background(), big); !errors.Is(err, ErrDocTooLarge) {
			t.Fatalf("err %v, want ErrDocTooLarge", err)
		}
		if w.buf.docs() != 1 || w.buf.bytes != before {
			t.Fatalf("oversized doc partially committed: docs %d bytes %d->%d", w.buf.docs(), before, w.buf.bytes)
		}
	})
	t.Run("Ingest", func(t *testing.T) {
		w, err := NewWriter(t.TempDir(), WriterOptions{MaxBufferBytes: 1 << 20, Workers: 4})
		if err != nil {
			t.Fatal(err)
		}
		defer w.Abort()
		src := make(chan model.SECChunk, 3)
		src <- g.chunk(1)
		src <- big
		src <- g.chunk(2)
		close(src)
		if err := w.Ingest(context.Background(), src); !errors.Is(err, ErrDocTooLarge) {
			t.Fatalf("err %v, want ErrDocTooLarge", err)
		}
	})
}

// TestPooledStorageIsBounded checks that one large document does not leave
// oversized buffers in the Writer's pools.
func TestPooledStorageIsBounded(t *testing.T) {
	w, err := NewWriter(t.TempDir(), WriterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Abort()
	c := newFixtureGen().chunk(1)
	c.Tokens, c.Text = nil, distinctTermsText(2*maxPooledDocTerms)
	a := newAnalyzer(64, 1<<30)
	mapBefore := fmt.Sprintf("%p", a.idx)
	d := new(analyzedDoc)
	if err := a.analyze(&c, d); err != nil {
		t.Fatal(err)
	}
	if fmt.Sprintf("%p", a.idx) == mapBefore {
		t.Fatal("analyzer kept a map that grew past maxPooledDocTerms")
	}
	if cap(d.terms) <= maxPooledDocTerms {
		t.Fatalf("test setup: cap(terms) %d", cap(d.terms))
	}
	w.releaseDoc(d)
	if d.terms != nil || d.tfs != nil {
		t.Fatalf("releaseDoc pooled oversized arrays: cap %d/%d", cap(d.terms), cap(d.tfs))
	}
	small := &analyzedDoc{terms: make([]string, 3, 8), tfs: make([]uint32, 3, 8)}
	w.releaseDoc(small)
	if cap(small.terms) != 8 || len(small.terms) != 0 {
		t.Fatal("releaseDoc dropped small reusable arrays")
	}
}
