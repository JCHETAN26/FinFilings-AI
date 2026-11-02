# Streaming SEC ingestion

This module adds an automated manifest-to-shards path alongside the original benchmark artifacts. It does not overwrite the Phase 1–4 datasets, sparse indexes, vector indexes, or citation registry.

```text
Filing manifest JSONL
  → shared identified EDGAR client (or local raw files)
  → disk-backed visible-text spool
  → deterministic Item 1A / Item 7 / Item 8 selection
  → section-bounded SECChunk JSONL
  → bounded Go writer
  → immutable shards + catalog.json + source provenance
```

## Components and bounds

- `py/ingest/edgar.py`: one shared client per pipeline, default five requests/second, configurable up to ten. It serializes request-header dispatch with a no-burst clock gate and a small timing margin. All redirects and retries pass through that gate. At most two response bodies can download concurrently. HTTPS and SEC hosts are allowlisted by default; automatic redirects are disabled and revalidated. The pipeline also checks archive URL CIK/accession against its filing metadata.
- A project/contact-email User-Agent is required for downloads. Requests use identity encoding; the client requires a complete HTTP 200 response, checks advertised length, caps raw data at 128 MiB by default, and computes SHA-256 while streaming 32-KiB blocks. Exclusive staging files, fsync, and rename prevent partial cache publication. Existing destinations are refused.
- HTTP 429/500/502/503/504 receive at most two retries by default, exponential backoff, and shared `Retry-After` cooldown. HTTP 403 and other permanent errors stop with explicit messages. Retry and redirect counts, statuses, completed byte counts and hashes are logged. No access-control bypass is attempted.
- SEC's published limit is aggregate across a user's traffic, including multiple machines. Reuse the client and coordinate other processes/hosts so their combined rate stays within the limit. This process-local limiter cannot police unrelated clients. [SEC fair-access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data).
- `py/ingest/sections.py`: reads raw UTF-8 in 64-KiB blocks, suppresses hidden HTML/inline-XBRL metadata, marks linked table-of-contents text, and spools normalized visible blocks to disk. HTML nesting is capped at 256, visible blocks/unconsumed HTML tokens at 1 MiB, lookahead at four blocks, and heading candidates at 512. Direct parser inputs are capped at 128 MiB by default. Plain-text input is also supported.
- Heading selection requires canonical titles, not arbitrary mentions of an item number. It handles split block headings, curly apostrophes, repeated same-item page headings, and short Item 8 incorporation/reference sections. It excludes Item 7A from Item 7, ends each section at the next recognized item, and refuses missing, ambiguous, or out-of-order major sections. Short reference text stays in its actual section; the parser does not follow references and relabel other sections.
- Chunks contain at most 256 whitespace words by default, never cross section boundaries, and have a 32-KiB text budget. IDs are deterministic: `<18-digit-accession>#item1A#passage-000000`. `SectionItem` is exactly `Item 1A`, `Item 7`, or `Item 8`; filing year comes from the manifest and is not inferred from fiscal-period dates.
- `py/ingest/pipeline.py`: streams a JSONL filing manifest, uses a disk-backed SQLite primary key to reject duplicate accessions, and processes one filing at a time. Only one raw filing, its spool, and its parsed passage file are staged at a time. Passage bytes flow through a pipe to the Go writer, providing backpressure. A fresh staged build is renamed to the final output only after every filing and shard succeeds. A failure cleans up the owned staging directory and publishes no partial final root.
- `pkg/ingest.Build` / `cmd/ingest`: stream validated JSONL into the existing PFOR/mmap writer. Defaults: 2,048 documents per shard, up to 256 documents per segment, 8 MiB accounted segment budget, one serial writer, and at most 4,096 shard-catalog entries. The serial Add path reuses the writer's instance-owned pools and preserves source order without launching extra analysis workers. Catalog metadata is bounded; no document collection or corpus-sized ID map is retained.

