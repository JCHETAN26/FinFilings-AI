// Command bench_kit exercises the actual disk-backed index on caller-supplied
// SECChunk JSONL, without generated documents or mocked search functions.
package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

type config struct {
	Corpus        string        `json:"corpus"`
	Queries       string        `json:"queries"`
	Out           string        `json:"output_root"`
	Work          string        `json:"index_root"`
	Workers       int           `json:"query_workers"`
	Requests      int           `json:"requests_per_round"`
	Rounds        int           `json:"rounds"`
	OpenRuns      int           `json:"mmap_open_runs"`
	IngestWorkers int           `json:"ingest_workers"`
	BufferMiB     int           `json:"segment_buffer_mib"`
	SegmentDocs   int           `json:"max_docs_per_segment"`
	TopK          int           `json:"top_k"`
	Timeout       time.Duration `json:"query_timeout_ns"`
}

type query struct {
	Text       string `json:"query"`
	ExpectHits bool   `json:"expect_hits"`
}

type distribution struct {
	Count int     `json:"count"`
	Min   float64 `json:"min_us"`
	P50   float64 `json:"p50_us"`
	P95   float64 `json:"p95_us"`
	P99   float64 `json:"p99_us"`
	Max   float64 `json:"max_us"`
	Mean  float64 `json:"mean_us"`
}

// Nearest-rank percentiles over every measured request; no sampling.
func summarize(ns []int64) distribution {
	if len(ns) == 0 {
		return distribution{}
	}
	slices.Sort(ns)
	var total float64
	for _, n := range ns {
		total += float64(n)
	}
	rank := func(p float64) float64 { return float64(ns[max(0, int(math.Ceil(p*float64(len(ns))))-1)]) / 1000 }
	return distribution{len(ns), float64(ns[0]) / 1000, rank(.5), rank(.95), rank(.99), float64(ns[len(ns)-1]) / 1000, total / float64(len(ns)) / 1000}
}

type memory struct {
	HeapAlloc  uint64  `json:"heap_alloc_bytes"`
	HeapInuse  uint64  `json:"heap_inuse_bytes"`
	TotalAlloc uint64  `json:"total_alloc_bytes"`
	NumGC      uint32  `json:"num_gc"`
	PauseNS    uint64  `json:"gc_pause_total_ns"`
	RSSKiB     *uint64 `json:"process_rss_kib,omitempty"`
	Goroutines int     `json:"goroutines"`
}

func snapshot() memory {
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	out := memory{HeapAlloc: m.HeapAlloc, HeapInuse: m.HeapInuse, TotalAlloc: m.TotalAlloc, NumGC: m.NumGC, PauseNS: m.PauseTotalNs, Goroutines: runtime.NumGoroutine()}
	if b, err := os.ReadFile("/proc/self/status"); err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(line, "VmRSS:") {
				fields := strings.Fields(line)
				if len(fields) > 1 {
					if v, e := strconv.ParseUint(fields[1], 10, 64); e == nil {
						out.RSSKiB = &v
					}
				}
			}
		}
	}
	return out
}

type ingestResult struct {
	Docs      int   `json:"documents"`
	TextBytes int64 `json:"text_bytes"`
	Err       error `json:"-"`
}
type indexing struct {
	ingestResult
	ElapsedMS        float64 `json:"elapsed_ms"`
	DocsPerSecond    float64 `json:"documents_per_second"`
	TextMiBPerSecond float64 `json:"text_mib_per_second"`
}

