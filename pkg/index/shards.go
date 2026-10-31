package index

// Sharded layout: one immutable index directory per filing quarter under a
// shard root, plus a root manifest published last.
//
//	<root>/
//	  2023_Q4/          ordinary index directory (MANIFEST + segment files)
//	  2024_Q1/
//	  shards.json       published by ShardedWriter.Close after every shard
//
// A ShardRouter opens only the shards listed in shards.json, maps every
// segment of every shard read-only (syscall.Mmap via Open), and answers each
// query by fanning out over the shards on a bounded worker pool. Scoring uses
// the union corpus statistics (document count, token count, and per-term
// document frequency summed over all shards), so merged BM25 scores are
// identical to those of a single Index holding the same documents in shard
// order. Ties are broken by shard order, then by index order within a shard.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"sort"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// ShardManifestName is the root manifest naming the published shards.
const ShardManifestName = "shards.json"

const (
	shardManifestVersion = 1
	maxShardManifestSize = 4 << 20
	// MaxShardsLimit is the largest number of shards a root may list.
	MaxShardsLimit = 1 << 16
)

var shardNamePattern = regexp.MustCompile(`^(\d{4})_Q([1-4])$`)

// ShardKey identifies a calendar filing quarter. Its directory name is
// YYYY_Q{1-4}.
type ShardKey struct {
	Year    int
	Quarter int
}

// ShardKeyForDate returns the calendar quarter of a filing date.
func ShardKeyForDate(t time.Time) ShardKey {
	return ShardKey{Year: t.Year(), Quarter: (int(t.Month())-1)/3 + 1}
}

// Validate checks the year against the EDGAR range and the quarter against 1..4.
func (k ShardKey) Validate() error {
	if k.Year < model.MinFilingYear || k.Year > model.MaxFilingYear || k.Quarter < 1 || k.Quarter > 4 {
		return fmt.Errorf("%w: shard key %d Q%d", ErrInvalidArgument, k.Year, k.Quarter)
	}
	return nil
}

// String returns the shard directory name, e.g. "2024_Q1".
func (k ShardKey) String() string { return fmt.Sprintf("%04d_Q%d", k.Year, k.Quarter) }

func (k ShardKey) less(o ShardKey) bool {
	return k.Year < o.Year || (k.Year == o.Year && k.Quarter < o.Quarter)
}

// ParseShardKey parses a YYYY_Q{1-4} directory name.
func ParseShardKey(name string) (ShardKey, error) {
	m := shardNamePattern.FindStringSubmatch(name)
	if m == nil {
		return ShardKey{}, fmt.Errorf("%w: shard name %q", ErrInvalidArgument, name)
	}
	year, _ := strconv.Atoi(m[1])
	quarter, _ := strconv.Atoi(m[2])
	k := ShardKey{Year: year, Quarter: quarter}
	return k, k.Validate()
}

type shardManifest struct {
	Version    int                  `json:"version"`
	MaxTermLen int                  `json:"max_term_len"`
	Shards     []shardManifestEntry `json:"shards"`
}

type shardManifestEntry struct {
	Name string `json:"name"`
	Docs uint64 `json:"docs"`
}

// ShardedWriterOptions configures a ShardedWriter. Zero values select the
// defaults.
type ShardedWriterOptions struct {
	// Writer configures every per-shard Writer. When MaxBufferBytes is zero
	// it defaults to 8 MiB here (not the single-index 64 MiB), and Workers
	// defaults to 1 because ShardedWriter feeds shards through Writer.Add.
	Writer WriterOptions
	// MaxOpenShards bounds how many quarter shards may be written by one
	// ShardedWriter; every shard keeps its Writer (and its segment buffer)
	// open until Close, so the accounted buffer memory is at most
	// MaxOpenShards x Writer.MaxBufferBytes. Default 16, max 256.
	MaxOpenShards int
}