## Input manifest

One filing per line; supply exactly one of `input` or `url`. Local paths resolve relative to the current working directory. `cik` and `accession` are strings; `year` is the calendar filing year. This path currently accepts 10-K / 10-K/A, not 10-Q headings.

```json
{"ticker":"MSFT","cik":"789019","year":2024,"accession":"000095017024087843","input":"data/sec/MSFT-000095017024087843.html","format":"html"}
```

For automated downloads, replace `input` with an archive URL:

```json
{"ticker":"MSFT","cik":"789019","year":2024,"accession":"000095017024087843","url":"https://www.sec.gov/Archives/edgar/data/789019/000095017024087843/msft-20240630.htm","format":"html"}
```

Additional supported fields: `form` (default `10-K`) and `format` (`html` by default; `text` for plain raw text). Unknown fields, duplicate accessions, invalid identities, oversized input and unsupported layouts are errors. Source metadata in local manifests must be trusted/correct; this is not an EDGAR submission-discovery service.

## Run

The ingestion path uses Go and the Python standard library; transformer dependencies are not needed for ingestion itself. From the repository root:

```bash
mkdir -p models/bin
go build -o models/bin/ingest ./cmd/ingest

# Create filings.jsonl with your actual filing metadata and local paths or URLs.
# Required when any row uses a URL: project identifier + valid contact email.
export SEC_USER_AGENT='YourProject your.email@example.com'

PYTHONPATH=py python3 -m ingest.pipeline \
  --manifest filings.jsonl \
  --output indexes/sectioned-production \
  --rate 5 \
  --docs-per-shard 2048 \
  --docs-per-segment 256 \
  --segment-mib 8
```

Use a fresh output path. For direct integration with another producer, pass section-tagged `SECChunk` records to Go:

```bash
./models/bin/ingest \
  -input sectioned-chunks.jsonl \
  -output indexes/sectioned-direct
```

The public Go builder inherits the index writer's assumption that upstream IDs are unique. The Python coordinator enforces unique filing accessions and its parser generates unique per-section IDs. An alternate producer must enforce uniqueness itself.

## Output and loader compatibility

```text
sectioned-production/
  catalog.json            # published shard paths/counts/fingerprints
  chunks.jsonl            # disk-backed full passage text and metadata
  provenance.jsonl        # per-filing source SHA and parser section ranges
  ingestion.json          # pipeline configuration and totals
  ingest.log              # structured Go shard publication logs
  shard-000000/
    MANIFEST               # actual filename is defined by index.ManifestName
    seg-000000.ffseg        # existing immutable binary segment format
    CORPUS_SHA256
  shard-000001/...
```

Shard file names are produced by the existing writer; consult each shard's manifest. Open one shard with the unchanged loader:

```go
ix, err := index.Open("indexes/sectioned-production/shard-000000", index.Options{
    MaxConcurrentSearches: 8,
})
if err != nil {
    return err
}
defer ix.Close()
```

`catalog.json` is a new ingestion catalog, not the existing single-index manifest. Each shard independently opens with `index.Open`; the test harness verifies this. Root `chunks.jsonl` can feed later embedding ingestion. A global shard query coordinator, matching vector partitioning, and enrollment into the audit agent's trusted accession registry are separate integration work. Pointing the existing single-index HTTP service at the shard root is not supported. Existing corpus-fingerprint checks remain enforced.

## Verification and observed results

Actual cached filings were used, without new SEC network requests for these measurements. Client pacing/retry/header/redirect behavior was verified with a real loopback HTTP server and explicitly labeled protocol fixtures.

| Filing | Raw source bytes | Item 1A chunks | Item 7 chunks | Item 8 chunks |
|---|---:|---:|---:|---:|
| AAPL 2023 | 1,559,034 | 39 | 10 | 34 |
| MSFT 2024 | 6,861,021 | 43 | 31 | 67 |
| NVDA 2024 | 2,085,676 | 63 | 24 | 1 |
| AMZN 2024 | 1,945,288 | 34 | 28 | 69 |

