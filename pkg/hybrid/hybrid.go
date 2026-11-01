// Package hybrid fuses actual sparse and remote PQ8 retrieval under bounded concurrency.
package hybrid

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"reflect"
	"sort"
	"strings"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// Hit carries scores from one ranking stage, independent of score scales in other stages.
type Hit struct {
	ID    string  `json:"id"`
	Score float64 `json:"score"`
}

type PassageHit struct {
	Hit
	Text     string          `json:"text"`
	Metadata model.ChunkMeta `json:"metadata"`
}

// SearchTimings separates engine admission, parallel retrieval and reranking.
// Total excludes TCP acceptance, HTTP body decoding and response transmission.
type SearchTimings struct {
	Admission float64 `json:"admission_seconds"`
	Sparse    float64 `json:"sparse_seconds"`
	Dense     float64 `json:"dense_seconds"`
	Fusion    float64 `json:"fusion_seconds"`
	Rerank    float64 `json:"rerank_seconds"`
	Total     float64 `json:"total_seconds"`
}

type Result struct {
	Timings  *SearchTimings `json:"timings,omitempty"`
	Sparse   []Hit          `json:"sparse"`
	Dense    []Hit          `json:"dense"`
	Hybrid   []Hit          `json:"hybrid"`
	Reranked []PassageHit   `json:"reranked,omitempty"`
}

type Options struct {
	SidecarURL    string
	MaxConcurrent int
	RetrieveN     int
	Timeout       time.Duration
	CorpusSHA256  string
}

// SparseSearcher is implemented by both a single mmap Index and a ShardRouter.
// The caller retains ownership and closes it after searches finish.
type SparseSearcher interface {
	SearchFiltered(context.Context, string, int, model.FilingFilter) ([]index.Hit, error)
}

type Engine struct {
	sparse  SparseSearcher
	options Options
	client  *http.Client
	slots   chan struct{}
}

// New does not take ownership of sparse; its caller closes it after all searches finish.
func New(sparse SparseSearcher, opts Options) (*Engine, error) {
	if sparse == nil || (reflect.ValueOf(sparse).Kind() == reflect.Pointer && reflect.ValueOf(sparse).IsNil()) {
		return nil, errors.New("hybrid: nil sparse index")
	}
	u, err := url.Parse(opts.SidecarURL)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("hybrid: invalid sidecar URL")
	}
	if opts.MaxConcurrent == 0 {
		opts.MaxConcurrent = 8
	}
	if opts.RetrieveN == 0 {
		opts.RetrieveN = 100
	}
	if opts.Timeout == 0 {
		opts.Timeout = 120 * time.Second
	}
	if opts.MaxConcurrent < 1 || opts.MaxConcurrent > 256 || opts.RetrieveN < 50 || opts.RetrieveN > 1000 || opts.Timeout <= 0 {
		return nil, errors.New("hybrid: invalid concurrency/depth/timeout")
	}
	opts.SidecarURL = strings.TrimRight(opts.SidecarURL, "/")
	return &Engine{sparse: sparse, options: opts, client: &http.Client{Timeout: opts.Timeout}, slots: make(chan struct{}, opts.MaxConcurrent)}, nil
}

func validateHits(hits []Hit, limit int) error {
	if len(hits) > limit {
		return errors.New("hybrid: remote hit count exceeds limit")
	}
	seen := make(map[string]bool, len(hits))
	for _, h := range hits {
		if h.ID == "" || len(h.ID) > 256 || math.IsNaN(h.Score) || math.IsInf(h.Score, 0) || seen[h.ID] {
			return errors.New("hybrid: invalid/duplicate hit")
		}
		seen[h.ID] = true
	}
	return nil
}

// RRF uses one-based ranks and k=60. Duplicate IDs within a branch are invalid;
// overlap across branches contributes once per branch. Ties break by ID.
func RRF(sparse, dense []Hit, limit int) ([]Hit, error) {
	if limit < 1 || limit > 50 {
		return nil, errors.New("hybrid: fusion limit must be 1..50")
	}
	if err := validateHits(sparse, 1000); err != nil {
		return nil, err
	}
	if err := validateHits(dense, 1000); err != nil {
		return nil, err
	}
	scores := make(map[string]float64, len(sparse)+len(dense))
	for _, branch := range [][]Hit{sparse, dense} {
		for rank, h := range branch {
			scores[h.ID] += 1 / float64(60+rank+1)
		}
	}
	hits := make([]Hit, 0, len(scores))
	for id, s := range scores {
		hits = append(hits, Hit{id, s})
	}
	sort.Slice(hits, func(i, j int) bool {
		if hits[i].Score == hits[j].Score {
			return hits[i].ID < hits[j].ID
		}
		return hits[i].Score > hits[j].Score
	})
	return hits[:min(limit, len(hits))], nil
}

func (e *Engine) post(ctx context.Context, path string, request, response any) error {
	body, err := json.Marshal(request)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.options.SidecarURL+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := e.client.Do(req)
	if err != nil {
		return fmt.Errorf("hybrid %s: %w", path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("hybrid %s: HTTP %d: %s", path, resp.StatusCode, b)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, (1<<20)+1))
	if err != nil {
		return err
	}
	if len(raw) > 1<<20 {
		return errors.New("hybrid: response exceeds 1 MiB")
	}
	if err = json.Unmarshal(raw, response); err != nil {
		return fmt.Errorf("hybrid: malformed response: %w", err)
	}
	return nil
}

