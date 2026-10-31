package ingest

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/JCHETAN26/FinFillings-AI/pkg/index"
	"github.com/JCHETAN26/FinFillings-AI/pkg/model"
)

type replayRecord struct {
	Ticker     string `json:"ticker"`
	CIK        string `json:"cik"`
	Accession  string `json:"accession"`
	Form       string `json:"form"`
	FilingDate string `json:"filing_date"`
	Document   string `json:"document"`
	Input      string `json:"input"`
}

// Opt-in replay of REAL downloaded SEC 10-K documents through the Go
// pipeline. The bytes are real EDGAR primary documents; the transport is a
// local fake EDGAR server so no network is used. Verifies: every chunk is
// tagged Item 1A/7/8; Go chunks equal the Python reference parser's chunks
// on the same bytes; quarter shards open under the mmap router; filtered
// queries return only the requested section; mappings are released on
// Close. It does not measure retrieval quality.
//
//	FINFILINGS_REPLAY_MANIFEST=bench_kit/results/ingest/go-edgar/replay-manifest.jsonl
//	FINFILINGS_REPLAY_REPORT=<path for the JSON report>   (optional)
func TestRealFilingReplayPipeline(t *testing.T) {
	manifest := os.Getenv("FINFILINGS_REPLAY_MANIFEST")
	if manifest == "" {
		t.Skip("set FINFILINGS_REPLAY_MANIFEST to a replay manifest of real SEC documents")
	}
	repo, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	if !filepath.IsAbs(manifest) {
		manifest = filepath.Join(repo, manifest)
	}
	f, err := os.Open(manifest)
	if err != nil {
		t.Fatal(err)
	}
	var records []replayRecord
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var r replayRecord
		dec := json.NewDecoder(bytes.NewReader(sc.Bytes()))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&r); err != nil {
			t.Fatal(err)
		}
		records = append(records, r)
	}
	f.Close()

	edgar := &fakeEDGAR{t: t}
	var ciks []string
	for _, r := range records {
		body, err := os.ReadFile(filepath.Join(repo, r.Input))
		if err != nil {
			t.Fatal(err)
		}
		edgar.companies = append(edgar.companies, fakeCompany{CIK: r.CIK, Ticker: r.Ticker, Filings: []fakeFiling{{r.Accession, r.FilingDate, r.Form, r.Document, body}}})
		ciks = append(ciks, r.CIK)
	}
	srv := httptest.NewServer(edgar)
	defer srv.Close()
	client, rt := testClient(t, srv, ClientOptions{})

	rss := func() uint64 {
		data, err := os.ReadFile("/proc/self/statm")
		if err != nil {
			return 0
		}
		var virtual, resident uint64
		fmt.Sscan(string(data), &virtual, &resident)
		return resident * uint64(os.Getpagesize())
	}
	runtime.GC()
	rssBefore := rss()
	out := filepath.Join(t.TempDir(), "edgar")
	began := time.Now()
	report, err := RunPipeline(context.Background(), client, ciks, out, PipelineOptions{
		Shards: index.ShardedWriterOptions{Writer: index.WriterOptions{MaxDocsPerSegment: 64}},
	})
	pipelineSeconds := time.Since(began).Seconds()
	if err != nil {
		t.Fatal(err)
	}
	var memory runtime.MemStats
	runtime.ReadMemStats(&memory)
	rssAfterPipeline := rss()

	passages := readJSONL[model.SECChunk](t, filepath.Join(out, PassagesName))
	for _, c := range passages {
		if c.SectionItem != "Item 1A" && c.SectionItem != "Item 7" && c.SectionItem != "Item 8" {
			t.Fatalf("untagged chunk %s: %q", c.ID, c.SectionItem)
		}
	}

	// Python reference parser on the same bytes.
	pythonMatches := map[string]bool{}
	if python, err := exec.LookPath("python3"); err == nil {
		byAccession := map[string][]model.SECChunk{}
		for _, c := range passages {
			acc := c.ID[:18]
			byAccession[acc] = append(byAccession[acc], c)
		}
		for _, r := range records {
			acc := strings.ReplaceAll(r.Accession, "-", "")
			pyOut := filepath.Join(t.TempDir(), acc+".jsonl")
			year := r.FilingDate[:4]
			script := fmt.Sprintf(`import sys; sys.path.insert(0, sys.argv[3]); from pathlib import Path; from ingest.sections import Filing, SectionParser
SectionParser(256).parse(Path(sys.argv[1]), Path(sys.argv[2]), Filing(%q, %q, %s, %q), 'html')`, r.Ticker, r.CIK, year, acc)
			cmd := exec.Command(python, "-I", "-c", script, filepath.Join(repo, r.Input), pyOut, filepath.Join(repo, "py"))
			cmd.Dir = t.TempDir()
			if msg, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("python reference %s: %v\n%s", r.Ticker, err, msg)
			}
			py := readJSONL[map[string]any](t, pyOut)
			got := byAccession[acc]
			if len(py) != len(got) {
				t.Fatalf("%s: python %d chunks, go %d", r.Ticker, len(py), len(got))
			}
			for i, c := range got {
				if py[i]["ID"] != c.ID || py[i]["SectionItem"] != c.SectionItem || py[i]["Text"] != c.Text {
					t.Fatalf("%s chunk %d differs from python reference", r.Ticker, i)
				}
			}
			pythonMatches[r.Ticker] = true
		}
	}

	root := filepath.Join(out, SegmentsDirName)
	router, err := index.OpenShardRouter(root, index.ShardRouterOptions{})
	if err != nil {
		t.Fatal(err)
	}
	stats, err := router.Stats()
	if err != nil || stats.Docs != uint64(len(passages)) {
		t.Fatal(stats, err)
	}
	sectionHits := map[string]int{}
	for item, query := range map[string]string{"Item 1A": "risk competition supply", "Item 7": "revenue operating income", "Item 8": "balance sheets cash flows"} {
		for _, r := range records {
			hits, err := router.SearchFiltered(context.Background(), query, 10, model.FilingFilter{Ticker: r.Ticker, Section: item})
			if err != nil {
				t.Fatal(err)
			}
			for _, h := range hits {
				if h.Chunk.SectionItem != item || h.Chunk.Ticker != r.Ticker {
					t.Fatalf("filtered hit mismatch %+v", h.Chunk)
				}
			}
			sectionHits[item] += len(hits)
		}
	}
	var shards []string
	for _, s := range router.Shards() {
		shards = append(shards, fmt.Sprintf("%s:%d", s.Key, s.Docs))
	}
	rssRouterOpen := rss()
	if err := router.Close(); err != nil {
		t.Fatal(err)
	}
	if maps, err := os.ReadFile("/proc/self/maps"); err == nil && strings.Contains(string(maps), root) {
		t.Fatal("segment mappings remain after router Close")
	}

	result := map[string]any{
		"scope":                      "REAL SEC 10-K primary documents replayed through a local fake EDGAR transport (no network). Verifies tagging, Go/Python parser agreement, sharding and mmap lifecycle. Not retrieval quality, not network ingestion latency.",
		"manifest":                   strings.TrimPrefix(manifest, repo+"/"),
		"filing_dates_note":          "filing_date values in the manifest only choose quarter shards; they were entered from the public EDGAR listing, not re-fetched in this run",
		"filings":                    len(report.Filings),
		"chunks":                     report.Chunks,
		"sections":                   report.Sections,
		"shards":                     shards,
		"segments":                   stats.Segments,
		"segment_file_bytes":         stats.FileBytes,
		"python_reference_identical": pythonMatches,
		"filtered_hits_by_section":   sectionHits,
		"requests":                   len(rt.sorted()),
		"max_request_starts_in_1s":   maxInWindow(rt.sorted(), time.Second),
		"requests_per_second_limit":  client.opts.RequestsPerSecond,
		"pipeline_wall_seconds":      pipelineSeconds,
		"rss_bytes_before":           rssBefore,
		"rss_bytes_after_pipeline":   rssAfterPipeline,
		"rss_bytes_router_open":      rssRouterOpen,
		"go_heap_alloc_after_bytes":  memory.HeapAlloc,
		"unclosed_segment_mappings":  0,
		"environment":                map[string]any{"go": runtime.Version(), "goos": runtime.GOOS, "goarch": runtime.GOARCH, "gomaxprocs": runtime.GOMAXPROCS(0)},
	}
	for _, fr := range report.Filings {
		result[fr.Ref.Ticker] = map[string]any{"accession": fr.Ref.Accession, "shard": fr.Shard, "raw_sha256": fr.Download.SHA256, "raw_bytes": fr.Download.Bytes, "sections": fr.Parse.Sections, "candidate_headings": fr.Parse.CandidateHeadings}
	}
	data, _ := json.MarshalIndent(result, "", "  ")
	if path := os.Getenv("FINFILINGS_REPLAY_REPORT"); path != "" {
		if err := os.WriteFile(path, append(data, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	t.Log(string(data))
}
