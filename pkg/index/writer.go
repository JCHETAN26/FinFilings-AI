package index

import (
	"context"
	"errors"
	"fmt"
	"hash/crc32"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"sync"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// WriterOptions configures a Writer. Zero values select the defaults.
type WriterOptions struct {
	// MaxBufferBytes bounds the accounted size of the in-memory segment
	// buffer (postings, term keys, stored fields). Before a document would
	// push the buffer past the limit, the buffer is flushed to a new segment
	// file. A single document whose own cost exceeds the limit fails with
	// ErrDocTooLarge. Default 64 MiB, minimum 4 KiB. See memSegment for
	// exactly what is accounted.
	MaxBufferBytes int64
	// MaxDocsPerSegment caps documents per segment. It also bounds the
	// per-search scoring buffers (12 bytes per doc of the largest segment).
	// Default 1<<20, maximum MaxDocsPerSegmentLimit.
	MaxDocsPerSegment int
	// Workers is the number of analysis goroutines used by Ingest.
	// Default runtime.GOMAXPROCS(0).
	Workers int
	// QueueDepth is the capacity of the Ingest job channel. Default 2*Workers.
	QueueDepth int
	// MaxTermLen drops tokens longer than this many bytes. It is recorded in
	// the manifest so queries use the same analyzer. Default 64.
	MaxTermLen int
}

func (o WriterOptions) withDefaults() (WriterOptions, error) {
	if o.MaxBufferBytes == 0 {
		o.MaxBufferBytes = 64 << 20
	}
	if o.MaxDocsPerSegment == 0 {
		o.MaxDocsPerSegment = 1 << 20
	}
	if o.Workers == 0 {
		o.Workers = runtime.GOMAXPROCS(0)
	}
	if o.QueueDepth == 0 {
		o.QueueDepth = 2 * o.Workers
	}
	if o.MaxTermLen == 0 {
		o.MaxTermLen = 64
	}
	switch {
	case o.MaxBufferBytes < 4<<10:
		return o, fmt.Errorf("%w: MaxBufferBytes %d < 4096", ErrInvalidArgument, o.MaxBufferBytes)
	case o.MaxDocsPerSegment < 1 || o.MaxDocsPerSegment > MaxDocsPerSegmentLimit:
		return o, fmt.Errorf("%w: MaxDocsPerSegment %d", ErrInvalidArgument, o.MaxDocsPerSegment)
	case o.Workers < 1 || o.Workers > 1024:
		return o, fmt.Errorf("%w: Workers %d", ErrInvalidArgument, o.Workers)
	case o.QueueDepth < 1 || o.QueueDepth > 1<<16:
		return o, fmt.Errorf("%w: QueueDepth %d", ErrInvalidArgument, o.QueueDepth)
	case o.MaxTermLen < 1 || o.MaxTermLen > MaxTermLenLimit:
		return o, fmt.Errorf("%w: MaxTermLen %d", ErrInvalidArgument, o.MaxTermLen)
	}
	return o, nil
}

// Writer builds an index directory by streaming documents into bounded
// in-memory buffers and flushing them as immutable segment files. Nothing is
// visible to readers until Close publishes the manifest.
//
// Writer methods are serialized by an internal mutex; concurrent calls are
// safe but do not run in parallel. Parallelism comes from Ingest's worker
// pool. Any error from Add, Ingest, or Flush (including context
// cancellation of Ingest) is sticky: later calls return ErrWriterFailed and
// Close does not publish. Duplicate chunk IDs are not detected, since that
// would require retaining every ID in memory.
//
// A Writer holds the directory's exclusive writer lock (WriterLockName) from
// NewWriter until Close or Abort returns, including when Close fails; a
// Writer that is neither closed nor aborted keeps the lock for the life of
// the process.
type Writer struct {
	mu       sync.Mutex
	dir      string
	opts     WriterOptions
	tab      *crc32.Table
	lock     *dirLock
	buf      *memSegment
	segments []string
	err      error
	closed   bool

	docPool      sync.Pool // *analyzedDoc, reused between analysis and commit
	analyzerPool sync.Pool // *analyzer, one held per active worker
	flushSc      flushScratch
}

// WriterLockName is the lock file NewWriter locks inside the index
// directory. Index readers ignore it.
const WriterLockName = ".writer.lock"

// NewWriter prepares dir (created if needed) for a new index. It acquires
// the directory's exclusive writer lock and then fails with ErrIndexExists
// if dir already contains a manifest, or ErrAbandonedBuild if it contains
// segment or temporary files from an unpublished build; segment files are
// never overwritten. On any error the lock is released.
func NewWriter(dir string, opts WriterOptions) (*Writer, error) {
	opts, err := opts.withDefaults()
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("index: create dir: %w", err)
	}
	lock, err := lockDir(dir)
	if err != nil {
		return nil, err
	}
	if err := checkFreshDir(dir); err != nil {
		return nil, errors.Join(err, lock.unlock())
	}
	w := &Writer{
		dir:  dir,
		opts: opts,
		tab:  crc32.MakeTable(crc32.Castagnoli),
		lock: lock,
		buf:  newMemSegment(),
	}
	w.docPool.New = func() any { return new(analyzedDoc) }
	w.analyzerPool.New = func() any { return newAnalyzer(opts.MaxTermLen, opts.MaxBufferBytes) }
	return w, nil
}