func (o ShardedWriterOptions) withDefaults() (ShardedWriterOptions, error) {
	if o.Writer.MaxBufferBytes == 0 {
		o.Writer.MaxBufferBytes = 8 << 20
	}
	if o.Writer.Workers == 0 {
		o.Writer.Workers = 1
	}
	if o.MaxOpenShards == 0 {
		o.MaxOpenShards = 16
	}
	if o.MaxOpenShards < 1 || o.MaxOpenShards > 256 {
		return o, fmt.Errorf("%w: MaxOpenShards %d", ErrInvalidArgument, o.MaxOpenShards)
	}
	w, err := o.Writer.withDefaults()
	o.Writer = w
	return o, err
}

// ShardedWriter routes chunks to one Writer per filing quarter under a fresh
// root directory. Methods are serialized by an internal mutex. Errors are
// sticky; Close publishes nothing after an error, and both a failed Close
// and Abort remove the root directory this writer created. Like Writer, it
// keeps no chunk list: only per-shard document counts.
type ShardedWriter struct {
	mu      sync.Mutex
	root    string
	opts    ShardedWriterOptions
	writers map[ShardKey]*Writer
	docs    map[ShardKey]uint64
	err     error
	closed  bool
}

// NewShardedWriter creates root (which must not exist; its parent is created
// if needed) for a new sharded index.
func NewShardedWriter(root string, opts ShardedWriterOptions) (*ShardedWriter, error) {
	opts, err := opts.withDefaults()
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(root), 0o755); err != nil {
		return nil, fmt.Errorf("index: create shard root parent: %w", err)
	}
	if err := os.Mkdir(root, 0o755); err != nil {
		if errors.Is(err, os.ErrExist) {
			return nil, fmt.Errorf("%w: %s", ErrIndexExists, root)
		}
		return nil, fmt.Errorf("index: create shard root: %w", err)
	}
	return &ShardedWriter{root: root, opts: opts, writers: map[ShardKey]*Writer{}, docs: map[ShardKey]uint64{}}, nil
}

// Add validates key and buffers c into that quarter's shard.
func (w *ShardedWriter) Add(ctx context.Context, key ShardKey, c model.SECChunk) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return ErrClosed
	}
	if w.err != nil {
		return fmt.Errorf("%w: %v", ErrWriterFailed, w.err)
	}
	if err := key.Validate(); err != nil {
		return err
	}
	sw := w.writers[key]
	if sw == nil {
		if len(w.writers) >= w.opts.MaxOpenShards {
			w.err = fmt.Errorf("%w: more than MaxOpenShards=%d quarter shards", ErrInvalidArgument, w.opts.MaxOpenShards)
			return w.err
		}
		var err error
		if sw, err = NewWriter(filepath.Join(w.root, key.String()), w.opts.Writer); err != nil {
			w.err = err
			return err
		}
		w.writers[key] = sw
	}
	if err := sw.Add(ctx, c); err != nil {
		w.err = err
		return err
	}
	w.docs[key]++
	return nil
}

// Close publishes every shard in chronological order, then atomically
// publishes shards.json. On any failure the root directory is removed.
func (w *ShardedWriter) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return ErrClosed
	}
	w.closed = true
	err := w.publishLocked()
	if err != nil {
		err = errors.Join(err, w.abortWritersLocked(), os.RemoveAll(w.root))
	}
	return err
}

func (w *ShardedWriter) publishLocked() error {
	if w.err != nil {
		return fmt.Errorf("%w: %v", ErrWriterFailed, w.err)
	}
	if len(w.writers) == 0 {
		return fmt.Errorf("%w: no documents added", ErrInvalidArgument)
	}
	keys := make([]ShardKey, 0, len(w.writers))
	for k := range w.writers {
		keys = append(keys, k)
	}
	slices.SortFunc(keys, func(a, b ShardKey) int {
		if a.less(b) {
			return -1
		}
		if b.less(a) {
			return 1
		}
		return 0
	})
	m := shardManifest{Version: shardManifestVersion, MaxTermLen: w.opts.Writer.MaxTermLen}
	for _, k := range keys {
		sw := w.writers[k]
		delete(w.writers, k)
		if err := sw.Close(); err != nil {
			return fmt.Errorf("index: publish shard %s: %w", k, err)
		}
		m.Shards = append(m.Shards, shardManifestEntry{Name: k.String(), Docs: w.docs[k]})
	}
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(w.root, ".shards-*.tmp")
	if err != nil {
		return err
	}
	_, err = tmp.Write(append(data, '\n'))
	err = errors.Join(err, tmp.Sync(), tmp.Close())
	if err == nil {
		err = os.Rename(tmp.Name(), filepath.Join(w.root, ShardManifestName))
	}
	if err != nil {
		return errors.Join(err, os.Remove(tmp.Name()))
	}
	return errors.Join(syncDir(w.root), syncDir(filepath.Dir(w.root)))
}

