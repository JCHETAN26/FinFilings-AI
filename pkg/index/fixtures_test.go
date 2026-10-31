package index

import (
	"cmp"
	"context"
	"fmt"
	"math"
	"math/rand/v2"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// Synthetic SEC-like chunks used as TEST FIXTURES ONLY. They are generated
// deterministically from their index, so tests can stream them into a
// Writer without holding the corpus. Their size and term distribution are
// not representative of real EDGAR filings; numbers measured on them are
// fixture numbers, not SEC corpus performance.
const fixtureCount = 1000

type fixtureGen struct {
	vocab []string
}

func newFixtureGen() *fixtureGen {
	base := strings.Fields(`the of and to in for our we by with revenue net income operating
		margin liquidity capital expenditure risk factors competition supply chain regulatory
		litigation goodwill impairment amortization depreciation inventory receivables debt covenant
		interest rate foreign currency exchange tax deferred cash flow dividend repurchase share segment
		cloud semiconductor pharmaceutical clinical trial energy oil gas reserves loan deposit credit
		loss provision lease pension acquisition merger restructuring guidance macroeconomic inflation
		demand customer contract backlog warranty patent intellectual property environmental climate
		emissions workforce employees compensation stock options derivatives hedging commodity
		volatility quarter fiscal year compared increase decrease primarily due growth disruption`)
	vocab := slices.Clone(base)
	for i := range 600 {
		vocab = append(vocab, fmt.Sprintf("fin%03d", i))
	}
	return &fixtureGen{vocab: vocab}
}

func (g *fixtureGen) chunk(i int) model.SECChunk {
	tickers := [...]string{"AAPL", "MSFT", "JPM", "XOM", "JNJ", "NVDA", "AMZN", "PFE", "BAC", "CVX"}
	sections := [...]string{"Item 1", "Item 1A", "Item 2", "Item 7", "Item 7A", "Item 8"}
	forms := [...]model.FormType{model.Form10K, model.Form10Q, model.Form10Q, model.Form10KA}

	r := rand.New(rand.NewPCG(uint64(i), 0x5EC10C))
	zipf := rand.NewZipf(r, 1.1, 1, uint64(len(g.vocab)-1))
	n := 60 + r.IntN(140)
	words := make([]string, 0, n+4)
	for range n {
		w := g.vocab[zipf.Uint64()]
		if r.IntN(20) == 0 {
			w = strings.ToUpper(w[:1]) + w[1:] // exercise case folding
		}
		words = append(words, w)
	}
	marker := fmt.Sprintf("marker%04d", i)
	at := r.IntN(len(words) + 1)
	words = slices.Insert(words, at, marker)
	if i%10 == 0 {
		for range 1 + i%3 {
			words = append(words, "cybersecurity")
		}
	}
	if i%25 == 0 {
		words = append(words, "goodwill", "impairment", "charge")
	}

	ticker := tickers[i%len(tickers)]
	year := 2015 + i%10
	c := model.SECChunk{
		ID:          fmt.Sprintf("FIXTURE-%s-%d-%04d", ticker, year, i),
		Ticker:      ticker,
		CIK:         fmt.Sprintf("%010d", 100000+(i%len(tickers))*7919),
		FormType:    forms[i%len(forms)],
		FilingYear:  year,
		SectionItem: sections[i%len(sections)],
	}
	if i%7 == 0 {
		// Pre-tokenized path: Tokens take precedence and Text must be ignored.
		c.Tokens = words
		c.Text = "ignoredtextsentinel"
	} else {
		c.Text = strings.Join(words, ", ")
	}
	return c
}

// stream sends fixtures [0, n) on a channel, generating them lazily.
func (g *fixtureGen) stream(ctx context.Context, n int) <-chan model.SECChunk {
	ch := make(chan model.SECChunk)
	go func() {
		defer close(ch)
		for i := range n {
			select {
			case ch <- g.chunk(i):
			case <-ctx.Done():
				return
			}
		}
	}()
	return ch
}

func buildFixtureIndex(tb testing.TB, dir string, opts WriterOptions, n int) {
	tb.Helper()
	w, err := NewWriter(dir, opts)
	if err != nil {
		tb.Fatal(err)
	}
	if err := w.Ingest(context.Background(), newFixtureGen().stream(context.Background(), n)); err != nil {
		tb.Fatal(err)
	}
	if err := w.Close(); err != nil {
		tb.Fatal(err)
	}
}

func openFixtureIndex(tb testing.TB, opts WriterOptions, n int) (*Index, string) {
	tb.Helper()
	dir := filepath.Join(tb.TempDir(), "idx")
	buildFixtureIndex(tb, dir, opts, n)
	ix, err := Open(dir, Options{})
	if err != nil {
		tb.Fatal(err)
	}
	tb.Cleanup(func() { _ = ix.Close() })
	return ix, dir
}

// refIndex is a brute-force in-memory BM25 oracle over the fixtures. It
// exists only in tests, to check the disk index's ranking and scores.
type refIndex struct {
	ids    []string
	tfs    []map[string]uint32
	lens   []uint32
	df     map[string]uint64
	tokens uint64
}

func newRefIndex(tb testing.TB, n int) *refIndex {
	tb.Helper()
	g := newFixtureGen()
	ref := &refIndex{df: map[string]uint64{}}
	a := newAnalyzer(64, math.MaxInt64) // oracle: no memory budget
	for i := range n {
		c := g.chunk(i)
		var d analyzedDoc
		if err := a.analyze(&c, &d); err != nil {
			tb.Fatal(err)
		}
		m := make(map[string]uint32, len(d.terms))
		for j, t := range d.terms {
			m[t] = d.tfs[j]
			ref.df[t]++
		}
		ref.ids = append(ref.ids, c.ID)
		ref.tfs = append(ref.tfs, m)
		ref.lens = append(ref.lens, d.length)
		ref.tokens += uint64(d.length)
	}
	return ref
}

type refHit struct {
	id    string
	score float64
	pos   int
}

func (r *refIndex) search(query string, k int, p BM25Params) []refHit {
	tok := newTokenizer(64)
	var terms []string
	for t, pos := tok.next(query, 0); t != nil; t, pos = tok.next(query, pos) {
		if !slices.Contains(terms, string(t)) {
			terms = append(terms, string(t))
		}
	}
	n := uint64(len(r.ids))
	avg := float64(r.tokens) / float64(n)
	var hits []refHit
	for d := range r.ids {
		s, matched := 0.0, false
		for _, t := range terms {
			tf := r.tfs[d][t]
			if tf == 0 {
				continue
			}
			s += bm25Term(bm25IDF(n, r.df[t]), float64(tf), float64(r.lens[d]), avg, p.K1, p.B)
			matched = true
		}
		if matched {
			hits = append(hits, refHit{id: r.ids[d], score: s, pos: d})
		}
	}
	slices.SortFunc(hits, func(a, b refHit) int {
		if c := cmp.Compare(b.score, a.score); c != 0 {
			return c
		}
		return cmp.Compare(a.pos, b.pos)
	})
	return hits[:min(k, len(hits))]
}

func fixtureQueries() []string {
	return []string{
		"revenue growth",
		"cybersecurity risk",
		"goodwill impairment charge",
		"interest rate risk hedging derivatives",
		"Supply-Chain disruption",
		"fin017 fin200 liquidity",
		"marker0042",
		"the and of",
		"FOREIGN currency exchange",
		"fin599 zzzzunknownterm",
	}
}
