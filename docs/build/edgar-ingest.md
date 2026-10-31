# Go EDGAR ingestion, section parser, quarter shards and mmap router

Status: implemented; see verification evidence and known limitations below.

## Modules

| File | What it does |
|---|---|
| `pkg/ingest/client.go` | EDGAR client: submissions API (`data.sec.gov/submissions/CIK##########.json`, `recent` block) and Archives document download. It requires a `User-Agent` of the form `<name> <contact email>` and rejects anything else before sending a request. Rate: 0 < r ≤ 10 req/s, default 5. Header dispatch is serialized. Every attempt, retries included, waits at least `1/r + 1 ms` after the previous headers complete; 429/5xx `Retry-After` postpones all waiting callers, including after exhausted retries. Retries are capped (default 2, max 4) and happen only before any body bytes are read. Redirects are not followed. Size limits: 128 MiB per document, 32 MiB per index JSON. Downloads stream to an `io.Writer` with SHA-256. |
| `pkg/ingest/parser.go` | Deterministic 10-K section parser (v2, `deterministic-canonical-heading-v2-go`). It began as a Go port of `py/ingest/sections.py` and uses the same IDs and tags. Pass 1 spools visible block-level text to disk (hidden/iXBRL/script/style removed, `<a href>` lines marked as linked unless the link targets an anchor that already appeared, i.e. a heading linking to itself). Pass 2 detects canonical `Item N. Title` headings with a 4-line window, so table-of-contents links never count as headings; a short trailing parenthetical such as `(MD&A)` is ignored, and the title may stand alone on a following line. If Item headings cannot verify all three sections, a titled-heading fallback also accepts standalone exact titles (for annual-report-order filers), and requires Item 8 to end at a later heading. It emits only Items 1A, 7 and 8, each tagged `Item 1A` / `Item 7` / `Item 8`. If any of the three is missing, duplicated or out of order, the whole filing is rejected (`ErrSections`) and nothing is emitted. No text is ever emitted untagged or under a guessed section. `ParseReport.HeadingMode` records `item` or `titled`. Uses only the standard library: a bounded streaming HTML scanner (1 MiB per token/block, depth 256, at most 512 heading candidates, at most 65,536 remembered anchor ids). |
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

- Initial verification used only local servers. The larger corpus run below subsequently exercised live SEC downloads with an operator-supplied real contact address. The `admin@finfilings.ai` string remains confined to tests.
- The rate limiter only covers one process. Concurrent processes or machines that share an identity need external coordination.
- Only 10-K and 10-K/A are parsed. The client lists 10-Q filings, but the pipeline rejects them, because 10-Q Part I/II items need their own heading table and tags.
- Only the submissions `recent` block is read. Older pages (`filings.files`) and the RSS/Atom feeds are not implemented.
- NVDA FY24's `Item 8` is a 32-word pointer to Item 15. Financial statements placed under Item 15 are therefore not indexed as `Item 8`. This matches the Python parser.
- Replay filing dates (`replay-manifest.jsonl`) were entered from the public EDGAR listing, not re-fetched. They only choose the quarter shards.
- Accession predicates in Go metadata, mmap search, Python metadata and SQLite now match the exact `<accession>#` boundary, supporting both original and section-tagged IDs.
- The hybrid engine accepts both `*index.Index` and `*index.ShardRouter`. The HTTP bridge automatically recognizes a quarter-shard root. Dense search and reranking still require a vector index built from the exact same passage bytes; mismatches are rejected.
- The HTML scanner handles the EDGAR subset (no HTML5 implied end tags). It is bounded and fails explicitly on anything outside its limits.
- No claims are made about sub-millisecond merge latency or flat RSS at production scale. Only the fixture micro-benchmark and the 4-filing replay RSS readings above were measured.

## Parser v2: bank and annual-report layouts (2026-10-31)

The live run rejected 53 of 191 filings. Inspecting re-downloaded bank 10-Ks found five layouts:

