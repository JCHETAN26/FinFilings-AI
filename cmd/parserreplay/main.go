// Command parserreplay is the replay harness for the Morgan Stanley titled-heading
// MD&A boundary fix. It fetches the single recorded filing (fetch) and parses
// identical source bytes with the historical parser-v2 behaviour and the current
// parser (replay), exporting immutable artifacts. See bench_kit/results/parser-boundaries-20261101/.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
)

func execute() error {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if len(os.Args) < 2 {
		return fmt.Errorf("usage: parserreplay fetch|replay [flags]")
	}
	switch os.Args[1] {
	case "fetch":
		fs := flag.NewFlagSet("fetch", flag.ExitOnError)
		dir := fs.String("dir", "data/parser-replay-20261101", "ignored directory for the downloaded raw document")
		rate := fs.Float64("rate", 1, "requests per second (0,1]")
		drift := fs.Bool("allow-drift", false, "if SEC now serves different bytes than the recorded filing, keep them under a labelled UNVERIFIED name instead of discarding")
		if err := fs.Parse(os.Args[2:]); err != nil {
			return err
		}
		out, err := fetchFixture(ctx, defaultFetchConfig(*dir, *rate, *drift))
		if err != nil {
			return err
		}
		return json.NewEncoder(os.Stdout).Encode(out)
	case "replay":
		fs := flag.NewFlagSet("replay", flag.ExitOnError)
		cfg := replayConfig{}
		fs.StringVar(&cfg.source, "source", "data/parser-replay-20261101/ms-20251231.sec-current-NOT-CORPUS-BYTES.htm", "raw Morgan Stanley document")
		fs.StringVar(&cfg.expectSHA256, "expect-sha256", "", "fail unless the source has exactly this SHA-256")
		fs.StringVar(&cfg.corpus, "corpus", "data/sec-scale-v2-evaluation-20261031/chunks.jsonl", "active expanded corpus (read only)")
		fs.StringVar(&cfg.originals, "originals", "data/sec", "directory with the four original filings and provenance.json")
		fs.StringVar(&cfg.indexDir, "index-dir", "indexes/parser-boundaries-20261101", "fresh ignored directory for immutable replay artifacts")
		fs.StringVar(&cfg.resultsDir, "results-dir", "bench_kit/results/parser-boundaries-20261101", "reviewable summary directory")
		if err := fs.Parse(os.Args[2:]); err != nil {
			return err
		}
		if _, err := runReplay(ctx, cfg); err != nil {
			return err
		}
		fmt.Println("replay complete:", cfg.resultsDir+"/replay-summary.json")
		return nil
	default:
		return fmt.Errorf("unknown subcommand %q", os.Args[1])
	}
}

func main() {
	if err := execute(); err != nil {
		fmt.Fprintln(os.Stderr, "parserreplay:", err)
		os.Exit(1)
	}
}