// Each scanner line is capped at 4 MiB and queue depth is bounded. No corpus
// collection is kept in memory. The timer includes JSON decoding, analysis,
// compression, all flushes, and the durable manifest publication.
func build(ctx context.Context, corpus, dir string, c config) (indexing, error) {
	start := time.Now()
	w, err := index.NewWriter(dir, index.WriterOptions{Workers: c.IngestWorkers, QueueDepth: 2 * c.IngestWorkers, MaxBufferBytes: int64(c.BufferMiB) << 20, MaxDocsPerSegment: c.SegmentDocs})
	if err != nil {
		return indexing{}, err
	}
	defer w.Abort()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	docs := make(chan model.SECChunk, 2*c.IngestWorkers)
	done := make(chan ingestResult, 1)
	go func() {
		var r ingestResult
		defer func() { close(docs); done <- r }()
		f, e := os.Open(corpus)
		if e != nil {
			r.Err = e
			return
		}
		defer f.Close()
		scanner := bufio.NewScanner(f)
		scanner.Buffer(make([]byte, 64<<10), 4<<20)
		for scanner.Scan() {
			var chunk model.SECChunk
			if e := json.Unmarshal(scanner.Bytes(), &chunk); e != nil {
				r.Err = fmt.Errorf("document %d: %w", r.Docs, e)
				return
			}
			select {
			case docs <- chunk:
				r.Docs++
				r.TextBytes += int64(len(chunk.Text))
			case <-ctx.Done():
				r.Err = ctx.Err()
				return
			}
		}
		r.Err = scanner.Err()
	}()
	err = w.Ingest(ctx, docs)
	if err != nil {
		cancel()
	}
	r := <-done
	if err != nil || r.Err != nil {
		return indexing{}, errors.Join(err, r.Err)
	}
	if r.Docs == 0 {
		return indexing{}, errors.New("empty corpus")
	}
	if err = w.Close(); err != nil {
		return indexing{}, err
	}
	elapsed := time.Since(start)
	return indexing{r, float64(elapsed) / float64(time.Millisecond), float64(r.Docs) / elapsed.Seconds(), float64(r.TextBytes) / (1 << 20) / elapsed.Seconds()}, nil
}

type loadResult struct {
	Completed       int          `json:"completed"`
	Errors          int          `json:"search_errors"`
	Mismatches      int          `json:"result_mismatches"`
	UnexpectedEmpty int          `json:"unexpected_empty"`
	UnexpectedHits  int          `json:"unexpected_hits"`
	PeakActive      int64        `json:"peak_active_api_calls"`
	ElapsedMS       float64      `json:"elapsed_ms"`
	QPS             float64      `json:"completed_per_second"`
	Service         distribution `json:"search_latency"`
	Scheduled       distribution `json:"dispatch_attempt_to_return_latency"`
	Counts          []int        `json:"query_counts"`
	MemoryBefore    memory       `json:"memory_before"`
	MemoryAfter     memory       `json:"memory_after"`
	GCCount         uint32       `json:"gc_count_during_load"`
	AllocBytes      uint64       `json:"allocated_bytes_during_load"`
	GCPauseNS       uint64       `json:"gc_pause_ns_during_load"`
}

type job struct {
	id       int
	enqueued time.Time
}
type observation struct {
	id, query          int
	service, scheduled int64
	status             string
}

