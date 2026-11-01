// Command load runs a finite closed-loop HTTP load with bounded workers and exact empirical quantiles.
package main

import (
	"bufio"
	"bytes"
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
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type query struct {
	ID    string `json:"id"`
	Query string `json:"query"`
}
type serverTimings struct {
	Present   bool    `json:"-"`
	Admission float64 `json:"admission_seconds"`
	Sparse    float64 `json:"sparse_seconds"`
	Dense     float64 `json:"dense_seconds"`
	Fusion    float64 `json:"fusion_seconds"`
	Rerank    float64 `json:"rerank_seconds"`
	Total     float64 `json:"total_seconds"`
}

type observation struct {
	ID, Worker, Status int
	NS                 int64
	Error              string
	Timings            serverTimings
}
type hit struct {
	ID       string              `json:"id"`
	Score    float64             `json:"score"`
	Text     string              `json:"text"`
	Metadata struct{ ID string } `json:"metadata"`
}
type percentiles struct {
	P50 int64 `json:"p50_ns"`
	P95 int64 `json:"p95_ns"`
	P99 int64 `json:"p99_ns"`
	Max int64 `json:"max_ns"`
}
type result struct {
	Endpoint    string         `json:"endpoint"`
	Requests    int            `json:"requests"`
	Workers     int            `json:"workers"`
	Peak        int64          `json:"peak_client_inflight"`
	Success     int            `json:"successes"`
	Failures    int            `json:"failures"`
	FailureRate float64        `json:"failure_rate"`
	Seconds     float64        `json:"seconds"`
	AttemptQPS  float64        `json:"completed_attempts_qps"`
	SuccessQPS  float64        `json:"successful_qps"`
	All         percentiles    `json:"all_attempt_latency"`
	Successful  *percentiles   `json:"successful_latency"`
	Statuses    map[string]int `json:"statuses"`
}

func timingCell(t serverTimings, value float64) string {
	if !t.Present {
		return ""
	}
	return strconv.FormatFloat(value, 'g', -1, 64)
}

func quantiles(values []int64) percentiles {
	sort.Slice(values, func(i, j int) bool { return values[i] < values[j] })
	at := func(p float64) int64 { return values[int(math.Ceil(p*float64(len(values))))-1] }
	return percentiles{at(.50), at(.95), at(.99), values[len(values)-1]}
}
func request(ctx context.Context, client *http.Client, url string, q query, dense bool, capture ...*serverTimings) (int, error) {
	payload := map[string]any{"query": q.Query}
	if dense {
		payload["k"] = 100
	} else {
		payload["rerank"] = true
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return 0, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, (1<<20)+1))
	if err != nil {
		return resp.StatusCode, err
	}
	if len(raw) > 1<<20 {
		return resp.StatusCode, errors.New("response exceeds 1 MiB")
	}
	if resp.StatusCode != 200 {
		return resp.StatusCode, fmt.Errorf("HTTP %d: %.512s", resp.StatusCode, raw)
	}
	var decoded struct {
		Timings  *serverTimings `json:"timings"`
		Hits     []hit          `json:"hits"`
		Reranked []hit          `json:"reranked"`
		Hybrid   []hit          `json:"hybrid"`
	}
	if err = json.Unmarshal(raw, &decoded); err != nil {
		return resp.StatusCode, err
	}
	if len(capture) > 0 && decoded.Timings != nil {
		*capture[0] = *decoded.Timings
		capture[0].Present = true
	}
	hits := decoded.Hits
	if !dense {
		hits = decoded.Reranked
		if len(decoded.Hybrid) == 0 || len(decoded.Hybrid) > 50 {
			return 200, errors.New("invalid fusion output")
		}
	}
	if len(hits) == 0 || (dense && len(hits) > 100) || (!dense && len(hits) > 10) {
		return 200, errors.New("invalid hit count")
	}
	seen := make(map[string]bool, len(hits))
	for _, h := range hits {
		if h.ID == "" || seen[h.ID] || math.IsNaN(h.Score) || math.IsInf(h.Score, 0) {
			return 200, errors.New("invalid hit identity/score")
		}
		seen[h.ID] = true
		if !dense && (h.Text == "" || h.Metadata.ID != h.ID) {
			return 200, errors.New("missing reranked content")
		}
	}
	return 200, nil
}
func run(url string, dense bool, queries []query, n, workers int, timeout time.Duration, dir string) (result, error) {
	// New connections per request prevent 16 accepted keepalive connections from
	// monopolizing the bounded server. Connection establishment is timed.
	transport := &http.Transport{DisableKeepAlives: true, MaxConnsPerHost: workers, MaxIdleConns: workers, ResponseHeaderTimeout: timeout}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: timeout}
	for i := 0; i < 5; i++ {
		if _, err := request(context.Background(), client, url, queries[i%len(queries)], dense); err != nil {
			return result{}, fmt.Errorf("warmup: %w", err)
		}
	}
	name := "hybrid"
	if dense {
		name = "dense"
	}
	file, err := os.Create(filepath.Join(dir, name+"-requests.csv"))
	if err != nil {
		return result{}, err
	}
	defer file.Close()
	writer := csv.NewWriter(file)
	if err = writer.Write([]string{"request", "worker", "query_id", "status", "elapsed_ns", "error", "engine_admission_seconds", "sparse_seconds", "dense_roundtrip_seconds", "fusion_seconds", "rerank_roundtrip_seconds", "engine_total_seconds"}); err != nil {
		return result{}, err
	}
	results := make(chan observation, workers)
	startGate := make(chan struct{})
	var group sync.WaitGroup
	var next, active, peak atomic.Int64
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	for w := 0; w < workers; w++ {
		group.Add(1)
		go func(worker int) {
			defer group.Done()
			<-startGate
			for {
				id := int(next.Add(1)) - 1
				if id >= n {
					return
				}
				begin := time.Now()
				a := active.Add(1)
				for {
					p := peak.Load()
					if a <= p || peak.CompareAndSwap(p, a) {
						break
					}
				}
				var timings serverTimings
				status, e := request(ctx, client, url, queries[id%len(queries)], dense, &timings)
				elapsed := time.Since(begin).Nanoseconds()
				active.Add(-1)
				o := observation{ID: id, Worker: worker, Status: status, NS: elapsed, Timings: timings}
				if e != nil {
					o.Error = e.Error()
				}
				results <- o
			}
		}(w)
	}
	started := time.Now()
	close(startGate)
	go func() { group.Wait(); close(results) }()
	all := make([]int64, 0, n)
	success := make([]int64, 0, n)
	r := result{Endpoint: url, Requests: n, Workers: workers, Statuses: make(map[string]int)}
	var writeErr error
	for o := range results {
		all = append(all, o.NS)
		r.Statuses[strconv.Itoa(o.Status)]++
		if o.Error == "" {
			r.Success++
			success = append(success, o.NS)
		} else {
			r.Failures++
		}
		if e := writer.Write([]string{strconv.Itoa(o.ID), strconv.Itoa(o.Worker), queries[o.ID%len(queries)].ID, strconv.Itoa(o.Status), strconv.FormatInt(o.NS, 10), o.Error, timingCell(o.Timings, o.Timings.Admission), timingCell(o.Timings, o.Timings.Sparse), timingCell(o.Timings, o.Timings.Dense), timingCell(o.Timings, o.Timings.Fusion), timingCell(o.Timings, o.Timings.Rerank), timingCell(o.Timings, o.Timings.Total)}); e != nil && writeErr == nil {
			writeErr = e
			cancel()
		}
	}
	r.Seconds = time.Since(started).Seconds()
	writer.Flush()
	if writeErr != nil {
		return r, writeErr
	}
	if err = writer.Error(); err != nil {
		return r, err
	}
	if len(all) != n {
		return r, errors.New("incomplete observation count")
	}
	r.Peak = peak.Load()
	r.FailureRate = float64(r.Failures) / float64(n)
	r.AttemptQPS = float64(n) / r.Seconds
	r.SuccessQPS = float64(r.Success) / r.Seconds
	r.All = quantiles(all)
	if len(success) > 0 {
		p := quantiles(success)
		r.Successful = &p
	}
	return r, file.Sync()
}
func main() {
	if err := execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
func execute() error {
	scenario := flag.String("scenario", "all", "all, hybrid or dense")
	workers := flag.Int("workers", 50, "persistent closed-loop clients")
	n := flag.Int("requests", 200, "measured attempts per endpoint")
	corpus := flag.String("queries", "data/phase2/queries.jsonl", "fixed genuine benchmark queries")
	output := flag.String("output", "bench_kit/results/phase4/load", "fresh result directory")
	search := flag.String("search", "http://127.0.0.1:8765/search", "hybrid endpoint")
	dense := flag.String("dense", "http://127.0.0.1:8766/dense", "PQ8 endpoint")
	timeout := flag.Duration("timeout", 180*time.Second, "total client request deadline")
	flag.Parse()
	if *scenario != "all" && *scenario != "hybrid" && *scenario != "dense" {
		return errors.New("scenario must be all, hybrid or dense")
	}
	if *workers < 1 || *workers > 256 || *n < *workers || *n > 1000000 || *timeout <= 0 {
		return errors.New("invalid load limits")
	}
	if _, err := os.Stat(*output); !os.IsNotExist(err) {
		return errors.New("output must be a fresh directory")
	}
	if err := os.MkdirAll(*output, 0755); err != nil {
		return err
	}
	file, err := os.Open(*corpus)
	if err != nil {
		return err
	}
	defer file.Close()
	hash := sha256.New()
	scanner := bufio.NewScanner(io.TeeReader(file, hash))
	scanner.Buffer(make([]byte, 4096), 16384)
	queries := make([]query, 0, 100)
	for scanner.Scan() {
		var q query
		if err = json.Unmarshal(scanner.Bytes(), &q); err != nil {
			return err
		}
		if strings.TrimSpace(q.Query) == "" || len(q.Query) > 8192 || q.ID == "" {
			return errors.New("invalid query")
		}
		queries = append(queries, q)
		if len(queries) > 1000 {
			return errors.New("query catalog exceeds 1000")
		}
	}
	if err = scanner.Err(); err != nil {
		return err
	}
	if len(queries) == 0 {
		return errors.New("empty workload")
	}
	report := map[string]any{"utc": time.Now().UTC().Format(time.RFC3339Nano), "go": runtime.Version(), "gomaxprocs": runtime.GOMAXPROCS(0), "queries": len(queries), "query_sha256": hex.EncodeToString(hash.Sum(nil)), "warmup_per_endpoint": 5, "timeout_seconds": timeout.Seconds(), "connection_policy": "new TCP connection for each request; connection, queue, full response and content validation timed", "percentiles": "exact empirical nearest-rank ceil(p*N), integer nanoseconds; no interpolation", "load_model": "finite closed-loop, zero think time; not an open-loop rate or soak test", "scenario": *scenario}
	for _, endpoint := range []struct {
		name, url string
		dense     bool
	}{{"hybrid", *search, false}, {"dense", *dense, true}} {
		if *scenario != "all" && endpoint.name != *scenario {
			continue
		}
		r, e := run(endpoint.url, endpoint.dense, queries, *n, *workers, *timeout, *output)
		if e != nil {
			return e
		}
		report[endpoint.name] = r
		b, _ := json.Marshal(r)
		fmt.Println(string(b))
	}
	b, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(*output, "report.json"), append(b, '\n'), 0644)
}
