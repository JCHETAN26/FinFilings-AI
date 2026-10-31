package index

import (
	"context"
	"fmt"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
	"math"
)

// BM25Params are the Okapi BM25 free parameters.
type BM25Params struct {
	K1 float64 // term-frequency saturation, >= 0; default 1.2
	B  float64 // length normalization, in [0, 1]; default 0.75
}

func (p BM25Params) validate() error {
	if math.IsNaN(p.K1) || p.K1 < 0 || math.IsInf(p.K1, 0) || math.IsNaN(p.B) || p.B < 0 || p.B > 1 {
		return fmt.Errorf("%w: BM25 k1=%v b=%v", ErrInvalidArgument, p.K1, p.B)
	}
	return nil
}

// bm25IDF is the Lucene-style non-negative BM25 IDF:
// ln(1 + (N - df + 0.5) / (df + 0.5)).
func bm25IDF(n, df uint64) float64 {
	return math.Log(1 + (float64(n)-float64(df)+0.5)/(float64(df)+0.5))
}

// bm25Term is the BM25 contribution of one query term to one document.
// It is > 0 for idf > 0 and tf >= 1.
func bm25Term(idf, tf, docLen, avgDocLen, k1, b float64) float64 {
	return idf * tf * (k1 + 1) / (tf + k1*(1-b+b*docLen/avgDocLen))
}

// searchScratch is the per-search working set. Index owns a sync.Pool of
// these, so steady-state searches reuse buffers instead of allocating.
// acc is sized for the largest segment, so a scratch costs about 12 bytes
// per document of that segment plus O(query terms x segments).
type searchScratch struct {
	tok     tokenizer
	qbuf    []byte    // concatenated unique query terms
	qoffs   []int     // qbuf[qoffs[i]:qoffs[i+1]] is term i
	idf     []float64 // per query term; 0 if absent from the index
	lookups []int32   // [term*numSegments+seg] term entry index, or -1
	acc     []float64 // per-doc score accumulator; all zero between uses
	touched []uint32  // docs with non-zero acc in the current segment
	top     topK
	it      postingIter
}

func newSearchScratch(maxTermLen int, maxSegDocs uint32) *searchScratch {
	return &searchScratch{
		tok:   newTokenizer(maxTermLen),
		qoffs: make([]int, 0, 16),
		acc:   make([]float64, maxSegDocs),
	}
}

func (sc *searchScratch) numTerms() int { return len(sc.qoffs) - 1 }

func (sc *searchScratch) term(i int) []byte { return sc.qbuf[sc.qoffs[i]:sc.qoffs[i+1]] }

// analyzeQuery tokenizes query into unique terms in first-occurrence order.
func (sc *searchScratch) analyzeQuery(query string, maxTerms int) error {
	sc.qbuf = sc.qbuf[:0]
	sc.qoffs = append(sc.qoffs[:0], 0)
outer:
	for tok, pos := sc.tok.next(query, 0); tok != nil; tok, pos = sc.tok.next(query, pos) {
		for i := range sc.numTerms() {
			if string(sc.term(i)) == string(tok) {
				continue outer
			}
		}
		if sc.numTerms() == maxTerms {
			return fmt.Errorf("%w: query has more than %d unique terms", ErrInvalidArgument, maxTerms)
		}
		sc.qbuf = append(sc.qbuf, tok...)
		sc.qoffs = append(sc.qoffs, len(sc.qbuf))
	}
	return nil
}

// scoreSegment accumulates BM25 scores (term-at-a-time) for every document
// of s matching a query term and offers them to sc.top. sc.acc is restored
// to all zeros on every return path.
func scoreSegment(ctx context.Context, s *segment, segIdx, numSegs int, sc *searchScratch, p BM25Params, avgDocLen float64, filter model.FilingFilter) (err error) {
	acc := sc.acc[:s.docCount]
	touched := sc.touched[:0]
	defer func() {
		if err != nil {
			for _, d := range touched {
				acc[d] = 0
			}
		}
		sc.touched = touched[:0]
	}()
	k1, b := p.K1, p.B
	for t := range sc.numTerms() {
		entry := sc.lookups[t*numSegs+segIdx]
		if entry < 0 {
			continue
		}
		idf := sc.idf[t]
		df, list := s.postingsOf(int(entry))
		sc.it.reset(list, df, s.docCount)
		for blocks := 1; ; blocks++ {
			n, err := sc.it.nextBlock()
			if err != nil {
				return fmt.Errorf("segment %s term %q: %w", s.name, sc.term(t), err)
			}
			if n == 0 {
				break
			}
			for i, d := range sc.it.docs[:n] {
				w := bm25Term(idf, float64(sc.it.tfs[i]), float64(s.docLen(d)), avgDocLen, k1, b)
				if acc[d] == 0 {
					touched = append(touched, d)
				}
				acc[d] += w
			}
			if blocks%64 == 0 {
				if err := ctx.Err(); err != nil {
					return err
				}
			}
		}
	}
	for _, d := range touched {
		if filter != (model.FilingFilter{}) {
			matches, decodeErr := s.matches(d, filter)
			if decodeErr != nil {
				return decodeErr
			}
			if !matches {
				acc[d] = 0
				continue
			}
		}
		sc.top.offer(scoredDoc{score: acc[d], seg: uint32(segIdx), doc: d})
		acc[d] = 0
	}
	return nil
}

type scoredDoc struct {
	score float64
	seg   uint32
	doc   uint32
}

// below reports whether a ranks strictly below b: lower score, then later
// (segment, doc) position. Ranking is therefore a deterministic total order.
func (a scoredDoc) below(b scoredDoc) bool {
	if a.score != b.score {
		return a.score < b.score
	}
	if a.seg != b.seg {
		return a.seg > b.seg
	}
	return a.doc > b.doc
}

// topK is a bounded min-heap holding at most k entries; the root is the
// lowest-ranked retained entry. Its capacity never exceeds Options.MaxTopK.
type topK struct {
	k     int
	items []scoredDoc
}

func (h *topK) reset(k int) {
	h.k = k
	if cap(h.items) < k {
		h.items = make([]scoredDoc, 0, k)
	}
	h.items = h.items[:0]
}

func (h *topK) offer(x scoredDoc) {
	if len(h.items) < h.k {
		h.items = append(h.items, x)
		h.up(len(h.items) - 1)
		return
	}
	if h.items[0].below(x) {
		h.items[0] = x
		h.down(0, len(h.items))
	}
}

func (h *topK) up(i int) {
	for i > 0 {
		p := (i - 1) / 2
		if !h.items[i].below(h.items[p]) {
			return
		}
		h.items[i], h.items[p] = h.items[p], h.items[i]
		i = p
	}
}

func (h *topK) down(i, n int) {
	for {
		l := 2*i + 1
		if l >= n {
			return
		}
		m := l
		if r := l + 1; r < n && h.items[r].below(h.items[l]) {
			m = r
		}
		if !h.items[m].below(h.items[i]) {
			return
		}
		h.items[i], h.items[m] = h.items[m], h.items[i]
		i = m
	}
}

// sortDesc heap-sorts the retained entries in place into descending rank
// order and returns them. The heap must be reset before reuse.
func (h *topK) sortDesc() []scoredDoc {
	for n := len(h.items) - 1; n > 0; n-- {
		h.items[0], h.items[n] = h.items[n], h.items[0]
		h.down(0, n)
	}
	return h.items
}