// Exactly c.Workers persistent query workers consume a bounded channel.
// API latency includes index admission wait, excludes job queue/validation/I/O.
// Dispatch latency additionally includes producer backpressure and queue wait.
// The CSV collector can backpressure workers; total QPS includes that overhead.
func load(ctx context.Context, ix *index.Index, qs []query, baseline [][]index.Hit, c config, output io.Writer) (loadResult, error) {
	writer := csv.NewWriter(output)
	if err := writer.Write([]string{"request", "query_index", "search_ns", "dispatch_attempt_to_return_ns", "status"}); err != nil {
		return loadResult{}, err
	}
	service := make([]int64, 0, c.Requests)
	scheduled := make([]int64, 0, c.Requests)
	jobs := make(chan job, c.Workers)
	observations := make(chan observation, 2*c.Workers)
	startGate := make(chan struct{})
	var workers, ready sync.WaitGroup
	var active, peak atomic.Int64
	workers.Add(c.Workers)
	ready.Add(c.Workers)
	for range c.Workers {
		go func() {
			defer workers.Done()
			ready.Done()
			<-startGate
			for j := range jobs {
				qi := j.id % len(qs)
				requestCtx, cancel := context.WithTimeout(ctx, c.Timeout)
				n := active.Add(1)
				for old := peak.Load(); n > old && !peak.CompareAndSwap(old, n); old = peak.Load() {
				}
				begin := time.Now()
				hits, err := ix.Search(requestCtx, qs[qi].Text, c.TopK)
				end := time.Now()
				active.Add(-1)
				cancel()
				status := "ok"
				switch {
				case err != nil:
					status = "error:" + err.Error()
				case len(hits) == 0 && qs[qi].ExpectHits:
					status = "unexpected_empty"
				case len(hits) > 0 && !qs[qi].ExpectHits:
					status = "unexpected_hits"
				case !slices.Equal(hits, baseline[qi]):
					status = "mismatch"
				}
				observations <- observation{j.id, qi, end.Sub(begin).Nanoseconds(), end.Sub(j.enqueued).Nanoseconds(), status}
			}
		}()
	}
	ready.Wait()
	r := loadResult{Counts: make([]int, len(qs)), MemoryBefore: snapshot()}
	start := time.Now()
	close(startGate)
	dispatched := make(chan struct{})
	go func() {
		defer close(dispatched)
		defer close(jobs)
		for i := 0; i < c.Requests; i++ {
			select {
			case jobs <- job{i, time.Now()}:
			case <-ctx.Done():
				return
			}
		}
	}()
	go func() { workers.Wait(); close(observations) }()
	var writeErr error
	for o := range observations {
		r.Completed++
		r.Counts[o.query]++
		service = append(service, o.service)
		scheduled = append(scheduled, o.scheduled)
		switch {
		case strings.HasPrefix(o.status, "error:"):
			r.Errors++
		case o.status == "mismatch":
			r.Mismatches++
		case o.status == "unexpected_empty":
			r.UnexpectedEmpty++
		case o.status == "unexpected_hits":
			r.UnexpectedHits++
		}
		if writeErr == nil {
			writeErr = writer.Write([]string{strconv.Itoa(o.id), strconv.Itoa(o.query), strconv.FormatInt(o.service, 10), strconv.FormatInt(o.scheduled, 10), o.status})
		}
	}
	<-dispatched
	elapsed := time.Since(start)
	r.MemoryAfter = snapshot()
	r.GCCount = r.MemoryAfter.NumGC - r.MemoryBefore.NumGC
	r.AllocBytes = r.MemoryAfter.TotalAlloc - r.MemoryBefore.TotalAlloc
	r.GCPauseNS = r.MemoryAfter.PauseNS - r.MemoryBefore.PauseNS
	writer.Flush()
	r.PeakActive = peak.Load()
	r.ElapsedMS = float64(elapsed) / float64(time.Millisecond)
	r.QPS = float64(r.Completed) / elapsed.Seconds()
	r.Service = summarize(service)
	r.Scheduled = summarize(scheduled)
	return r, errors.Join(writeErr, writer.Error(), ctx.Err())
}

type round struct {
	Number        int          `json:"round"`
	IndexPath     string       `json:"index_path"`
	Indexing      indexing     `json:"indexing"`
	Stats         index.Stats  `json:"index_stats"`
	Open          distribution `json:"validated_mmap_open"`
	OpenSamplesNS []int64      `json:"validated_mmap_open_samples_ns"`
	Load          loadResult   `json:"load"`
	Stable        bool         `json:"stable"`
}