func (w *ShardedWriter) abortWritersLocked() error {
	var errs []error
	for k, sw := range w.writers {
		errs = append(errs, sw.Abort())
		delete(w.writers, k)
	}
	return errors.Join(errs...)
}

// Abort discards every unpublished shard and removes the root directory. It
// is a no-op after Close or Abort.
func (w *ShardedWriter) Abort() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return nil
	}
	w.closed = true
	return errors.Join(w.abortWritersLocked(), os.RemoveAll(w.root))
}

// ShardRouterOptions configures a ShardRouter. Zero values select the
// defaults.
type ShardRouterOptions struct {
	// Index configures every shard. Its MaxConcurrentSearches is always set
	// to the router's MaxConcurrentSearches: each admitted router search
	// holds at most one slot per shard at a time, so shard admission never
	// blocks. Peak scoring scratch is therefore
	// shards x MaxConcurrentSearches x ~12 bytes x largest-segment docs.
	Index Options
	// MaxConcurrentSearches is the number of router search slots; further
	// callers wait, honoring their context and Close. Default
	// runtime.GOMAXPROCS(0), max 4096.
	MaxConcurrentSearches int
	// Workers is the number of goroutines one search uses to fan out over
	// shards. Default min(runtime.GOMAXPROCS(0), shards), max 256. Total
	// fan-out goroutines are bounded by MaxConcurrentSearches x Workers.
	Workers int
	// MaxShards rejects roots listing more shards. Default 4096, max
	// MaxShardsLimit.
	MaxShards int
}

// ShardInfo describes one opened shard.
type ShardInfo struct {
	Key  ShardKey
	Docs uint64
}

// ShardRouter is a read-only, memory-mapped view over a sharded root.
// Search and Stats are safe for concurrent use. Close waits for admitted
// searches, then closes (and unmaps) every shard; hits are copied out of the
// mappings and stay valid after Close.
type ShardRouter struct {
	mu        sync.RWMutex
	closed    bool
	shards    []*Index
	info      []ShardInfo
	docs      uint64
	tokens    uint64
	opts      ShardRouterOptions
	scratch   sync.Pool // *routerScratch; in-use count <= cap(slots)
	slots     chan struct{}
	done      chan struct{}
	closeOnce sync.Once
}

type routerScratch struct {
	dfs    [][]uint64 // per shard, per query term
	global collectionStats
	hits   [][]Hit // per shard ranked hits
	errs   []error
	merged []Hit
}

