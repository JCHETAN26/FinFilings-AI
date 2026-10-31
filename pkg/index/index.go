// Package index implements a disk-backed, compressed inverted index with
// BM25 retrieval over SEC filing chunks.
//
// An index directory is built by a Writer, which streams documents into
// bounded in-memory buffers and flushes immutable segment files (see
// segment.go for the format), then publishes a manifest (manifest.go). An
// Index opens the manifest, memory-maps every segment read-only, validates
// it, and serves concurrent BM25 searches across all segments with global
// collection statistics.
package index

import (
	"context"
	"errors"
	"fmt"
	"hash/crc32"
	"path/filepath"
	"runtime"
	"sync"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// Options configures an Index. Zero values select the defaults.
type Options struct {
	BM25          BM25Params // default {K1: 1.2, B: 0.75} when both are zero
	MaxTopK       int        // largest k accepted by Search; default 1000, max 65536
	MaxQueryTerms int        // largest unique query term count; default 64, max 4096
	// MaxConcurrentSearches is the number of search slots. Each admitted
	// search holds one slot and one scoring scratch (about 12 bytes per doc
	// of the largest segment), so this bounds peak scoring memory. Further
	// callers wait for a slot, honoring their context and Close.
	// Default runtime.GOMAXPROCS(0), max 4096.
	MaxConcurrentSearches int
}

func (o Options) withDefaults() (Options, error) {
	if o.MaxConcurrentSearches == 0 {
		o.MaxConcurrentSearches = runtime.GOMAXPROCS(0)
	}
	if o.MaxConcurrentSearches < 1 || o.MaxConcurrentSearches > 4096 {
		return o, fmt.Errorf("%w: MaxConcurrentSearches %d", ErrInvalidArgument, o.MaxConcurrentSearches)
	}
	if o.BM25 == (BM25Params{}) {
		o.BM25 = BM25Params{K1: 1.2, B: 0.75}
	}
	if o.MaxTopK == 0 {
		o.MaxTopK = 1000
	}
	if o.MaxQueryTerms == 0 {
		o.MaxQueryTerms = 64
	}
	if err := o.BM25.validate(); err != nil {
		return o, err
	}
	if o.MaxTopK < 1 || o.MaxTopK > 1<<16 {
		return o, fmt.Errorf("%w: MaxTopK %d", ErrInvalidArgument, o.MaxTopK)
	}
	if o.MaxQueryTerms < 1 || o.MaxQueryTerms > 4096 {
		return o, fmt.Errorf("%w: MaxQueryTerms %d", ErrInvalidArgument, o.MaxQueryTerms)
	}
	return o, nil
}

// Hit is one search result. All fields are copied out of the segment
// mappings and remain valid after the Index is closed.
type Hit struct {
	Score float64
	Chunk model.ChunkMeta
}

// Stats describes an open index.
type Stats struct {
	Segments     int
	Docs         uint64
	Tokens       uint64 // sum of indexed document lengths
	Terms        uint64 // sum of per-segment dictionary sizes
	Postings     uint64 // sum of document frequencies (one per term-doc pair)
	PostingBytes uint64 // compressed size of all posting lists
	FileBytes    uint64 // total size of all segment files
}

// RawPostingBytes is the uncompressed baseline: one uint32 doc ID plus one
// uint32 term frequency per posting.
func (s Stats) RawPostingBytes() uint64 { return 8 * s.Postings }

// Index is an open, read-only, memory-mapped index.
//
// Concurrency and lifecycle: Search and Stats are safe for concurrent use.
//
//   - Admission: Search first takes one of MaxConcurrentSearches slots (a
//     buffered channel). While waiting it returns ctx.Err() if its context
//     ends, or ErrClosed if Close is called. At most MaxConcurrentSearches
//     scoring scratches are in use at once.
//   - Mapping safety: an admitted Search (and Stats) holds the read lock for
//     its full duration, so the mappings it reads cannot be unmapped
//     underneath it.
//   - Close first closes the done channel, which wakes every queued caller
//     (they return ErrClosed without touching segments), then takes the
//     write lock, which waits for admitted searches to finish, then unmaps
//     every segment. Queued callers never hold the lock and admitted
//     searches never wait on Close, so Close cannot deadlock with them.
//     Close is idempotent; later calls return ErrClosed.
type Index struct {
	mu         sync.RWMutex
	closed     bool
	segs       []*segment
	docs       uint64
	tokens     uint64
	maxSegDocs uint32
	opts       Options
	scratch    sync.Pool     // *searchScratch; in-use count <= cap(slots)
	slots      chan struct{} // search admission semaphore
	done       chan struct{} // closed by the first Close
	closeOnce  sync.Once
	hooks      searchHooks
}

// searchHooks are test-only observation points; nil in production.
type searchHooks struct {
	admitted func() // called after a slot is taken
	released func() // called just before the slot is returned
}

// admit takes a search slot, or fails if ctx ends or the Index closes
// first.
func (ix *Index) admit(ctx context.Context) error {
	select {
	case <-ix.done:
		return ErrClosed
	default:
	}
	select {
	case ix.slots <- struct{}{}:
	case <-ctx.Done():
		return ctx.Err()
	case <-ix.done:
		return ErrClosed
	}
	if ix.hooks.admitted != nil {
		ix.hooks.admitted()
	}
	return nil
}

func (ix *Index) release() {
	if ix.hooks.released != nil {
		ix.hooks.released()
	}
	<-ix.slots
}

// Open opens the index published in dir. Every segment is mapped and fully
// structurally validated (including its CRC-32C); any failure closes the
// mappings opened so far and returns an error wrapping ErrCorrupt or the
// underlying I/O error.
func Open(dir string, opts Options) (*Index, error) {
	opts, err := opts.withDefaults()
	if err != nil {
		return nil, err
	}
	tab := crc32.MakeTable(crc32.Castagnoli)
	m, err := readManifest(dir, tab)
	if err != nil {
		return nil, err
	}
	ix := &Index{
		opts:  opts,
		slots: make(chan struct{}, opts.MaxConcurrentSearches),
		done:  make(chan struct{}),
	}
	for _, name := range m.segments {
		mf, err := mapFile(filepath.Join(dir, name))
		if err != nil {
			return nil, errors.Join(err, ix.unmapAll())
		}
		s, err := parseSegment(name, mf.data, tab)
		if err != nil {
			return nil, errors.Join(err, mf.unmap(), ix.unmapAll())
		}
		s.file = mf
		ix.segs = append(ix.segs, s)
		ix.docs += uint64(s.docCount)
		ix.tokens += s.totalTokens
		ix.maxSegDocs = max(ix.maxSegDocs, s.docCount)
	}
	maxTermLen, maxSegDocs := m.maxTermLen, ix.maxSegDocs
	ix.scratch.New = func() any { return newSearchScratch(maxTermLen, maxSegDocs) }
	return ix, nil
}

// Search returns up to k documents ranked by BM25 against query, highest
// score first, ties broken by index order. The query is analyzed like
// document text and duplicate terms are ignored. k must be in
// [1, Options.MaxTopK]. A query with no indexable terms returns no hits.
// A posting list that fails to decode yields an error wrapping ErrCorrupt.
// Search waits for a free slot (see Index); while waiting it returns
// ctx.Err() or ErrClosed.
func (ix *Index) Search(ctx context.Context, query string, k int) ([]Hit, error) {
	return ix.SearchFiltered(ctx, query, k, model.FilingFilter{})
}

// SearchFiltered applies metadata constraints before top-k admission; corpus IDF is unchanged.
func (ix *Index) SearchFiltered(ctx context.Context, query string, k int, filter model.FilingFilter) ([]Hit, error) {
	if err := filter.Validate(); err != nil {
		return nil, err
	}
	if k < 1 || k > ix.opts.MaxTopK {
		return nil, fmt.Errorf("%w: k=%d outside [1, %d]", ErrInvalidArgument, k, ix.opts.MaxTopK)
	}
	if err := ix.admit(ctx); err != nil {
		return nil, err
	}
	defer ix.release()
	ix.mu.RLock()
	defer ix.mu.RUnlock()
	if ix.closed {
		return nil, ErrClosed
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	sc := ix.scratch.Get().(*searchScratch)
	defer ix.scratch.Put(sc)

	if err := sc.analyzeQuery(query, ix.opts.MaxQueryTerms); err != nil {
		return nil, err
	}
	nt, ns := sc.numTerms(), len(ix.segs)
	if nt == 0 || ix.docs == 0 {
		return nil, nil
	}
	sc.lookups = sc.lookups[:0]
	sc.idf = sc.idf[:0]
	for t := range nt {
		var df uint64
		for _, s := range ix.segs {
			i, ok := s.lookup(sc.term(t))
			if !ok {
				sc.lookups = append(sc.lookups, -1)
				continue
			}
			sc.lookups = append(sc.lookups, int32(i))
			d, _ := s.postingsOf(i)
			df += uint64(d)
		}
		idf := 0.0
		if df > 0 {
			idf = bm25IDF(ix.docs, df)
		}
		sc.idf = append(sc.idf, idf)
	}
	avgDocLen := float64(ix.tokens) / float64(ix.docs)
	sc.top.reset(k)
	for si, s := range ix.segs {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if err := scoreSegment(ctx, s, si, ns, sc, ix.opts.BM25, avgDocLen, filter); err != nil {
			return nil, err
		}
	}
	ranked := sc.top.sortDesc()
	if len(ranked) == 0 {
		return nil, nil
	}
	hits := make([]Hit, len(ranked))
	for i, r := range ranked {
		meta, err := ix.segs[r.seg].meta(r.doc)
		if err != nil {
			return nil, err
		}
		hits[i] = Hit{Score: r.score, Chunk: meta}
	}
	return hits, nil
}

// Stats scans the term dictionaries and returns index statistics.
func (ix *Index) Stats() (Stats, error) {
	ix.mu.RLock()
	defer ix.mu.RUnlock()
	if ix.closed {
		return Stats{}, ErrClosed
	}
	st := Stats{Segments: len(ix.segs), Docs: ix.docs, Tokens: ix.tokens}
	for _, s := range ix.segs {
		st.Terms += uint64(s.termCount)
		st.PostingBytes += uint64(len(s.postings))
		st.FileBytes += uint64(len(s.file.data))
		for i := range int(s.termCount) {
			df, _ := s.postingsOf(i)
			st.Postings += uint64(df)
		}
	}
	return st, nil
}

// Close waits for in-flight searches, then unmaps all segments. It is
// idempotent; later Search and Stats calls return ErrClosed.
func (ix *Index) Close() error {
	ix.closeOnce.Do(func() { close(ix.done) }) // wake queued callers first
	ix.mu.Lock()
	defer ix.mu.Unlock()
	if ix.closed {
		return nil
	}
	ix.closed = true
	return ix.unmapAll()
}

func (ix *Index) unmapAll() error {
	var errs []error
	for _, s := range ix.segs {
		errs = append(errs, s.file.unmap())
	}
	ix.segs = nil
	return errors.Join(errs...)
}
