// Command finfilings builds a real sparse index or serves the hybrid bridge locally.
package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/hybrid"
	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

func build(corpus, dir string) error {
	w, err := index.NewWriter(dir, index.WriterOptions{Workers: 8, QueueDepth: 16, MaxBufferBytes: 8 << 20, MaxDocsPerSegment: 2048})
	if err != nil {
		return err
	}
	defer w.Abort()
	f, err := os.Open(corpus)
	if err != nil {
		return err
	}
	defer f.Close()
	h := sha256.New()
	scanner := bufio.NewScanner(io.TeeReader(f, h))
	scanner.Buffer(make([]byte, 65536), 4<<20)
	count := 0
	for scanner.Scan() {
		var c model.SECChunk
		if err = json.Unmarshal(scanner.Bytes(), &c); err != nil {
			return err
		}
		if err = w.Add(context.Background(), c); err != nil {
			return fmt.Errorf("chunk %d: %w", count, err)
		}
		count++
	}
	if err = scanner.Err(); err != nil {
		return err
	}
	if count == 0 {
		return errors.New("empty corpus")
	}
	if err = w.Close(); err != nil {
		return err
	}
	if err = os.WriteFile(filepath.Join(dir, "CORPUS_SHA256"), []byte(hex.EncodeToString(h.Sum(nil))+"\n"), 0644); err != nil {
		return err
	}
	fmt.Printf("indexed %d real passages at %s\n", count, dir)
	return nil
}

func serve(dir, sidecar, address string) error {
	ix, err := index.Open(dir, index.Options{MaxConcurrentSearches: 8})
	if err != nil {
		return err
	}
	defer ix.Close()
	fingerprint, err := os.ReadFile(filepath.Join(dir, "CORPUS_SHA256"))
	if err != nil {
		return err
	}
	engine, err := hybrid.New(ix, hybrid.Options{SidecarURL: sidecar, MaxConcurrent: 8, RetrieveN: 100, Timeout: 120 * time.Second, CorpusSHA256: stringTrim(fingerprint)})
	if err != nil {
		return err
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"status": "ready", "corpus_sha256": stringTrim(fingerprint)})
	})
	mux.HandleFunc("/search", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST required", http.StatusMethodNotAllowed)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, 16384)
		var request struct {
			Query   string             `json:"query"`
			Rerank  bool               `json:"rerank"`
			Filters model.FilingFilter `json:"filters"`
		}
		decoder := json.NewDecoder(r.Body)
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&request); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		var trailing any
		if err := decoder.Decode(&trailing); err != io.EOF {
			http.Error(w, "expected one JSON request", http.StatusBadRequest)
			return
		}
		result, err := engine.SearchFiltered(r.Context(), request.Query, request.Rerank, request.Filters)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadGateway)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(result)
	})
	server := &http.Server{Addr: address, Handler: mux, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 130 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16384}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return err
	}
	bounded := &boundedListener{Listener: listener, slots: make(chan struct{}, 16), done: make(chan struct{})}
	done := make(chan error, 1)
	go func() { done <- server.Serve(bounded) }()
	fmt.Println("hybrid bridge listening", address)
	select {
	case err = <-done:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 125*time.Second)
	defer cancel()
	if err = server.Shutdown(shutdown); err != nil {
		return err
	}
	return nil
}

func stringTrim(b []byte) string {
	for len(b) > 0 && (b[len(b)-1] == '\n' || b[len(b)-1] == '\r') {
		b = b[:len(b)-1]
	}
	return string(b)
}

func main() {
	mode := flag.String("mode", "serve", "build or serve")
	corpus := flag.String("corpus", "data/phase2/chunks.jsonl", "SECChunk JSONL")
	dir := flag.String("index", "indexes/sparse-phase2", "immutable sparse index")
	sidecar := flag.String("sidecar", "http://127.0.0.1:8766", "local dense/rerank sidecar")
	address := flag.String("listen", "127.0.0.1:8765", "local bridge listener")
	flag.Parse()
	var err error
	switch *mode {
	case "build":
		err = build(*corpus, *dir)
	case "serve":
		err = serve(*dir, *sidecar, *address)
	default:
		err = errors.New("mode must be build or serve")
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