// checkFreshDir requires that dir holds no published index and no files
// from an abandoned build. The caller must hold the writer lock.
func checkFreshDir(dir string) error {
	if _, err := os.Stat(filepath.Join(dir, ManifestName)); err == nil {
		return fmt.Errorf("%w: %s", ErrIndexExists, dir)
	} else if !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("index: stat manifest: %w", err)
	}
	leftovers, err := abandonedFiles(dir)
	if err != nil {
		return err
	}
	if len(leftovers) > 0 {
		return fmt.Errorf("%w: %s has %d file(s), e.g. %s", ErrAbandonedBuild, dir, len(leftovers), leftovers[0])
	}
	return nil
}

// abandonedFiles lists files a Writer may create before publishing: segment
// files and segment/manifest temporaries. Other files are ignored.
func abandonedFiles(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("index: read dir: %w", err)
	}
	var out []string
	for _, e := range entries {
		n := e.Name()
		tmp := strings.HasSuffix(n, ".tmp") && (strings.HasPrefix(n, ".seg-") || strings.HasPrefix(n, ".manifest-"))
		if tmp || validSegmentName(n) {
			out = append(out, n)
		}
	}
	return out, nil
}

// CleanAbandoned removes the files of an unpublished build from dir (see
// ErrAbandonedBuild) so a new Writer can use it. It takes the writer lock,
// so it fails with ErrWriterLocked while a Writer is active, and it refuses
// (ErrIndexExists) to touch a directory with a published manifest. It
// returns the removed file names.
func CleanAbandoned(dir string) (removed []string, err error) {
	lock, err := lockDir(dir)
	if err != nil {
		return nil, err
	}
	defer func() { err = errors.Join(err, lock.unlock()) }()
	if _, err := os.Stat(filepath.Join(dir, ManifestName)); err == nil {
		return nil, fmt.Errorf("%w: %s", ErrIndexExists, dir)
	} else if !errors.Is(err, fs.ErrNotExist) {
		return nil, fmt.Errorf("index: stat manifest: %w", err)
	}
	files, err := abandonedFiles(dir)
	if err != nil {
		return nil, err
	}
	for _, f := range files {
		if err := os.Remove(filepath.Join(dir, f)); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return removed, fmt.Errorf("index: remove abandoned file: %w", err)
		}
		removed = append(removed, f)
	}
	return removed, syncDir(dir)
}

// Add analyzes and buffers one chunk on the calling goroutine.
func (w *Writer) Add(ctx context.Context, c model.SECChunk) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if err := w.usable(); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	a := w.analyzerPool.Get().(*analyzer)
	d := w.docPool.Get().(*analyzedDoc)
	err := a.analyze(&c, d)
	w.analyzerPool.Put(a)
	if err == nil {
		err = w.commit(d)
	}
	w.releaseDoc(d)
	if err != nil {
		w.err = err
	}
	return err
}

