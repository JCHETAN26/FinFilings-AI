// Command ingest writes immutable mmap-compatible shards, either from
// section-tagged SECChunk JSONL (default) or, with -ciks, by fetching 10-K
// filings from SEC EDGAR into quarter shards (see ingest.RunPipeline).
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"github.com/JCHETAN26/FinFillings-AI/pkg/ingest"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
)

func execute() error {
	source := flag.String("input", "-", "section-tagged JSONL, or - for stdin")
	output := flag.String("output", "indexes/ingested", "fresh shard-root directory")
	docs := flag.Int("docs-per-shard", 2048, "bounded documents per shard")
	segmentDocs := flag.Int("docs-per-segment", 0, "documents per binary segment; default min(256, docs-per-shard)")
	segmentMiB := flag.Int64("segment-mib", 8, "accounted segment memory budget")
	ciks := flag.String("ciks", "", "comma-separated CIKs: fetch 10-K filings from EDGAR instead of reading -input")
	userAgent := flag.String("user-agent", os.Getenv("SEC_USER_AGENT"), "EDGAR mode: '<name> <contact email>' (default $SEC_USER_AGENT)")
	rate := flag.Float64("rate", ingest.DefaultRequestsPerSecond, "EDGAR mode: requests per second, at most 10")
	filings := flag.Int("max-filings", 1, "EDGAR mode: newest 10-K filings per CIK (1..64)")
	skip := flag.Bool("skip-unparseable", false, "EDGAR mode: record filings without verifiable Items 1A/7/8 as skipped instead of failing")
	flag.Parse()
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if *ciks != "" {
		if *segmentMiB < 1 || *segmentMiB > 256 {
			return fmt.Errorf("segment-mib must be 1..256")
		}
		logger := slog.New(slog.NewJSONHandler(os.Stderr, nil))
		client, err := ingest.NewClient(ingest.ClientOptions{UserAgent: *userAgent, RequestsPerSecond: *rate})
		if err != nil {
			return err
		}
		report, err := ingest.RunPipeline(ctx, client, strings.Split(*ciks, ","), *output, ingest.PipelineOptions{
			MaxFilingsPerCompany: *filings,
			SkipUnparseable:      *skip,
			Shards:               index.ShardedWriterOptions{Writer: index.WriterOptions{MaxBufferBytes: *segmentMiB << 20, MaxDocsPerSegment: max(*segmentDocs, 0)}},
			Logger:               logger,
		})
		if err != nil {
			return err
		}
		return json.NewEncoder(os.Stdout).Encode(report)
	}
	var input io.Reader = os.Stdin
	if *source != "-" {
		f, err := os.Open(*source)
		if err != nil {
			return err
		}
		defer f.Close()
		input = f
	}
	if *segmentMiB < 1 || *segmentMiB > 256 {
		return fmt.Errorf("segment-mib must be 1..256")
	}
	catalog, err := ingest.Build(ctx, input, *output, ingest.Options{DocsPerShard: *docs, DocsPerSegment: *segmentDocs, SegmentBytes: *segmentMiB << 20, Logger: slog.New(slog.NewJSONHandler(os.Stderr, nil))})
	if err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(catalog)
}
func main() {
	if err := execute(); err != nil {
		slog.Error("ingestion_failed", "error", err)
		os.Exit(1)
	}
}
