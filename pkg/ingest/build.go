// Package ingest streams section-tagged SECChunk JSONL into bounded immutable shards.
package ingest

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"log/slog"
	"os"
	"path/filepath"

	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

type Options struct {
	DocsPerShard   int
	DocsPerSegment int
	SegmentBytes   int64
	MaxShards      int
	Logger         *slog.Logger
}

type Shard struct {
	Path         string `json:"path"`
	Documents    int    `json:"documents"`
	Segments     int    `json:"segments"`
	CorpusSHA256 string `json:"corpus_sha256"`
}

type Catalog struct {
	Version      int     `json:"version"`
	Documents    int     `json:"documents"`
	CorpusSHA256 string  `json:"corpus_sha256"`
	Shards       []Shard `json:"shards"`
}

func syncDirectory(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}

func writeSynced(path string, data []byte) error {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
	if err != nil {
		return err
	}
	if _, err = file.Write(data); err != nil {
		return errors.Join(err, file.Close())
	}
	return errors.Join(file.Sync(), file.Close())
}

// Build requires a fresh output path and publishes catalog.json LAST. A failed
// build removes only its newly created output directory. One serial Add path
// gives deterministic segment/doc ordering and natural producer backpressure;
// the writer reuses instance-owned sync.Pool scratch. No document list is kept.
// Open each catalog shard with the unchanged index.Open mmap loader.
func Build(ctx context.Context, input io.Reader, output string, opts Options) (catalog Catalog, err error) {
	if opts.DocsPerShard == 0 {
		opts.DocsPerShard = 2048
	}
	if opts.DocsPerSegment == 0 {
		opts.DocsPerSegment = min(256, opts.DocsPerShard)
	}
	if opts.SegmentBytes == 0 {
		opts.SegmentBytes = 8 << 20
	}
	if opts.MaxShards == 0 {
		opts.MaxShards = 4096
	}
	if opts.DocsPerShard < 1 || opts.DocsPerShard > 1<<20 || opts.DocsPerSegment < 1 || opts.DocsPerSegment > opts.DocsPerShard || opts.SegmentBytes < 4096 || opts.SegmentBytes > 256<<20 || opts.MaxShards < 1 || opts.MaxShards > 65536 {
		return catalog, errors.New("ingest: invalid shard/segment limits")
	}
	if opts.Logger == nil {
		opts.Logger = slog.New(slog.NewJSONHandler(io.Discard, nil))
	}
	if err = os.MkdirAll(filepath.Dir(output), 0755); err != nil {
		return catalog, err
	}
	if err = os.Mkdir(output, 0755); err != nil {
		return catalog, fmt.Errorf("ingest: fresh output required: %w", err)
	}
	published := false
	var writer *index.Writer
	defer func() {
		if writer != nil {
			err = errors.Join(err, writer.Abort())
		}
		if !published {
			err = errors.Join(err, os.RemoveAll(output))
		}
	}()
	store, err := os.OpenFile(filepath.Join(output, "chunks.jsonl"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
	if err != nil {
		return catalog, err
	}
	defer func() {
		if store != nil {
			err = errors.Join(err, store.Close())
		}
	}()
	buffer := bufio.NewWriterSize(store, 64<<10)
	corpusHash := sha256.New()
	var shardHash hash.Hash
	current := Shard{}
	catalog.Version = 1
	finish := func() error {
		if writer == nil {
			return nil
		}
		if closeErr := writer.Close(); closeErr != nil {
			return closeErr
		}
		writer = nil
		current.CorpusSHA256 = hex.EncodeToString(shardHash.Sum(nil))
		shardDir := filepath.Join(output, current.Path)
		if e := writeSynced(filepath.Join(shardDir, "CORPUS_SHA256"), []byte(current.CorpusSHA256+"\n")); e != nil {
			return e
		}
		ix, e := index.Open(shardDir, index.Options{MaxConcurrentSearches: 1})
		if e != nil {
			return e
		}
		stats, e := ix.Stats()
		closeErr := ix.Close()
		if e = errors.Join(e, closeErr); e != nil {
			return e
		}
		if int(stats.Docs) != current.Documents {
			return errors.New("ingest: shard count mismatch")
		}
		current.Segments = stats.Segments
		if e := syncDirectory(shardDir); e != nil {
			return e
		}
		catalog.Shards = append(catalog.Shards, current)
		opts.Logger.Info("shard_published", "path", current.Path, "documents", current.Documents, "segments", current.Segments, "sha256", current.CorpusSHA256)
		return nil
	}
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 65536), 1<<20)
	line := 0
	for scanner.Scan() {
		line++
		if err = ctx.Err(); err != nil {
			return catalog, err
		}
		raw := scanner.Bytes()
		if len(bytes.TrimSpace(raw)) == 0 {
			return catalog, fmt.Errorf("ingest line %d: empty record", line)
		}
		var chunk model.SECChunk
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err = decoder.Decode(&chunk); err != nil {
			return catalog, fmt.Errorf("ingest line %d: %w", line, err)
		}
		var trailing any
		if e := decoder.Decode(&trailing); e != io.EOF {
			return catalog, fmt.Errorf("ingest line %d: expected one JSON object", line)
		}
		if err = chunk.Validate(); err != nil {
			return catalog, fmt.Errorf("ingest line %d: %w", line, err)
		}
		if chunk.FormType != model.Form10K && chunk.FormType != model.Form10KA {
			return catalog, errors.New("ingest: section-tagged flow currently requires 10-K")
		}
		if chunk.SectionItem != "Item 1A" && chunk.SectionItem != "Item 7" && chunk.SectionItem != "Item 8" {
			return catalog, errors.New("ingest: unverified/unsupported section tag")
		}
		if len(chunk.Text) > 32768 || len(chunk.Tokens) > 0 {
			return catalog, errors.New("ingest: require <=32 KiB passage text and no token overrides")
		}
		if writer == nil {
			if len(catalog.Shards) >= opts.MaxShards {
				return catalog, errors.New("ingest: shard catalog limit exceeded")
			}
			current = Shard{Path: fmt.Sprintf("shard-%06d", len(catalog.Shards))}
			shardHash = sha256.New()
			writer, err = index.NewWriter(filepath.Join(output, current.Path), index.WriterOptions{Workers: 1, QueueDepth: 1, MaxBufferBytes: opts.SegmentBytes, MaxDocsPerSegment: opts.DocsPerSegment})
			if err != nil {
				return catalog, err
			}
		}
		if err = writer.Add(ctx, chunk); err != nil {
			return catalog, fmt.Errorf("ingest line %d: %w", line, err)
		}
		if _, err = buffer.Write(raw); err != nil {
			return catalog, err
		}
		if err = buffer.WriteByte('\n'); err != nil {
			return catalog, err
		}
		for _, h := range []hash.Hash{corpusHash, shardHash} {
			if _, err = h.Write(raw); err != nil {
				return catalog, err
			}
			if _, err = h.Write([]byte{'\n'}); err != nil {
				return catalog, err
			}
		}
		current.Documents++
		catalog.Documents++
		if current.Documents == opts.DocsPerShard {
			if err = finish(); err != nil {
				return catalog, err
			}
		}
	}
	if err = scanner.Err(); err != nil {
		return catalog, fmt.Errorf("ingest line %d: %w", line+1, err)
	}
	if err = ctx.Err(); err != nil {
		return catalog, err
	}
	if catalog.Documents == 0 {
		return catalog, errors.New("ingest: empty input")
	}
	if err = finish(); err != nil {
		return catalog, err
	}
	if err = buffer.Flush(); err != nil {
		return catalog, err
	}
	if err = store.Sync(); err != nil {
		return catalog, err
	}
	if err = store.Close(); err != nil {
		store = nil
		return catalog, err
	}
	store = nil
	catalog.CorpusSHA256 = hex.EncodeToString(corpusHash.Sum(nil))
	data, err := json.MarshalIndent(catalog, "", "  ")
	if err != nil {
		return catalog, err
	}
	temporary := filepath.Join(output, ".catalog.tmp")
	if err = writeSynced(temporary, append(data, '\n')); err != nil {
		return catalog, err
	}
	if err = os.Rename(temporary, filepath.Join(output, "catalog.json")); err != nil {
		return catalog, err
	}
	if err = syncDirectory(output); err != nil {
		return catalog, err
	}
	if err = syncDirectory(filepath.Dir(output)); err != nil {
		return catalog, err
	}
	published = true
	opts.Logger.Info("ingestion_complete", "documents", catalog.Documents, "shards", len(catalog.Shards), "sha256", catalog.CorpusSHA256)
	return catalog, nil
}