type report struct {
	Started    string   `json:"started_at"`
	Config     config   `json:"config"`
	GoVersion  string   `json:"go_version"`
	GOOS       string   `json:"goos"`
	GOARCH     string   `json:"goarch"`
	CPUs       int      `json:"logical_cpus"`
	GOMAXPROCS int      `json:"gomaxprocs"`
	Host       string   `json:"hostname"`
	CPUModel   string   `json:"cpu_model"`
	CorpusSHA  string   `json:"corpus_sha256"`
	QuerySHA   string   `json:"queries_sha256"`
	SourceSHA  string   `json:"source_sha256"`
	Queries    []query  `json:"queries"`
	Notes      []string `json:"measurement_notes"`
	Rounds     []round  `json:"rounds"`
	Stable     bool     `json:"stable"`
}

func fileHash(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err = io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func sourceHash() (string, error) {
	paths := []string{"go.mod"}
	for _, root := range []string{"pkg", "bench_kit"} {
		err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				if d.Name() == "results" {
					return filepath.SkipDir
				}
				return nil
			}
			if strings.HasSuffix(path, ".go") || strings.HasSuffix(path, ".s") || strings.HasSuffix(path, ".py") {
				paths = append(paths, path)
			}
			return nil
		})
		if err != nil {
			return "", err
		}
	}
	sort.Strings(paths)
	h := sha256.New()
	for _, p := range paths {
		sum, err := fileHash(p)
		if err != nil {
			return "", err
		}
		fmt.Fprintf(h, "%s\x00%s\n", p, sum)
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func readQueries(path string) ([]query, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 4096), 65536)
	var qs []query
	for scanner.Scan() {
		var q query
		if err = json.Unmarshal(scanner.Bytes(), &q); err != nil {
			return nil, err
		}
		if strings.TrimSpace(q.Text) == "" {
			return nil, errors.New("empty query")
		}
		if len(qs) == 1024 {
			return nil, errors.New("workload exceeds 1024 queries")
		}
		qs = append(qs, q)
	}
	if err = scanner.Err(); err != nil {
		return nil, err
	}
	if len(qs) == 0 {
		return nil, errors.New("empty workload")
	}
	return qs, nil
}

func validate(c config) error {
	if c.Workers < 1 || c.Workers > 1024 || c.Requests < c.Workers || c.Requests > 1_000_000 || c.Rounds < 1 || c.Rounds > 100 || c.OpenRuns < 1 || c.OpenRuns > 1000 || c.TopK < 1 || c.TopK > 1000 || c.IngestWorkers < 1 || c.IngestWorkers > 1024 || c.BufferMiB < 1 || c.BufferMiB > 1024 || c.SegmentDocs < 1 || c.SegmentDocs > 1<<26 || c.Timeout <= 0 {
		return errors.New("invalid limits: workers/ingest 1..1024, requests workers..1e6, rounds 1..100, opens 1..1000, topk 1..1000, buffer 1..1024 MiB, segment docs 1..2^26, positive timeout")
	}
	return nil
}