| Layout | Example filers | v2 result |
|---|---|---|
| Title ends in an abbreviation, `(MD&A)` / `(“MD&A”)` | AXP, PNC, COF, CVS | parsed |
| Company name on the Item line, title on the next line | BAC | parsed |
| Body heading is a link to its own anchor | COF (Item 8), AFL (Item 9) | parsed |
| No `Item N` labels; sections headed by plain titles | MS | parsed (titled fallback) |
| Items only cross-reference page ranges of an embedded annual report | JPM, PYPL (Item 8), C | still rejected |
| Items incorporated by reference from a separate Exhibit 13 | USB, WFC | still rejected |
| Item 7 heading repeated as a glossary entry | TFC | still rejected (duplicate) |

Re-parsing the exact 191 documents (SHA-256 checked against the recorded downloads):
- **Before:** 138 accepted, 53 rejected.
- **v2:** 148 accepted, 43 rejected.
- **Gained (10):** BAC, MS, AXP ×2, PNC ×2, COF ×2, CVS ×2.
- **Lost:** 0.
- **Unchanged:** 136 of the 138 previously accepted filings produce identical sections.
- **Changed (2, both AFL):** these are corrections. Items 1A and 8 now begin with their own heading lines, and Item 8 no longer ends with the self-linked Item 9 heading.

The Go output still equals the Python v1 parser chunk-for-chunk on the four real replay filings and on the synthetic fixture. Morgan Stanley's titled Item 7 stops at a "Cybersecurity" subheading inside MD&A, so about 3,000 MD&A words are not emitted. Nothing is mislabelled.

This is a parser-only check: the expanded index and benchmark were built with v1 and were not rebuilt. Evidence: `bench_kit/results/ingest/parser-v2-20261031/` (`cohort-reparse.json`, `cohort-reparse.log`, test/race/vet logs). Each new rule has a synthetic regression test, and disabling any one of them fails the tests (`pkg/ingest/parser_variants_test.go`).

## Serve quarter shards through hybrid search

New Go pipeline runs write `segments/CORPUS_SHA256`, the SHA-256 of the exact
`passages.jsonl` bytes. Build the vector artifacts from that same passage file,
then point the bridge at the quarter-shard root. The bridge keeps eight admitted
hybrid searches, four shard workers per search, and sixteen live connections.

```bash
PYTHONPATH=py python3 py/embed.py --corpus indexes/edgar/passages.jsonl --out indexes/vector-edgar
PYTHONPATH=py python3 py/service.py --index indexes/vector-edgar --port 8766
# In a separate terminal:
go run ./cmd/finfilings -mode serve -index indexes/edgar/segments -sidecar http://127.0.0.1:8766
```

PQ8 training requires at least 256 actual passages. Old quarter-shard outputs
without a fingerprint must be rebuilt; the bridge refuses missing or malformed
fingerprints. The Python manifest pipeline's `catalog.json` uses a different
layout and is not a quarter-shard root. Agent trusted-source registry enrollment
remains separate from search serving.

## Follow-up: real-model hybrid integration (2026-10-31)

The complete quarter-shard serving path has now been exercised with the actual
pinned BGE model, FAISS PQ8 index, and cross-encoder. Four cached SEC documents
were replayed through the Go ingestion pipeline, then their exact passage stream
was embedded into a new vector index. No SEC network traffic or LLM calls were
made. These artifacts are separate from the Phase 1–4 benchmark indexes.

- **443 passages, 3 quarter shards, 9 binary segments.** The Go/Python parser
  comparison and mmap cleanup checks passed under the race detector before the
  artifacts were exported.
- **13/13 hybrid HTTP cases passed:** four companies × three sections, plus an
  absent accession that correctly returned no results. All result-stage IDs
  satisfied ticker/year/section/accession filters.
- **111/111 reranked passages matched the stored source text and metadata.**
  NVIDIA Item 8 returned its one reference passage; no referenced statements were
  relabeled or invented.
- The passage stream, sparse shards, vector manifest, and both live service
  health responses agreed on corpus SHA-256
  `0c00800fb7d58c0dea7b018b5ace0c15c8eac48dc79a492b1793d0f4d90c3c3b`.