// OpenShardRouter opens every shard listed in root/shards.json. Unlisted
// directories are ignored. A malformed manifest, an invalid or duplicate
// shard name, a document count that disagrees with the opened shard, or
// shards built with different MaxTermLen fail with ErrCorrupt; any failure
// closes the shards opened so far.
func OpenShardRouter(root string, opts ShardRouterOptions) (*ShardRouter, error) {
	if opts.MaxConcurrentSearches == 0 {
		opts.MaxConcurrentSearches = runtime.GOMAXPROCS(0)
	}
	if opts.MaxShards == 0 {
		opts.MaxShards = 4096
	}
	if opts.MaxConcurrentSearches < 1 || opts.MaxConcurrentSearches > 4096 || opts.MaxShards < 1 || opts.MaxShards > MaxShardsLimit || opts.Workers < 0 || opts.Workers > 256 {
		return nil, fmt.Errorf("%w: router limits", ErrInvalidArgument)
	}
	opts.Index.MaxConcurrentSearches = opts.MaxConcurrentSearches
	if _, err := opts.Index.withDefaults(); err != nil {
		return nil, err
	}
	m, err := readShardManifest(root, opts.MaxShards)
	if err != nil {
		return nil, err
	}
	if opts.Workers == 0 {
		opts.Workers = min(runtime.GOMAXPROCS(0), len(m.Shards))
	}
	r := &ShardRouter{opts: opts, slots: make(chan struct{}, opts.MaxConcurrentSearches), done: make(chan struct{})}
	for _, e := range m.Shards {
		key, _ := ParseShardKey(e.Name) // validated by readShardManifest
		ix, err := Open(filepath.Join(root, e.Name), opts.Index)
		if err != nil {
			return nil, errors.Join(fmt.Errorf("index: open shard %s: %w", e.Name, err), r.closeShards())
		}
		r.shards = append(r.shards, ix)
		if ix.docs != e.Docs || ix.maxTermLen != m.MaxTermLen {
			return nil, errors.Join(fmt.Errorf("%w: shard %s disagrees with %s (docs %d vs %d, max term len %d vs %d)", ErrCorrupt, e.Name, ShardManifestName, ix.docs, e.Docs, ix.maxTermLen, m.MaxTermLen), r.closeShards())
		}
		r.info = append(r.info, ShardInfo{Key: key, Docs: ix.docs})
		r.docs += ix.docs
		r.tokens += ix.tokens
	}
	n := len(r.shards)
	r.scratch.New = func() any {
		return &routerScratch{dfs: make([][]uint64, n), hits: make([][]Hit, n), errs: make([]error, n)}
	}
	return r, nil
}

func readShardManifest(root string, maxShards int) (*shardManifest, error) {
	f, err := os.Open(filepath.Join(root, ShardManifestName))
	if err != nil {
		return nil, fmt.Errorf("index: open shard manifest: %w", err)
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, maxShardManifestSize+1))
	if err != nil {
		return nil, fmt.Errorf("index: read shard manifest: %w", err)
	}
	if len(data) > maxShardManifestSize {
		return nil, fmt.Errorf("%w: shard manifest exceeds %d bytes", ErrCorrupt, maxShardManifestSize)
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var m shardManifest
	if err := dec.Decode(&m); err != nil {
		return nil, fmt.Errorf("%w: shard manifest: %v", ErrCorrupt, err)
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, fmt.Errorf("%w: trailing data in shard manifest", ErrCorrupt)
	}
	if m.Version != shardManifestVersion || m.MaxTermLen < 1 || m.MaxTermLen > MaxTermLenLimit || len(m.Shards) == 0 {
		return nil, fmt.Errorf("%w: shard manifest header", ErrCorrupt)
	}
	if len(m.Shards) > maxShards {
		return nil, fmt.Errorf("%w: %d shards exceed MaxShards=%d", ErrInvalidArgument, len(m.Shards), maxShards)
	}
	var prev ShardKey
	for i, e := range m.Shards {
		k, err := ParseShardKey(e.Name)
		if err != nil || e.Docs == 0 || (i > 0 && !prev.less(k)) {
			return nil, fmt.Errorf("%w: shard entry %d (%q) invalid, empty, or out of order", ErrCorrupt, i, e.Name)
		}
		prev = k
	}
	return &m, nil
}

// Shards returns the opened shards in chronological order.
func (r *ShardRouter) Shards() []ShardInfo { return slices.Clone(r.info) }

// Stats sums the statistics of every shard.
func (r *ShardRouter) Stats() (Stats, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if r.closed {
		return Stats{}, ErrClosed
	}
	var total Stats
	for _, ix := range r.shards {
		s, err := ix.Stats()
		if err != nil {
			return Stats{}, err
		}
		total.Segments += s.Segments
		total.Docs += s.Docs
		total.Tokens += s.Tokens
		total.Terms += s.Terms
		total.Postings += s.Postings
		total.PostingBytes += s.PostingBytes
		total.FileBytes += s.FileBytes
	}
	return total, nil
}

// Search is SearchFiltered with no filter.
func (r *ShardRouter) Search(ctx context.Context, query string, k int) ([]Hit, error) {
	return r.SearchFiltered(ctx, query, k, model.FilingFilter{})
}