func run(c config) (string, error) {
	if err := validate(c); err != nil {
		return "", err
	}
	qs, err := readQueries(c.Queries)
	if err != nil {
		return "", err
	}
	corpusHash, err := fileHash(c.Corpus)
	if err != nil {
		return "", err
	}
	queryHash, err := fileHash(c.Queries)
	if err != nil {
		return "", err
	}
	source, err := sourceHash()
	if err != nil {
		return "", err
	}
	for _, dir := range []string{c.Out, c.Work} {
		if err = os.MkdirAll(dir, 0755); err != nil {
			return "", err
		}
	}
	out, err := os.MkdirTemp(c.Out, "phase1-")
	if err != nil {
		return "", err
	}
	hostname, _ := os.Hostname()
	cpu := "unavailable"
	if b, e := os.ReadFile("/proc/cpuinfo"); e == nil {
		for _, l := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(l, "model name") {
				_, cpu, _ = strings.Cut(l, ":")
				cpu = strings.TrimSpace(cpu)
				break
			}
		}
	}
	r := report{Started: time.Now().UTC().Format(time.RFC3339), Config: c, GoVersion: runtime.Version(), GOOS: runtime.GOOS, GOARCH: runtime.GOARCH, CPUs: runtime.NumCPU(), GOMAXPROCS: runtime.GOMAXPROCS(0), Host: hostname, CPUModel: cpu, CorpusSHA: corpusHash, QuerySHA: queryHash, SourceSHA: source, Queries: qs, Stable: true, Notes: []string{
		"Real caller-supplied SECChunk JSONL; verify provenance.json for data origin. No duplication or generated documents in the supplied SEC preparation script.",
		"Indexing includes disk JSONL read/decode, analysis, PFOR, segment flushes and fsync/manifest publication; excludes network download and HTML preparation.",
		"Validated mmap initialization is end-to-end index.Open, including manifest parsing, syscalls, CRC checks and structure validation; not isolated mmap syscall time. Filesystem cache is warm after indexing; caches are not dropped.",
		"Fixed-work saturated closed-loop load; bounded job queue=workers, results queue=2*workers. Not an open-loop arrival-rate test or HTTP measurement. No SLO pass threshold is assumed.",
		"Exactly workers persistent query goroutines; index admission slots also equal workers. Search latency includes admission, excludes dispatch queue, validation and CSV output; dispatch-attempt latency additionally includes queue/backpressure. QPS includes validation/collection overhead.",
		"Pre-load warmup: every worker runs every query once. Every measured hit list must exactly match a serial baseline (including scores and metadata). This tests stability, not independently labeled relevance.",
		"Nearest-rank percentiles over every observation; exact latency storage bounded to 16 bytes/request (max 1e6 requests). Raw CSV contains every request, including failures.",
		"Stable means every scheduled request completed, no search errors, no baseline mismatches and no unexpected hit/empty results; does not certify production scale or multi-tenant safety.",
	}}
	for n := 1; n <= c.Rounds; n++ {
		runtime.GC()
		dir, e := os.MkdirTemp(c.Work, fmt.Sprintf("round-%d-", n))
		if e != nil {
			return out, e
		}
		idx, e := build(context.Background(), c.Corpus, dir, c)
		if e != nil {
			return out, e
		}
		runtime.GC()
		rr := round{Number: n, IndexPath: dir, Indexing: idx}
		opts := index.Options{MaxConcurrentSearches: c.Workers}
		for range c.OpenRuns {
			begin := time.Now()
			ix, e := index.Open(dir, opts)
			took := time.Since(begin)
			if e != nil {
				return out, e
			}
			rr.OpenSamplesNS = append(rr.OpenSamplesNS, took.Nanoseconds())
			if e = ix.Close(); e != nil {
				return out, e
			}
		}
		rr.Open = summarize(slices.Clone(rr.OpenSamplesNS))
		ix, e := index.Open(dir, opts)
		if e != nil {
			return out, e
		}
		if rr.Stats, e = ix.Stats(); e != nil {
			ix.Close()
			return out, e
		}
		if rr.Stats.Docs != uint64(idx.Docs) {
			ix.Close()
			return out, errors.New("indexed document count mismatch")
		}
		baseline := make([][]index.Hit, len(qs))
		for i, q := range qs {
			baseline[i], e = ix.Search(context.Background(), q.Text, c.TopK)
			if e != nil {
				ix.Close()
				return out, e
			}
			if (len(baseline[i]) > 0) != q.ExpectHits {
				ix.Close()
				return out, fmt.Errorf("query %q failed expected hit/empty check", q.Text)
			}
		}
		if e = warm(ix, qs, baseline, c); e != nil {
			ix.Close()
			return out, e
		}
		f, e := os.Create(filepath.Join(out, fmt.Sprintf("requests-%d.csv", n)))
		if e != nil {
			ix.Close()
			return out, e
		}
		rr.Load, e = load(context.Background(), ix, qs, baseline, c, f)
		e = errors.Join(e, f.Close(), ix.Close())
		if e != nil {
			return out, e
		}
		rr.Stable = rr.Load.Completed == c.Requests && rr.Load.Errors == 0 && rr.Load.Mismatches == 0 && rr.Load.UnexpectedEmpty == 0 && rr.Load.UnexpectedHits == 0
		r.Stable = r.Stable && rr.Stable
		r.Rounds = append(r.Rounds, rr)
		fmt.Printf("Round %d: %d docs, %.0f docs/s, %.2f text MiB/s; mmap-open p50 %.3f ms p99 %.3f ms; %d workers, %d requests, %.0f QPS, search p50 %.3f ms p95 %.3f ms p99 %.3f ms max %.3f ms; errors=%d mismatches=%d stable=%v\n", n, idx.Docs, idx.DocsPerSecond, idx.TextMiBPerSecond, rr.Open.P50/1000, rr.Open.P99/1000, c.Workers, rr.Load.Completed, rr.Load.QPS, rr.Load.Service.P50/1000, rr.Load.Service.P95/1000, rr.Load.Service.P99/1000, rr.Load.Service.Max/1000, rr.Load.Errors, rr.Load.Mismatches, rr.Stable)
	}
	// Catch corpus/workload changes during the run, rather than report a stale identity.
	finalCorpus, e := fileHash(c.Corpus)
	if e != nil {
		return out, e
	}
	finalQueries, e := fileHash(c.Queries)
	if e != nil {
		return out, e
	}
	if finalCorpus != corpusHash || finalQueries != queryHash {
		return out, errors.New("input changed during benchmark")
	}
	b, e := json.MarshalIndent(r, "", "  ")
	if e != nil {
		return out, e
	}
	if e = os.WriteFile(filepath.Join(out, "report.json"), append(b, '\n'), 0644); e != nil {
		return out, e
	}
	if !r.Stable {
		return out, errors.New("stability checks failed; see report and raw CSV")
	}
	return out, nil
}