// Ingest consumes src until it is closed, analyzing chunks on a bounded
// worker pool and committing them in arrival order, so the resulting index
// is deterministic for a given input order and options. At most
// Workers+QueueDepth analyzed documents are in flight at once. It returns
// when src is closed and drained, or on the first error or cancellation.
func (w *Writer) Ingest(ctx context.Context, src <-chan model.SECChunk) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if err := w.usable(); err != nil {
		return err
	}
	if err := w.ingest(ctx, src); err != nil {
		w.err = err
		return err
	}
	return nil
}

type ingestJob struct {
	seq   uint64
	chunk model.SECChunk
}

type dispatchResult struct {
	count uint64
	err   error
}

func (w *Writer) ingest(parent context.Context, src <-chan model.SECChunk) error {
	ctx, cancel := context.WithCancelCause(parent)
	defer cancel(nil)

	window := w.opts.Workers + w.opts.QueueDepth
	jobs := make(chan ingestJob, w.opts.QueueDepth)
	results := make(chan *analyzedDoc, window)
	// inflight bounds dispatched-but-uncommitted documents, which bounds the
	// reorder buffer below to window entries.
	inflight := make(chan struct{}, window)
	dispatched := make(chan dispatchResult, 1)

	go func() {
		defer close(jobs)
		var seq uint64
		for {
			var c model.SECChunk
			var ok bool
			select {
			case <-ctx.Done():
				dispatched <- dispatchResult{seq, context.Cause(ctx)}
				return
			case c, ok = <-src:
			}
			if !ok {
				dispatched <- dispatchResult{seq, nil}
				return
			}
			select {
			case inflight <- struct{}{}:
			case <-ctx.Done():
				dispatched <- dispatchResult{seq, context.Cause(ctx)}
				return
			}
			select {
			case jobs <- ingestJob{seq: seq, chunk: c}:
				seq++
			case <-ctx.Done():
				dispatched <- dispatchResult{seq, context.Cause(ctx)}
				return
			}
		}
	}()

	var wg sync.WaitGroup
	for range w.opts.Workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			a := w.analyzerPool.Get().(*analyzer)
			defer w.analyzerPool.Put(a)
			for job := range jobs {
				d := w.docPool.Get().(*analyzedDoc)
				d.seq = job.seq
				d.err = a.analyze(&job.chunk, d)
				select {
				case results <- d:
				case <-ctx.Done():
					w.releaseDoc(d)
				}
			}
		}()
	}
	go func() {
		wg.Wait()
		close(results)
	}()

	pending := make(map[uint64]*analyzedDoc, window)
	var next uint64
	var firstErr error
	for d := range results {
		if firstErr != nil {
			w.releaseDoc(d)
			continue
		}
		if d.err != nil {
			firstErr = d.err
			w.releaseDoc(d)
			cancel(firstErr)
			continue
		}
		pending[d.seq] = d
		for {
			pd, ok := pending[next]
			if !ok {
				break
			}
			delete(pending, next)
			err := w.commit(pd)
			w.releaseDoc(pd)
			<-inflight
			next++
			if err != nil {
				firstErr = err
				cancel(err)
				break
			}
		}
	}
	for _, pd := range pending {
		w.releaseDoc(pd)
	}
	dr := <-dispatched
	switch {
	case firstErr != nil:
		return firstErr
	case dr.err != nil:
		return fmt.Errorf("index: ingest interrupted after %d documents: %w", next, dr.err)
	case next != dr.count:
		return fmt.Errorf("index: ingest interrupted after %d of %d documents: %w", next, dr.count, context.Cause(ctx))
	}
	return nil
}

// Flush writes buffered documents to a new segment file. The segment is not
// visible to readers until Close publishes the manifest.
func (w *Writer) Flush() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if err := w.usable(); err != nil {
		return err
	}
	if err := w.flushLocked(); err != nil {
		w.err = err
		return err
	}
	return nil
}