// Search launches exactly two retrieval goroutines per admitted request and
// waits for both before releasing its slot. Cancellation propagates to HTTP
// and sparse search. No goroutines are launched by an unadmitted request.
func (e *Engine) Search(parent context.Context, query string, rerank bool) (Result, error) {
	return e.SearchFiltered(parent, query, rerank, model.FilingFilter{})
}

// SearchFiltered constrains both branches before fusion.
func (e *Engine) SearchFiltered(parent context.Context, query string, rerank bool, filter model.FilingFilter) (Result, error) {
	started := time.Now()
	timing := &SearchTimings{}
	if err := filter.Validate(); err != nil {
		return Result{}, err
	}
	if strings.TrimSpace(query) == "" || len(query) > 8192 {
		return Result{}, errors.New("hybrid: query must be 1..8192 bytes")
	}
	ctx, cancel := context.WithTimeout(parent, e.options.Timeout)
	defer cancel()
	select {
	case e.slots <- struct{}{}:
	case <-ctx.Done():
		return Result{}, ctx.Err()
	}
	defer func() { <-e.slots }()
	timing.Admission = time.Since(started).Seconds()
	type branch struct {
		dense   bool
		hits    []Hit
		err     error
		seconds float64
	}
	results := make(chan branch, 2)
	go func() {
		begin := time.Now()
		hits, err := e.sparse.SearchFiltered(ctx, query, e.options.RetrieveN, filter)
		out := make([]Hit, len(hits))
		for i, h := range hits {
			out[i] = Hit{h.Chunk.ID, h.Score}
		}
		results <- branch{false, out, err, time.Since(begin).Seconds()}
	}()
	go func() {
		begin := time.Now()
		var response struct {
			Hits      []Hit  `json:"hits"`
			CorpusSHA string `json:"corpus_sha256"`
		}
		err := e.post(ctx, "/dense", struct {
			Query   string             `json:"query"`
			K       int                `json:"k"`
			Filters model.FilingFilter `json:"filters"`
		}{query, e.options.RetrieveN, filter}, &response)
		if err == nil && e.options.CorpusSHA256 != "" && response.CorpusSHA != e.options.CorpusSHA256 {
			err = errors.New("hybrid: dense/sparse corpus fingerprint mismatch")
		}
		if err == nil {
			err = validateHits(response.Hits, e.options.RetrieveN)
		}
		results <- branch{true, response.Hits, err, time.Since(begin).Seconds()}
	}()
	var out Result
	var errs []error
	for range 2 {
		b := <-results
		if b.err != nil {
			errs = append(errs, b.err)
			cancel()
		}
		if b.dense {
			out.Dense = b.hits
			timing.Dense = b.seconds
		} else {
			out.Sparse = b.hits
			timing.Sparse = b.seconds
		}
	}
	if err := errors.Join(errs...); err != nil {
		return Result{}, err
	}
	fusionStarted := time.Now()
	fused, err := RRF(out.Sparse, out.Dense, 50)
	if err != nil {
		return Result{}, err
	}
	out.Hybrid = fused
	timing.Fusion = time.Since(fusionStarted).Seconds()
	if rerank && len(fused) > 0 {
		ids := make([]string, len(fused))
		allowed := make(map[string]bool, len(fused))
		for i, h := range fused {
			ids[i] = h.ID
			allowed[h.ID] = true
		}
		var response struct {
			Hits      []PassageHit `json:"hits"`
			CorpusSHA string       `json:"corpus_sha256"`
		}
		rerankStarted := time.Now()
		if err = e.post(ctx, "/rerank", struct {
			Query string   `json:"query"`
			IDs   []string `json:"ids"`
			K     int      `json:"k"`
		}{query, ids, 10}, &response); err != nil {
			return Result{}, err
		}
		if e.options.CorpusSHA256 != "" && response.CorpusSHA != e.options.CorpusSHA256 {
			return Result{}, errors.New("hybrid: reranker corpus mismatch")
		}
		baseHits := make([]Hit, len(response.Hits))
		for i, h := range response.Hits {
			baseHits[i] = h.Hit
			if h.Text == "" || h.Metadata.ID != h.ID || !filter.Matches(h.Metadata) {
				return Result{}, errors.New("hybrid: missing/inconsistent reranked passage")
			}
		}
		if err = validateHits(baseHits, 10); err != nil {
			return Result{}, err
		}
		if len(response.Hits) != min(10, len(fused)) {
			return Result{}, errors.New("hybrid: incomplete reranker output")
		}
		for i, h := range response.Hits {
			if !allowed[h.ID] {
				return Result{}, errors.New("hybrid: reranker returned unknown ID")
			}
			if i > 0 && h.Score > response.Hits[i-1].Score {
				return Result{}, errors.New("hybrid: reranker scores not descending")
			}
		}
		out.Reranked = response.Hits
		timing.Rerank = time.Since(rerankStarted).Seconds()
	}
	timing.Total = time.Since(started).Seconds()
	out.Timings = timing
	return out, nil
}