- PQ8 trained on 443 real vectors. This is below the recommended 9,984 training
  vectors for 256-centroid codebooks. This run verifies integration and source
  consistency; it does not establish nDCG, recall, or load-testing performance.
- Both supervised retrieval processes shut down after verification.

[Full report](../../bench_kit/results/ingest/real-hybrid-20261031/report.json)
· [Every query and response](../../bench_kit/results/ingest/real-hybrid-20261031/queries.jsonl)
· [Replay evidence](../../bench_kit/results/ingest/real-hybrid-20261031/replay-report.json)
· [Embedding/model log](../../bench_kit/results/ingest/real-hybrid-20261031/embed.log)
· [Regression suite evidence](../../bench_kit/results/ingest/fixes-20261031/README.md).

Reproduce with fresh directories and the project Python dependencies installed:

```bash
python3 scripts/verify-ingest-hybrid.py \
  --output bench_kit/results/ingest/my-real-hybrid-run \
  --artifacts indexes/my-real-hybrid-run
```

The harness uses offline pinned model snapshots, starts and owns both local
services on temporary ports, checks every response, and saves logs. It requires
Go, the existing cached filings, and the Phase 2 Python dependencies. Source
filing dates come from the replay manifest, not a fresh EDGAR lookup.

Remaining ingestion work includes old submissions-page discovery, explicit
10-Q parsing, and trusted-source registry enrollment for agent audits over
the newly section-tagged corpus. Live SEC ingestion is covered below.

## Larger live SEC corpus (2026-10-31)

Using an operator-supplied real identifying User-Agent, a fixed 100-company
cohort was queried for up to two recent 10-K filings per company at five requests
per second. **138 accepted filings from 71 companies** yielded **26,412 passages
in nine filing-quarter shards**. The deterministic parser rejected 53 filings
whose major sections could not be verified. These were logged explicitly rather
than being silently indexed with guessed section tags. The accepted raw filings
totaled 548,341,779 bytes; that excludes rejected downloads.

[Fixed cohort](../../bench_kit/results/ingest/scale-20261031/cohort.json)
· [Coverage and parsing gaps](../../bench_kit/results/ingest/scale-20261031/ingestion-summary.json)
· [Per-filing source hashes and URLs](../../bench_kit/results/ingest/scale-20261031/ingestion-report.json).

For a controlled comparison, the 26,412 new SEC passages were appended to the
original 19,511-passage benchmark collection, retaining its 100 query records
and published FinQA supporting-fact judgments byte-for-byte: **45,923 retrieval
candidates**. The new SEC passages are not comprehensively relevance-judged;
quality scores describe frozen-judgment retrieval under corpus expansion.
The true-quarter ingestion output remains separate from this mixed benchmark,
which uses its own single sparse index because legacy FinQA snippets have no
verified filing quarters.

Embedding reuse requires checksum-verified artifacts and matching ID, complete
text, model revision, dimension, normalization and token limit. Changed text
is always encoded again. PQ8 codebooks are trained afresh on the expanded
collection; old PQ codes are not reused. Historical Phase 1–4 results remain
unchanged. The larger retrieval and load benchmarks are recorded separately.

The completed expanded benchmark measured reranked nDCG@10 **0.539313**
(previously 0.575744), PQ8/exact float32 ID recall@10 **0.535**, and native
vector-payload reduction **96.3175%**. Both 50-worker HTTP loads completed
200/200 successful requests: hybrid p99 **100.366191 s**, **0.535769 QPS**;
dense p99 **0.911780 s**, **55.477333 QPS**. This is a quality and full-pipeline
latency regression under corpus expansion, despite zero request failures.
All rankings and 400 per-request observations passed independent checks.

[Complete scaled-corpus results and limitations](../../bench_kit/results/ingest/scale-20261031/RESULTS.md)
· [Independent checks](../../bench_kit/results/ingest/scale-20261031/independent-checks.json).
