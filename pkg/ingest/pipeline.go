package ingest

import (
	"bufio"
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
	"strings"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

// Pipeline output layout (all under one fresh directory):
//
//	segments/YYYY_Q{1-4}/  immutable quarter shards (index.ShardedWriter)
//	segments/shards.json   shard manifest; open with index.OpenShardRouter
//	passages.jsonl         SECChunk passage store (text for reranking/citations)
//	provenance.jsonl       one record per ingested filing
//	report.json            published last; its presence marks a complete run
const (
	SegmentsDirName  = "segments"
	PassagesName     = "passages.jsonl"
	ProvenanceName   = "provenance.jsonl"
	ReportName       = "report.json"
	maxPipelineCIKs  = 10000
	maxFilingsPerCIK = 64
)

// PipelineOptions configures RunPipeline. Zero values select the defaults.
type PipelineOptions struct {
	// Forms selects listed forms to ingest; default 10-K. Only 10-K and
	// 10-K/A are accepted because the section parser handles only them.
	Forms []model.FormType
	// MaxFilingsPerCompany takes the newest N matching filings per CIK
	// (after the date bounds). Default 1, max 64.
	MaxFilingsPerCompany int
	// FiledSince/FiledBefore optionally bound the filing date
	// (inclusive / exclusive).
	FiledSince, FiledBefore time.Time
	// SkipUnparseable records filings whose sections cannot be verified
	// (ErrSections, before anything is emitted) as skipped instead of
	// failing the run. Default false: fail fast.
	SkipUnparseable bool
	Parser          ParserOptions
	Shards          index.ShardedWriterOptions
	Logger          *slog.Logger
}

// FilingReport records one ingested filing.
type FilingReport struct {
	Ref      FilingRef   `json:"ref"`
	Shard    string      `json:"shard"`
	Download Download    `json:"download"`
	Parse    ParseReport `json:"parse"`
}

// SkippedFiling records a filing excluded by SkipUnparseable.
type SkippedFiling struct {
	Ref    FilingRef `json:"ref"`
	Reason string    `json:"reason"`
}

// PipelineReport summarizes a completed run.
type PipelineReport struct {
	Version           int             `json:"version"`
	Parser            string          `json:"parser"`
	RequestsPerSecond float64         `json:"requests_per_second"`
	Filings           []FilingReport  `json:"filings"`
	Skipped           []SkippedFiling `json:"skipped,omitempty"`
	Chunks            int             `json:"chunks"`
	Shards            []string        `json:"shards"`
	Sections          map[string]int  `json:"sections"`
}

// RunPipeline lists each CIK's filings, then for one filing at a time
// streams the primary document to a disk spool, parses Items 1A/7/8, and
// appends every tagged chunk to its filing-quarter shard and to the passage
// store. Memory per filing is bounded by the parser limits; the passage
// collection is never held in memory. out must not exist; on any failure it
// is removed.
func RunPipeline(ctx context.Context, client *Client, ciks []string, out string, opts PipelineOptions) (report PipelineReport, err error) {
	if client == nil || len(ciks) == 0 || len(ciks) > maxPipelineCIKs {
		return report, errors.New("ingest: pipeline needs a client and 1..10000 CIKs")
	}
	if len(opts.Forms) == 0 {
		opts.Forms = []model.FormType{model.Form10K}
	}
	for _, f := range opts.Forms {
		if f != model.Form10K && f != model.Form10KA {
			return report, fmt.Errorf("%w: pipeline form %q", ErrUnsupportedForm, f)
		}
	}
	if opts.MaxFilingsPerCompany == 0 {
		opts.MaxFilingsPerCompany = 1
	}
	if opts.MaxFilingsPerCompany < 1 || opts.MaxFilingsPerCompany > maxFilingsPerCIK {
		return report, errors.New("ingest: MaxFilingsPerCompany must be 1..64")
	}
	if opts.Logger == nil {
		opts.Logger = slog.New(slog.NewJSONHandler(io.Discard, nil))
	}
	if opts.Parser.Logger == nil {
		opts.Parser.Logger = opts.Logger
	}
	if err := os.MkdirAll(filepath.Dir(out), 0o755); err != nil {
		return report, err
	}
	if err := os.Mkdir(out, 0o755); err != nil {
		return report, fmt.Errorf("ingest: fresh pipeline output required: %w", err)
	}
	published := false
	defer func() {
		if !published {
			err = errors.Join(err, os.RemoveAll(out))
		}
	}()
	work := filepath.Join(out, ".work")
	if err := os.Mkdir(work, 0o700); err != nil {
		return report, err
	}
	if opts.Parser.SpoolDir == "" {
		opts.Parser.SpoolDir = work
	}
	parser, err := NewParser(opts.Parser)
	if err != nil {
		return report, err
	}
	writer, err := index.NewShardedWriter(filepath.Join(out, SegmentsDirName), opts.Shards)
	if err != nil {
		return report, err
	}
	writerOpen := true
	defer func() {
		if writerOpen {
			err = errors.Join(err, writer.Abort())
		}
	}()
	passages, err := newSyncedJSONL(filepath.Join(out, PassagesName))
	if err != nil {
		return report, err
	}
	defer func() { err = errors.Join(err, passages.close()) }()
	provenance, err := newSyncedJSONL(filepath.Join(out, ProvenanceName))
	if err != nil {
		return report, err
	}
	defer func() { err = errors.Join(err, provenance.close()) }()

	report = PipelineReport{Version: 1, Parser: ParserVersion, RequestsPerSecond: client.opts.RequestsPerSecond, Sections: map[string]int{}}
	seen := map[string]bool{}
	shards := map[string]bool{}
	for _, cik := range ciks {
		refs, err := client.RecentFilings(ctx, cik, opts.Forms...)
		if err != nil {
			return report, err
		}
		taken := 0
		for _, ref := range refs {
			if taken == opts.MaxFilingsPerCompany {
				break
			}
			if (!opts.FiledSince.IsZero() && ref.FilingDate.Before(opts.FiledSince)) || (!opts.FiledBefore.IsZero() && !ref.FilingDate.Before(opts.FiledBefore)) {
				continue
			}
			if seen[ref.Accession] {
				return report, fmt.Errorf("ingest: accession %s listed twice", ref.Accession)
			}
			seen[ref.Accession] = true
			taken++
			fr, skipped, err := ingestFiling(ctx, client, parser, writer, passages, work, ref)
			if err != nil {
				if opts.SkipUnparseable && skipped {
					report.Skipped = append(report.Skipped, SkippedFiling{Ref: ref, Reason: err.Error()})
					opts.Logger.Warn("filing_skipped", "accession", ref.Accession, "error", err)
					continue
				}
				return report, fmt.Errorf("ingest: filing %s: %w", ref.Accession, err)
			}
			if err := provenance.write(fr); err != nil {
				return report, err
			}
			report.Filings = append(report.Filings, fr)
			report.Chunks += fr.Parse.Chunks
			for item, s := range fr.Parse.Sections {
				report.Sections[item] += s.Chunks
			}
			if !shards[fr.Shard] {
				shards[fr.Shard] = true
				report.Shards = append(report.Shards, fr.Shard)
			}
			opts.Logger.Info("filing_ingested", "accession", ref.Accession, "ticker", ref.Ticker, "form", ref.Form, "shard", fr.Shard, "chunks", fr.Parse.Chunks)
		}
	}
	if report.Chunks == 0 {
		return report, errors.New("ingest: no filings ingested")
	}
	writerOpen = false
	if err := writer.Close(); err != nil {
		return report, err
	}
	for _, f := range []*syncedJSONL{passages, provenance} {
		if err := f.commit(); err != nil {
			return report, err
		}
	}
	// Fingerprint the exact passage stream consumed by embedding ingestion.
	if err := writeSynced(filepath.Join(out, SegmentsDirName, "CORPUS_SHA256"), []byte(hex.EncodeToString(passages.hash.Sum(nil))+"\n")); err != nil {
		return report, err
	}
	if err := syncDirectory(filepath.Join(out, SegmentsDirName)); err != nil {
		return report, err
	}
	if err := os.Remove(work); err != nil {
		return report, err
	}
	data, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return report, err
	}
	tmp := filepath.Join(out, ".report.tmp")
	if err := writeSynced(tmp, append(data, '\n')); err != nil {
		return report, err
	}
	if err := os.Rename(tmp, filepath.Join(out, ReportName)); err != nil {
		return report, err
	}
	if err := errors.Join(syncDirectory(out), syncDirectory(filepath.Dir(out))); err != nil {
		return report, err
	}
	published = true
	opts.Logger.Info("pipeline_complete", "filings", len(report.Filings), "skipped", len(report.Skipped), "chunks", report.Chunks, "shards", len(report.Shards))
	return report, nil
}

