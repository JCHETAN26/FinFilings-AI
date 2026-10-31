package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"

	"github.com/JCHETAN26/FinFillings-AI/pkg/hybrid"
	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// Protocol fixtures exercise the actual HTTP bridge and mmap router. Dense
// scores here test transport/fusion, not vector retrieval quality.
func TestShardBridgeSearchRerankAndFingerprint(t *testing.T) {
	root := filepath.Join(t.TempDir(), "segments")
	w, err := index.NewShardedWriter(root, index.ShardedWriterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Abort()
	chunks := []model.SECChunk{
		{ID: "000032019323000106#item1A#passage-000000", Ticker: "AAPL", CIK: "320193", FormType: model.Form10K, FilingYear: 2023, SectionItem: "Item 1A", Text: "revenue risk apple"},
		{ID: "000078901923000106#item1A#passage-000000", Ticker: "MSFT", CIK: "789019", FormType: model.Form10K, FilingYear: 2023, SectionItem: "Item 1A", Text: "revenue risk microsoft"},
	}
	var corpus bytes.Buffer
	for i, c := range chunks {
		if err := w.Add(context.Background(), index.ShardKey{Year: 2023, Quarter: i + 1}, c); err != nil {
			t.Fatal(err)
		}
		if err := json.NewEncoder(&corpus).Encode(c); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(corpus.Bytes())
	fingerprint := hex.EncodeToString(digest[:])
	if err := os.WriteFile(filepath.Join(root, "CORPUS_SHA256"), []byte(fingerprint+"\n"), 0644); err != nil {
		t.Fatal(err)
	}
	var mismatch atomic.Bool
	sidecar := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hash := fingerprint
		if mismatch.Load() {
			hash = "different-corpus"
		}
		switch r.URL.Path {
		case "/dense":
			var req struct {
				Query   string             `json:"query"`
				K       int                `json:"k"`
				Filters model.FilingFilter `json:"filters"`
			}
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				t.Error(err)
				http.Error(w, "bad request", 400)
				return
			}
			var hits []hybrid.Hit
			for _, c := range chunks {
				if req.Filters.Matches(c.Meta()) {
					hits = append(hits, hybrid.Hit{ID: c.ID, Score: 1})
				}
			}
			json.NewEncoder(w).Encode(map[string]any{"hits": hits, "corpus_sha256": hash})
		case "/rerank":
			var req struct {
				IDs []string `json:"ids"`
			}
			json.NewDecoder(r.Body).Decode(&req)
			var hits []hybrid.PassageHit
			for _, id := range req.IDs {
				for _, c := range chunks {
					if id == c.ID {
						hits = append(hits, hybrid.PassageHit{Hit: hybrid.Hit{ID: id, Score: 1}, Text: c.Text, Metadata: c.Meta()})
					}
				}
			}
			json.NewEncoder(w).Encode(map[string]any{"hits": hits, "corpus_sha256": hash})
		default:
			http.NotFound(w, r)
		}
	}))
	defer sidecar.Close()
	handler, sparse, err := searchHandler(root, sidecar.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer sparse.Close()
	if _, ok := sparse.(*index.ShardRouter); !ok {
		t.Fatal("bridge did not select shard router")
	}
	server := httptest.NewServer(handler)
	defer server.Close()
	for _, filtered := range []bool{false, true} {
		filter := model.FilingFilter{}
		want := 2
		if filtered {
			filter.Accession = "000032019323000106"
			want = 1
		}
		body, _ := json.Marshal(map[string]any{"query": "revenue risk", "rerank": true, "filters": filter})
		resp, err := http.Post(server.URL+"/search", "application/json", bytes.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		var result hybrid.Result
		err = json.NewDecoder(resp.Body).Decode(&result)
		resp.Body.Close()
		if err != nil || resp.StatusCode != 200 || len(result.Sparse) != want || len(result.Hybrid) != want || len(result.Reranked) != want {
			t.Fatalf("filtered=%v status=%d result=%+v err=%v", filtered, resp.StatusCode, result, err)
		}
	}
	mismatch.Store(true)
	resp, err := http.Post(server.URL+"/search", "application/json", bytes.NewBufferString(`{"query":"risk"}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatal("accepted mismatched dense corpus", resp.StatusCode)
	}
}

func TestSingleIndexBridgeCompatibilityAndFingerprintValidation(t *testing.T) {
	root := t.TempDir()
	corpus := filepath.Join(root, "chunks.jsonl")
	dir := filepath.Join(root, "single")
	chunk := model.SECChunk{ID: "unit-test", Ticker: "AAPL", CIK: "320193", FormType: model.Form10K, FilingYear: 2023, SectionItem: "Item 7", Text: "revenue"}
	raw, err := json.Marshal(chunk)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(corpus, append(raw, '\n'), 0644); err != nil {
		t.Fatal(err)
	}
	if err := build(corpus, dir); err != nil {
		t.Fatal(err)
	}
	_, sparse, err := searchHandler(dir, "http://127.0.0.1:1")
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := sparse.(*index.Index); !ok {
		t.Fatal("single-index compatibility broken")
	}
	sparse.Close()
	if err := os.WriteFile(filepath.Join(dir, "CORPUS_SHA256"), []byte("invalid"), 0644); err != nil {
		t.Fatal(err)
	}
	if _, _, err := searchHandler(dir, "http://127.0.0.1:1"); err == nil {
		t.Fatal("malformed fingerprint accepted")
	}
	if err := os.Remove(filepath.Join(dir, "CORPUS_SHA256")); err != nil {
		t.Fatal(err)
	}
	if _, _, err := searchHandler(dir, "http://127.0.0.1:1"); err == nil {
		t.Fatal("missing fingerprint accepted")
	}
}