// Close flushes remaining documents and atomically publishes the manifest.
// After an earlier error it publishes nothing and returns an error wrapping
// ErrWriterFailed and removes unpublished segment files before releasing
// the writer lock. A second Close returns ErrClosed.
func (w *Writer) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return ErrClosed
	}
	w.closed = true
	err := w.publishLocked()
	if err != nil {
		// Remove this Writer's unpublished segments while still holding the
		// lock, so no later Writer can have reused their names.
		err = errors.Join(err, w.discardLocked())
	}
	w.buf = nil
	return errors.Join(err, w.lock.unlock())
}

func (w *Writer) publishLocked() error {
	if w.err != nil {
		return fmt.Errorf("%w: %v", ErrWriterFailed, w.err)
	}
	if err := w.flushLocked(); err != nil {
		w.err = err
		return err
	}
	m := &manifest{maxTermLen: w.opts.MaxTermLen, segments: w.segments}
	if err := writeManifest(w.dir, m, w.tab); err != nil {
		w.err = err
		if _, statErr := os.Stat(filepath.Join(w.dir, ManifestName)); statErr == nil {
			// The rename happened (only the directory fsync failed): the
			// index is published and its segments must be kept.
			w.segments = nil
		}
		return err
	}
	return nil
}

// Abort discards buffered documents, removes the segment files written by
// this Writer, and releases the writer lock. It is a no-op once Close or
// Abort has returned (Close already cleans up after a failure).
func (w *Writer) Abort() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return nil
	}
	w.closed = true
	w.buf = nil
	return errors.Join(w.discardLocked(), w.lock.unlock())
}

func (w *Writer) discardLocked() error {
	var errs []error
	for _, s := range w.segments {
		if err := os.Remove(filepath.Join(w.dir, s)); err != nil && !errors.Is(err, fs.ErrNotExist) {
			errs = append(errs, fmt.Errorf("index: remove unpublished segment: %w", err))
		}
	}
	w.segments = nil
	return errors.Join(errs...)
}

func (w *Writer) usable() error {
	switch {
	case w.closed:
		return ErrClosed
	case w.err != nil:
		return fmt.Errorf("%w: %v", ErrWriterFailed, w.err)
	}
	return nil
}

// commit adds one analyzed document to the buffer, flushing first if the
// document would push the buffer past MaxBufferBytes or MaxDocsPerSegment.
func (w *Writer) commit(d *analyzedDoc) error {
	need := w.buf.cost(d)
	if w.buf.docs() > 0 && (w.buf.bytes+need > w.opts.MaxBufferBytes || w.buf.docs() >= w.opts.MaxDocsPerSegment) {
		if err := w.flushLocked(); err != nil {
			return err
		}
		need = w.buf.cost(d)
	}
	if need > w.opts.MaxBufferBytes {
		return fmt.Errorf("%w: needs %d bytes, limit %d", ErrDocTooLarge, need, w.opts.MaxBufferBytes)
	}
	w.buf.add(d)
	return nil
}

func (w *Writer) flushLocked() error {
	if w.buf.docs() == 0 {
		return nil
	}
	name := segmentFileName(len(w.segments))
	path := filepath.Join(w.dir, name)
	// Never replace an existing segment: a reader may have it mapped. Under
	// the writer lock and checkFreshDir this can only trip if a process
	// outside the locking protocol wrote into the directory.
	if _, err := os.Lstat(path); err == nil {
		return fmt.Errorf("%w: segment %s already exists", ErrAbandonedBuild, name)
	} else if !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("index: stat segment: %w", err)
	}
	err := writeSegmentFile(path, w.buf, w.tab, &w.flushSc)
	clear(w.flushSc.terms) // do not pin the flushed segment's term strings
	if err != nil {
		return err
	}
	w.segments = append(w.segments, name)
	w.buf = newMemSegment()
	return nil
}

