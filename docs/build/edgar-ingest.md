# Go EDGAR ingestion, section parser, quarter shards and mmap router

Status: implemented and verified on 2026-10-31. Not committed and not signed off; see Known limitations.

## Modules

| File | What it does |
|---|---|
| `pkg/ingest/client.go` | EDGAR client: submissions API (`data.sec.gov/submissions/CIK##########.json`, `recent` block) and Archives document download. It requires a `User-Agent` of the form `<name> <contact email>` and rejects anything else before sending a request. Rate: 0 < r ≤ 10 req/s, default 5. Every attempt, retries included, reserves a start time at least `1/r + 1 ms` after the previous one, and 429/5xx `Retry-After` pushes the schedule back. Retries are capped (default 2, max 4) and happen only before any body bytes are read. Redirects are not followed. Size limits: 128 MiB per document, 32 MiB per index JSON. Downloads stream to an `io.Writer` with SHA-256. |
| `pkg/ingest/parser.go` | Deterministic 10-K section parser. It is a Go port of `py/ingest/sections.py` and uses the same IDs and tags. Pass 1 spools visible block-level text to disk (hidden/iXBRL/script/style removed, `<a href>` lines marked as linked). Pass 2 detects canonical `Item N. Title` headings with a 4-line window, so table-of-contents links never count as headings. It emits only Items 1A, 7 and 8, each tagged `Item 1A` / `Item 7` / `Item 8`. If any of the three is missing, duplicated or out of order, the whole filing is rejected (`ErrSections`) and nothing is emitted. No text is ever emitted untagged or under a guessed section. Uses only the standard library: a bounded streaming HTML scanner (1 MiB per token/block, depth 256, at most 512 heading candidates). |
| `pkg/ingest/pipeline.go` | `RunPipeline`: list filings → download one filing at a time to a disk spool → parse → `index.ShardedWriter` (quarter shard from the filing date) and `passages.jsonl`. Writes `provenance.jsonl`, and writes `report.json` last. Requires a fresh output directory and removes it on failure. `SkipUnparseable` (off by default) records `ErrSections` filings as skipped. |
| `pkg/index/shards.go` | `ShardKey` (`YYYY_Q{1-4}`). `ShardedWriter`: one immutable index per quarter, open shards capped by `MaxOpenShards` (default 16), so accounted buffer memory is at most `MaxOpenShards × MaxBufferBytes`. `shards.json` is published atomically last. `ShardRouter`: opens the listed shards (`syscall.Mmap` through the unchanged `index.Open`), runs two bounded fan-outs per query (per-shard df, then per-shard top-k scoring with union `N`/avgdl/df), and does a stable merge. Admission is bounded by `MaxConcurrentSearches`, per-search goroutines by `Workers`. Corrupt or inconsistent manifests return `ErrCorrupt`. |
| `pkg/index/index.go` | Internal refactor only: `SearchFiltered` now calls `searchFiltered(..., global *collectionStats)`, and `termDFs` was added. The public API is unchanged. |
| `cmd/ingest` | New `-ciks` EDGAR mode (`-user-agent` / `$SEC_USER_AGENT`, `-rate`, `-max-filings`, `-skip-unparseable`). The JSONL mode is unchanged. |

Shard scoring uses union statistics. A router over N shards returns bit-identical scores, and the same order (ties broken by shard order, then index order), as one index over the same documents. `TestShardRouterMatchesSingleIndex` checks this across worker counts, filters and k values. A mutation that scores with per-shard statistics instead fails both that test and the pipeline test.

## Verification (logs in `bench_kit/results/ingest/go-edgar/`)

- `go vet ./...` passes, `gofmt -l .` lists nothing, `go test ./...` passes, and `go test -v -race ./...` passes: 111 PASS, 3 opt-in SKIP, no data races (`go-test-race-v.log`).
- **Synthetic e2e** (`TestPipelineEndToEndSyntheticEDGAR`, generated fixture filings served by a local fake EDGAR):
  - Verified behavior:
    - the User-Agent is present on every request;
    - the 10-Q and excess filings are never fetched;
    - an unparseable filing fails the run (output removed), or is skipped when skipping is enabled;
    - 4 quarter shards are produced;
    - every passage is tagged;
    - TOC, Item 1/1B/7A/9, hidden iXBRL, comment and script text is unsearchable;
    - filtered router scores equal a single-index oracle;
    - RRF fuses over sharded IDs.
  - The second RRF branch is another BM25 query, not embeddings. This tests fusion plumbing only.
- **Rate limit** (local server): 14 concurrent downloads at 10 req/s gave at most 10 request starts in any 1 s window. `Retry-After: 1` was honored, and 404/302/over-limit `Retry-After` responses were not retried.
- **Real filings replay** (`TestRealFilingReplayPipeline`, opt-in): the 4 real 10-K primary documents in `data/sec/` (AAPL FY23, MSFT FY24, NVDA FY24, AMZN FY23), served by the local fake transport, so no network was used.
  - All 443 Go chunks are identical (ID, section, text) to the Python reference parser on the same bytes: Item 1A 179, Item 7 93, Item 8 171.
  - 3 quarter shards (`2023_Q4`, `2024_Q1`, `2024_Q3`), 9 segments.
  - Filtered queries returned only the requested section, and no segment mappings remained after `Close`.
  - Report: `real-replay-report.json`. A run under `-race` is in `real-replay-race.log`.
- **Benchmarks** (synthetic fixtures, allocations tracked; not SEC latency):
  - Single-index search allocations are unchanged from Phase 1: 51, 6 and 0 allocs/op.
  - An A/B run of HEAD against the working tree shows no measurable change (`bench-single-index-head-vs-worktree.txt`).
  - Router over 4 fixture shards: about 32 µs/op, about 8 KB/op, 197 allocs/op (fan-out goroutines plus per-shard hit slices) (`bench-router-vs-single.txt`).

## Known limitations / unresolved

- **No live SEC traffic was sent.** All client behavior was verified against local servers. The first real run should use a real contact address. The `admin@finfilings.ai` string appears only in tests, and the CLI requires the operator to supply one.
- The rate limiter only covers one process. Concurrent processes or machines that share an identity need external coordination.
- Only 10-K and 10-K/A are parsed. The client lists 10-Q filings, but the pipeline rejects them, because 10-Q Part I/II items need their own heading table and tags.
- Only the submissions `recent` block is read. Older pages (`filings.files`) and the RSS/Atom feeds are not implemented.
- NVDA FY24's `Item 8` is a 32-word pointer to Item 15. Financial statements placed under Item 15 are therefore not indexed as `Item 8`. This matches the Python parser.
- Replay filing dates (`replay-manifest.jsonl`) were entered from the public EDGAR listing, not re-fetched. They only choose the quarter shards.
- `model.FilingFilter.Accession` matches `<accession>#passage-` IDs. Section-tagged IDs are `<accession>#item<N>#passage-…`, so accession filtering does not match parser output. This is a pre-existing issue and was left unchanged to preserve the Phase 1/2 API.
- `hybrid.Engine` still takes a single `*index.Index`. Dense+sparse hybrid search over a `ShardRouter` is not wired up.
- The HTML scanner handles the EDGAR subset (no HTML5 implied end tags). It is bounded and fails explicitly on anything outside its limits.
- No claims are made about sub-millisecond merge latency or flat RSS at production scale. Only the fixture micro-benchmark and the 4-filing replay RSS readings above were measured.