func warm(ix *index.Index, qs []query, baseline [][]index.Hit, c config) error {
	var wg sync.WaitGroup
	errs := make(chan error, c.Workers)
	for range c.Workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i, q := range qs {
				ctx, cancel := context.WithTimeout(context.Background(), c.Timeout)
				hits, err := ix.Search(ctx, q.Text, c.TopK)
				cancel()
				if err != nil {
					errs <- err
					return
				}
				if !slices.Equal(hits, baseline[i]) {
					errs <- errors.New("warmup result mismatch")
					return
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	var all []error
	for e := range errs {
		all = append(all, e)
	}
	return errors.Join(all...)
}

func main() {
	var c config
	flag.StringVar(&c.Corpus, "corpus", "data/sec/chunks.jsonl", "SECChunk JSONL file")
	flag.StringVar(&c.Queries, "queries", "bench_kit/queries.jsonl", "bounded query workload JSONL")
	flag.StringVar(&c.Out, "out", "bench_kit/results", "new result directory parent")
	flag.StringVar(&c.Work, "work-dir", "indexes/phase1-bench", "new disk index directory parent")
	flag.IntVar(&c.Workers, "workers", 50, "concurrent query goroutines and index admission slots")
	flag.IntVar(&c.Requests, "requests", 100000, "requests per round (max 1e6)")
	flag.IntVar(&c.Rounds, "rounds", 3, "independent indexing and load rounds")
	flag.IntVar(&c.OpenRuns, "open-runs", 10, "validated mmap opens per round")
	flag.IntVar(&c.IngestWorkers, "ingest-workers", 8, "bounded analysis workers")
	flag.IntVar(&c.BufferMiB, "buffer-mib", 8, "accounted segment/analyzer budget MiB")
	flag.IntVar(&c.SegmentDocs, "segment-docs", 256, "force bounded on-disk segment sizes")
	flag.IntVar(&c.TopK, "top-k", 10, "requested search hits")
	flag.DurationVar(&c.Timeout, "query-timeout", 5*time.Second, "per-query deadline")
	flag.Parse()
	out, err := run(c)
	if out != "" {
		fmt.Println("Results:", out)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