func (w *Writer) releaseDoc(d *analyzedDoc) {
	clear(d.terms) // drop string references so pooled docs do not pin terms
	d.terms, d.tfs, d.stored = d.terms[:0], d.tfs[:0], d.stored[:0]
	// Do not let one large document pin its arrays in the pool.
	if cap(d.terms) > maxPooledDocTerms {
		d.terms, d.tfs = nil, nil
	}
	if cap(d.stored) > maxPooledStoredBytes {
		d.stored = nil
	}
	d.length, d.err = 0, nil
	w.docPool.Put(d)
}

func dirOf(path string) string { return filepath.Dir(path) }

// analyzedDoc is a tokenized document ready to be committed.
type analyzedDoc struct {
	seq    uint64
	terms  []string // unique terms in first-occurrence order
	tfs    []uint32 // tfs[i] is the frequency of terms[i]
	length uint32   // indexed token count
	stored []byte   // encoded stored fields
	err    error
}

// Per-document analysis budget. Every distinct term is charged before it is
// inserted, conservatively covering both sides of its lifetime:
//   - analysis: an analyzer map entry (estimated, incl. table slack),
//     plus a string header and tf slot in analyzedDoc with 2x append slack;
//   - commit: termOverheadBytes plus the initial posting arrays in
//     memSegment;
//   - the term bytes, charged twice (token copy and map key).
//
// The analyzer's budget is WriterOptions.MaxBufferBytes, so a document is
// rejected with ErrDocTooLarge during tokenization, as soon as it crosses
// the budget, rather than after all its terms have been materialized. Any
// document that passes analysis also fits an empty memSegment, because the
// analyzer's charge is at least memSegment.cost of the same document.
const (
	analyzerMapEntryBytes = 64
	analyzerTermBytes     = analyzerMapEntryBytes + 2*(16+4) + termOverheadBytes + 2*4*minPostingCap
	// Pooled analyzers and docs that grew past these sizes are discarded
	// rather than reused, so one large document does not pin memory.
	maxPooledDocTerms    = 1 << 14
	maxPooledStoredBytes = 4 << 10
)

// analyzer holds per-goroutine analysis scratch space.
type analyzer struct {
	tok    tokenizer
	idx    map[string]int32
	budget int64 // maximum accounted bytes for one document
	used   int64 // accounted bytes of the document being analyzed
}

func newAnalyzer(maxTermLen int, budget int64) *analyzer {
	return &analyzer{tok: newTokenizer(maxTermLen), idx: make(map[string]int32), budget: budget}
}

// reset drops the previous document's terms; a map that grew large is
// replaced, since clearing does not release its table.
func (a *analyzer) reset() {
	if len(a.idx) > maxPooledDocTerms {
		a.idx = make(map[string]int32)
	} else {
		clear(a.idx)
	}
	a.used = 0
}

func (a *analyzer) charge(n int64) error {
	if a.used+n > a.budget {
		return fmt.Errorf("%w: analysis needs more than %d bytes", ErrDocTooLarge, a.budget)
	}
	a.used += n
	return nil
}

func (a *analyzer) analyze(c *model.SECChunk, d *analyzedDoc) error {
	if err := c.Validate(); err != nil {
		return err
	}
	defer a.reset()
	a.reset()
	if err := a.charge(docOverheadBytes); err != nil {
		return fmt.Errorf("chunk %q: %w", c.ID, err)
	}
	if len(c.Tokens) > 0 {
		for _, t := range c.Tokens {
			if err := a.feed(t, d); err != nil {
				return fmt.Errorf("chunk %q: %w", c.ID, err)
			}
		}
	} else if err := a.feed(c.Text, d); err != nil {
		return fmt.Errorf("chunk %q: %w", c.ID, err)
	}
	if d.length == 0 {
		return fmt.Errorf("chunk %q: %w", c.ID, ErrEmptyDocument)
	}
	d.stored = appendStored(d.stored[:0], c)
	if err := a.charge(int64(len(d.stored))); err != nil {
		return fmt.Errorf("chunk %q: %w", c.ID, err)
	}
	return nil
}