// ingestFiling downloads, parses and indexes one filing. skippable is true
// only when the error occurred before any chunk was emitted and was a
// section-verification failure.
func ingestFiling(ctx context.Context, client *Client, parser *Parser, writer *index.ShardedWriter, passages *syncedJSONL, work string, ref FilingRef) (fr FilingReport, skippable bool, err error) {
	raw, err := os.CreateTemp(work, "filing-*.raw")
	if err != nil {
		return fr, false, err
	}
	defer func() { err = errors.Join(err, raw.Close(), os.Remove(raw.Name())) }()
	dl, err := client.Download(ctx, ref, raw)
	if err != nil {
		return fr, false, err
	}
	if _, err := raw.Seek(0, io.SeekStart); err != nil {
		return fr, false, err
	}
	format := FormatHTML
	if strings.HasSuffix(strings.ToLower(ref.PrimaryDocument), ".txt") {
		format = FormatText
	}
	key := index.ShardKeyForDate(ref.FilingDate)
	filing := Filing{Ticker: ref.Ticker, CIK: ref.CIK, Year: ref.FilingDate.Year(), Accession: ref.Accession, Form: ref.Form}
	emitted := 0
	pr, err := parser.Parse(ctx, raw, format, filing, func(c model.SECChunk) error {
		emitted++
		if err := writer.Add(ctx, key, c); err != nil {
			return err
		}
		return passages.write(c)
	})
	if err != nil {
		return fr, emitted == 0 && errors.Is(err, ErrSections), err
	}
	if pr.RawSHA256 != dl.SHA256 || pr.RawBytes != dl.Bytes {
		return fr, false, errors.New("ingest: parsed bytes differ from downloaded bytes")
	}
	return FilingReport{Ref: ref, Shard: key.String(), Download: dl, Parse: pr}, false, nil
}

// syncedJSONL is an exclusive-create, buffered JSON Lines file.
type syncedJSONL struct {
	hash hash.Hash
	f    *os.File
	buf  *bufio.Writer
	enc  *json.Encoder
}

func newSyncedJSONL(path string) (*syncedJSONL, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	buf := bufio.NewWriterSize(f, 64<<10)
	digest := sha256.New()
	enc := json.NewEncoder(io.MultiWriter(buf, digest))
	enc.SetEscapeHTML(false)
	return &syncedJSONL{f: f, buf: buf, enc: enc, hash: digest}, nil
}

func (s *syncedJSONL) write(v any) error { return s.enc.Encode(v) }

func (s *syncedJSONL) commit() error {
	if err := s.buf.Flush(); err != nil {
		return err
	}
	return s.f.Sync()
}

func (s *syncedJSONL) close() error {
	if s.f == nil {
		return nil
	}
	err := s.f.Close()
	s.f = nil
	return err
}