// SearchFiltered returns up to k hits over all shards ranked by BM25 with
// union corpus statistics; see the package-level sharding notes. It runs two
// bounded fan-outs (document frequencies, then per-shard top-k scoring) and
// merges the per-shard rankings.
func (r *ShardRouter) SearchFiltered(ctx context.Context, query string, k int, filter model.FilingFilter) ([]Hit, error) {
	if err := filter.Validate(); err != nil {
		return nil, err
	}
	maxTopK := r.opts.Index.MaxTopK
	if maxTopK == 0 {
		maxTopK = 1000
	}
	if k < 1 || k > maxTopK {
		return nil, fmt.Errorf("%w: k=%d outside [1, %d]", ErrInvalidArgument, k, maxTopK)
	}
	select {
	case <-r.done:
		return nil, ErrClosed
	default:
	}
	select {
	case r.slots <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-r.done:
		return nil, ErrClosed
	}
	defer func() { <-r.slots }()
	r.mu.RLock()
	defer r.mu.RUnlock()
	if r.closed {
		return nil, ErrClosed
	}
	sc := r.scratch.Get().(*routerScratch)
	defer func() {
		for i := range sc.hits {
			sc.hits[i] = nil // do not pin returned hit slices
		}
		clear(sc.errs)
		clear(sc.merged)
		sc.merged = sc.merged[:0]
		r.scratch.Put(sc)
	}()

	if err := r.fanOut(ctx, sc.errs, func(ctx context.Context, i int) error {
		var err error
		sc.dfs[i], err = r.shards[i].termDFs(ctx, query, sc.dfs[i][:0])
		return err
	}); err != nil {
		return nil, err
	}
	nt := len(sc.dfs[0])
	if nt == 0 || r.docs == 0 {
		return nil, nil
	}
	sc.global.docs, sc.global.tokens = r.docs, r.tokens
	sc.global.df = append(sc.global.df[:0], make([]uint64, nt)...)
	for _, dfs := range sc.dfs {
		if len(dfs) != nt {
			return nil, fmt.Errorf("%w: shards analyzed the query differently", ErrCorrupt)
		}
		for t, df := range dfs {
			sc.global.df[t] += df
		}
	}
	if err := r.fanOut(ctx, sc.errs, func(ctx context.Context, i int) error {
		var err error
		sc.hits[i], err = r.shards[i].searchFiltered(ctx, query, k, filter, &sc.global)
		return err
	}); err != nil {
		return nil, err
	}
	for _, hs := range sc.hits {
		sc.merged = append(sc.merged, hs...)
	}
	// Stable: equal scores keep shard order, then within-shard index order.
	sort.SliceStable(sc.merged, func(a, b int) bool { return sc.merged[a].Score > sc.merged[b].Score })
	if len(sc.merged) == 0 {
		return nil, nil
	}
	return slices.Clone(sc.merged[:min(k, len(sc.merged))]), nil
}

// fanOut runs fn for every shard on at most Workers goroutines. The first
// failure cancels the remaining work; it returns the parent context error if
// that ended, else the first non-cancellation error in shard order.
func (r *ShardRouter) fanOut(parent context.Context, errs []error, fn func(context.Context, int) error) error {
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	var next atomic.Int64
	var wg sync.WaitGroup
	for range min(r.opts.Workers, len(r.shards)) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				i := int(next.Add(1) - 1)
				if i >= len(r.shards) {
					return
				}
				if err := fn(ctx, i); err != nil {
					errs[i] = err
					cancel()
				}
			}
		}()
	}
	wg.Wait()
	if err := parent.Err(); err != nil {
		return err
	}
	var first error
	for _, err := range errs {
		if err == nil {
			continue
		}
		if !errors.Is(err, context.Canceled) {
			return err
		}
		if first == nil {
			first = err
		}
	}
	return first
}

// Close waits for admitted searches, then closes every shard. It is
// idempotent.
func (r *ShardRouter) Close() error {
	r.closeOnce.Do(func() { close(r.done) })
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return nil
	}
	r.closed = true
	return r.closeShards()
}

func (r *ShardRouter) closeShards() error {
	var errs []error
	for _, ix := range r.shards {
		errs = append(errs, ix.Close())
	}
	r.shards = nil
	return errors.Join(errs...)
}