Total: **443 section-tagged passages, seven shards, 28 immutable binary segments**, using 64 documents per shard and 16 per segment for the verification run. NVIDIA's Item 8 is a 32-word reference section; its referenced financial statements are not silently relabeled.

The real multi-megabyte test replays the **6,861,021-byte Microsoft 10-K** with verified SHA-256 `ee55571f4ec7f56874706add491553a10c701ad6e355532dcc736debd5ccfcdf`:

- Six Python parse/cleanup cycles produced byte-identical JSONL with 141 correctly tagged passages. File descriptors stayed at four. Traced retained memory remained bounded; peak traced allocations were approximately 1.39 MB.
- Six Go build/mmap/search/close/GC cycles each produced five shards, checked all three section filters, and reopened the original binary format. File descriptors stayed at six, goroutines at two, and no segment mappings remained after Close. Retained Go heap grew from 419,496 to 421,656 bytes, including the retained observation records.
- These finite checks found no observed descriptor, mapping or goroutine leaks and enforce retained-heap growth tolerances (1 MiB Python / 8 MiB Go). RSS can retain allocator pages; this does not prove indefinite leak freedom or production-scale memory capacity.

[Raw per-filing provenance](../../bench_kit/results/ingest/provenance.jsonl) · [Shard catalog](../../bench_kit/results/ingest/catalog.json) · [Real parser report](../../bench_kit/results/ingest/real/report.json) · [Go resource replay](../../bench_kit/results/ingest/real/go-memory.json).

Reproduce after acquiring the original Microsoft filing:

```bash
PYTHONPATH=py python3 -m unittest discover -s py -p test_ingest.py
go test ./...
go test -race ./...
go test -tags=purego ./...
go vet ./...

# Choose a fresh report directory.
python3 bench_kit/eval_ingest.py --output bench_kit/results/ingest/local-real
```

`TestRealFilingRepeatedIngestion` is opt-in through `FINFILINGS_REAL_CHUNKS`; the evaluation script sets it to the parsed real file. Ordinary Go tests use explicitly labeled synthetic unit fixtures. The durable-build allocation benchmark is fixture-only and is excluded from SEC throughput claims.

## Remaining scope

The parser is deterministic and fail-closed, not a universal HTML/financial-document interpreter. Unrecognized heading variants, severely malformed markup, scanned PDFs, non-UTF-8 sources, and long lines/tokens exceeding configured bounds are rejected for review. Batches fail atomically rather than silently skipping bad filings. Submission discovery, cross-machine request-budget coordination, resumable distributed scheduling, reference expansion, shard-wide query serving, and automatic audit-registry enrollment are not implemented by this scaffold.

## Related: Go SEC parser v3

This page documents the Python manifest pipeline. The separate Go EDGAR parser (`pkg/ingest/parser.go`) is now `deterministic-canonical-heading-v3-go` for new ingestions: in titled-heading mode it ends Item 7 at a recognised Item 7A heading and ignores backward inferred titles, keeping section identity (no Item 7A-as-Item 7, no Item 15-as-Item 8). Real-source diagnosis, Codex-verified replay and limitations: [`bench_kit/results/parser-boundaries-20261101/RESULTS.md`](../../bench_kit/results/parser-boundaries-20261101/RESULTS.md); revised known-gap note: [`docs/build/edgar-ingest.md`](../build/edgar-ingest.md). Existing corpora were not rebuilt in place; a separately versioned, targeted Morgan Stanley replacement corpus and its runbook are in [`docs/build/parser-v3-adoption.md`](../build/parser-v3-adoption.md) (validated: gates pass, 30/30 agent cases verified; commit/push pending — [results with all 30 cases](../../bench_kit/results/parser-v3-adoption-20261101/RESULTS.md)). CLI defaults still select v2.