func (a *analyzer) feed(text string, d *analyzedDoc) error {
	for tok, pos := a.tok.next(text, 0); tok != nil; tok, pos = a.tok.next(text, pos) {
		if d.length == math.MaxUint32 {
			return fmt.Errorf("%w: more than 2^32-1 tokens", ErrDocTooLarge)
		}
		d.length++
		if i, ok := a.idx[string(tok)]; ok {
			d.tfs[i]++
			continue
		}
		if err := a.charge(int64(analyzerTermBytes + 2*len(tok))); err != nil {
			return err
		}
		t := string(tok)
		a.idx[t] = int32(len(d.terms))
		d.terms = append(d.terms, t)
		d.tfs = append(d.tfs, 1)
	}
	return nil
}

// Memory accounting for memSegment. Posting arrays are grown by this code
// (not by append), so their backing-array bytes are accounted exactly.
// termOverheadBytes is an estimate of map entry, string header, and
// postingBuf struct cost per distinct term; docOverheadBytes covers the
// per-document length and stored-offset entries. Go's runtime overhead for
// the map's own growth, slice growth of docLens/storedOffs/stored, and
// garbage from replaced backing arrays awaiting GC are not accounted.
const (
	termOverheadBytes = 112
	docOverheadBytes  = 12
	minPostingCap     = 4
)

type postingBuf struct {
	docs []uint32
	tfs  []uint32
}

// memSegment is the bounded in-memory buffer for the segment being built.
type memSegment struct {
	postings    map[string]*postingBuf
	docLens     []uint32
	stored      []byte
	storedOffs  []uint64 // len = docs+1, storedOffs[0] = 0
	totalTokens uint64
	bytes       int64 // accounted bytes, see constants above
}

func newMemSegment() *memSegment {
	return &memSegment{postings: make(map[string]*postingBuf), storedOffs: []uint64{0}}
}

func (m *memSegment) docs() int { return len(m.docLens) }

func growPostingCap(c int) int { return max(minPostingCap, 2*c) }

// cost returns the accounted bytes that adding d would allocate.
func (m *memSegment) cost(d *analyzedDoc) int64 {
	c := int64(len(d.stored) + docOverheadBytes)
	for _, t := range d.terms {
		pb := m.postings[t]
		switch {
		case pb == nil:
			c += int64(termOverheadBytes + len(t) + 2*4*minPostingCap)
		case len(pb.docs) == cap(pb.docs):
			c += int64(2 * 4 * (growPostingCap(cap(pb.docs)) - cap(pb.docs)))
		}
	}
	return c
}

func (m *memSegment) add(d *analyzedDoc) {
	id := uint32(len(m.docLens))
	for i, t := range d.terms {
		pb := m.postings[t]
		if pb == nil {
			pb = &postingBuf{docs: make([]uint32, 0, minPostingCap), tfs: make([]uint32, 0, minPostingCap)}
			m.postings[t] = pb
			m.bytes += int64(termOverheadBytes + len(t) + 2*4*minPostingCap)
		} else if len(pb.docs) == cap(pb.docs) {
			oc, nc := cap(pb.docs), growPostingCap(cap(pb.docs))
			pb.docs = append(make([]uint32, 0, nc), pb.docs...)
			pb.tfs = append(make([]uint32, 0, nc), pb.tfs...)
			m.bytes += int64(2 * 4 * (nc - oc))
		}
		pb.docs = append(pb.docs, id)
		pb.tfs = append(pb.tfs, d.tfs[i])
	}
	m.docLens = append(m.docLens, d.length)
	m.totalTokens += uint64(d.length)
	m.stored = append(m.stored, d.stored...)
	m.storedOffs = append(m.storedOffs, uint64(len(m.stored)))
	m.bytes += int64(len(d.stored) + docOverheadBytes)
}

func (m *memSegment) sortedTerms(dst []string) []string {
	for t := range m.postings {
		dst = append(dst, t)
	}
	slices.Sort(dst)
	return dst
}

// flushScratch is Writer-owned scratch reused across segment flushes. Its
// size is proportional to the largest flushed segment's term count.
type flushScratch struct {
	terms   []string
	entries [][termEntrySize]byte
	enc     []byte
	block   [BlockSize]uint32
}
