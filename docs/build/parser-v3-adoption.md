# Parser v3 adoption — new versioned corpus (tooling and runbook)

Status (November 1, 2026): **adoption validated; all pre-registered gates pass; published as commit `6f18c04` (recorded in Git history); no deployment performed.**

| Stage | State |
|---|---|
| Corpus migration, trusted ingest report, frozen `broad-validation-v2` | done; independently verified (Codex) |
| v3 sparse (49,406) and vector (49,405 reused bit-identically, 1 encoded) indexes | built; independently verified |
| Python (40 application, 136 benchmark-contract), `go vet`, `go test -race` with the real MS file | pass (Codex) |
| Matched 100-query quality + 200-request/endpoint 50-client load, `v2-control` vs `v3-candidate` | done; every nDCG and 400 latencies per run recomputed independently |
| 30-case agent audit `agent-run-01`, strict verifier (`--strict-code --enforce-gates`) | **30/30 verified, 0 failures, 187 citations, 51/52 = 98.08 %**; 26 supported / 1 partial (HPE 2024) / 3 expected-unavailable |
| `compare_v3_gates.py` | OVERALL PASS ([`gates.json`](../../bench_kit/results/parser-v3-adoption-20261101/gates.json)) |
| Commit, push | Published as `6f18c04`; recorded in Git history; no deployment performed |

Full figures, all 30 case rows and caveats: [`bench_kit/results/parser-v3-adoption-20261101/RESULTS.md`](../../bench_kit/results/parser-v3-adoption-20261101/RESULTS.md).
Disclosed: dense-only nDCG@10 fell 0.01497 (the gated fused/reranked metrics did not); the agent suite has no MS case, so it does not establish MS or general parser quality.

CLI defaults remain v2 (every path option defaults to the v2 artifact); v3 is selected only by passing the v3 paths explicitly. No deployment was performed and nothing is
claimed about any running service. This is a mixed-lineage corpus: one filing (MS) was reparsed with parser v3, 147 filings were not reparsed (no new raw files
were obtained for them), and the 13-byte difference between the current and the previously archived MS source is unexplained. It is not "all v3".

## What was built

| Item | Path | Notes |
|---|---|---|
| v3 evaluation data | `data/sec-scale-v3-evaluation-20261101/` | `chunks.jsonl` (49,406 passages), `queries.jsonl`/`qrels.tsv` (byte copies of v2), `provenance.json`, `migration.json` |
| v3 SEC ingestion output | `indexes/sec-scale-v3-20261101/` | `passages.jsonl` (29,895), `report.json` (the trusted ingestion report), `provenance.jsonl` |
| Frozen suite | `bench_kit/suites/broad-validation-v2.json` (+ `.sha256`) | 30 cases carried over unchanged from `broad-validation-v1`, tied to the v3 corpus SHA |
| Migration tool | `bench_kit/adopt_parser_v3.py` (`build`, `verify`) | exclusive create, hash-verified inputs, streaming |
| Gate comparison | `bench_kit/compare_v3_gates.py` | pre-registered thresholds, compares saved run directories only |
| Tests | `bench_kit/test_adopt_parser_v3.py` | synthetic migration fixtures, real-artifact checks, parameterization, gate arithmetic, and the whole existing verifier contract suite re-run against v3 |

The v2 corpus, ingestion output, indexes, `broad-validation-v1` and all earlier evidence are unmodified (checked by hash in the tests).

## Lineage (explicit and mixed)

This is a **targeted replacement**, not a re-ingestion: only Morgan Stanley (`MS`, accession `000089542126000086`) was reparsed with
`deterministic-canonical-heading-v3-go`. The other **147 filings were not reparsed** and keep their parser-v2 output byte for byte, as do
the 19,511 baseline passages. The ingestion report says so: `parser: "mixed"`, `parsers` counts (147 v2, 1 v3) and a `lineage` block.

* Replacement: `indexes/parser-boundaries-20261101-codex/ms-v3/passages.jsonl` (SHA-256 `84df9e8c…90fc`), checked against the
  Codex-verified replay summary.
* Current SEC bytes of the MS filing: 9,714,819 bytes, SHA-256 `3002143b…78b2`. They **differ** from the previously archived acquisition
  (9,714,832 bytes, `57698077…12df`), whose HTML is no longer available. The v3 report records the current identity in `download`/`parse`
  and keeps the previous identity as the first entry of `raw_identity_history`, labelled as history that is not re-verified. Raw-file
  equivalence is not claimed.
* Evidence for equivalence of the indexed text: the parser-v2 replay of the current bytes reproduces all 361 active-corpus MS records
  **byte for byte** (re-checked by the tool). Against that, v3 yields 315 passages: 314 byte-identical, **46 removed**
  (`item7#passage-000081`…`000126`) and **one changed ID** (`000089542126000086#item7#passage-000080`). The removed text is Item 7A risk
  disclosure that v2 had filed under Item 7; no MD&A coverage is gained.
* Corpus: 49,452 → **49,406** passages; corpus SHA-256 `c6220762…7a56f` (v2: `9e7267d5…6c72`).

### Guards in `adopt_parser_v3.py`

Existing output paths are refused (`FileExistsError`, nothing touched); work happens in `*.staging-<pid>` directories that are removed on any
failure; both outputs are renamed into place only after an independent re-derivation passes. It rejects: a raw fixture hash other than the
stated one; a replacement, legacy replay or parse report that differs from the verified replay summary; legacy output that is not
byte-identical to the active corpus; a non-contiguous or duplicated filing run; truncated, oversized (> 4 MiB) or duplicate-ID records;
replacement IDs absent from the active corpus; and any count other than the stated 361 / 315 / 314 / 46 / 1 / 49,452 / 49,406.
Memory is bounded: records are streamed one at a time; the only in-memory sets are the replaced filing's ≤ 4,096 line digests and the 148
report entries; duplicate IDs are tracked in a disk-backed SQLite table.

### Verifier and trusted enrollment

`agent_suite.build_trusted_sources` is unchanged in behaviour: a source's `raw_sha256` is the report's *current* `download.sha256`
(so MS differs from v2 only in that field; the test checks this). `agent_suite.check_report_lineage` is called by the offline verifier and by
the evaluation supervisor: mixed parsers without a lineage declaration, an altered untouched filing, a missing or altered previous raw
identity, an inconsistent current identity, a wrong previous-report hash or an unsafe path are all problems, and
`artifact-hashes.json` must record the same `previous_ingest_report_sha256`. Citation text/metadata/URL/tag, scope, exact-section and bound
checks are untouched; the complete existing verifier contract suite is re-run against the v3 corpus, report and suite
(`VerifierContractsV3`). v2-style runs still verify (the historical `run-01` re-verifies 30/30 offline).

## Commands

Run from the repository root. Use fresh output paths; every builder refuses existing ones.

```bash
export PYTHONPATH="$PWD/models/runtime:$PWD/py" HF_HOME="$PWD/models/hf-cache" HF_HUB_OFFLINE=1 \
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2 NUMBA_NUM_THREADS=4 GOCACHE=/tmp/finfilings-go-cache

# 0. corpus (already built; reproduce only into new paths) and independent re-verification of what is on disk
python3 bench_kit/adopt_parser_v3.py verify
python3 bench_kit/adopt_parser_v3.py build --out-data data/sec-scale-v3-recheck --out-sec indexes/sec-scale-v3-recheck   # optional; compare hashes, then remove

# 1. release prerequisites
python3 -m unittest discover -s py -p 'test_*.py'
python3 -m unittest discover -s bench_kit -p 'test_*.py'
go vet ./... && go test -count=1 ./... && go test -count=1 -race ./...
(cd bench_kit/suites && sha256sum -c broad-validation-v1.sha256 broad-validation-v2.sha256)

# 2. sparse index (single index, same builder and corpus-streaming path as v2)
go build -o models/bin/finfilings ./cmd/finfilings
models/bin/finfilings -mode build -corpus data/sec-scale-v3-evaluation-20261101/chunks.jsonl \
  -index indexes/sparse-sec-scale-v3-20261101

# 3. vectors: reuse the checksum-verified v2 PQ128 embeddings, encode only what changed
#    expected: reused 49405, newly encoded 1 (000089542126000086#item7#passage-000080) — confirmed offline against the v2 store
python3 py/embed.py --corpus data/sec-scale-v3-evaluation-20261101/chunks.jsonl \
  --out indexes/vector-sec-scale-v3-pq128-20261101 \
  --reuse-index indexes/vector-sec-scale-v2-pq128-20261031 \
  --subvectors 128 --train-limit 20000 --train-iterations 25 --threads 2 --batch-size 32
# the PQ seed is fixed in code (20261030); the manifest must show reused_embeddings 49405, newly_encoded_embeddings 1,
# subvectors 128, training_count 20000, training_iterations 25, seed 20261030, and the same model_revision as v2.

# 4. quality + 50-client load: frozen profile (run the identical command for the matched v2 control, with the v2 --data/--vectors/--sparse paths
#    and --out …/v2-control; both runs were executed this way) (4 workers x 2 threads, ONNX batch 1, dense batch 8 / trigger 8 / wait 2 ms)
python3 scripts/benchmark-expanded.py \
  --data data/sec-scale-v3-evaluation-20261101 \
  --vectors indexes/vector-sec-scale-v3-pq128-20261101 \
  --sparse indexes/sparse-sec-scale-v3-20261101 \
  --reranker-onnx models/reranker-onnx-fp32-optimized-20261031 \
  --inference-workers 4 --threads 2 --rerank-batch-size 1 \
  --dense-batch-size 8 --dense-batch-trigger 8 --dense-batch-wait-ms 2 \
  --out bench_kit/results/parser-v3-adoption-20261101/v3-candidate

# 5. agent run: broad-validation-v2 (suite pinned by hash; fresh --out; the old suite and results are not touched)
SUITE_SHA=$(cut -d' ' -f1 bench_kit/suites/broad-validation-v2.sha256)
python3 scripts/evaluate-expanded-agent.py --out bench_kit/results/parser-v3-adoption-20261101/agent-run-01 \
  --corpus-dir data/sec-scale-v3-evaluation-20261101 \
  --ingest-report indexes/sec-scale-v3-20261101/report.json \
  --vector-index indexes/vector-sec-scale-v3-pq128-20261101 \
  --sparse-index indexes/sparse-sec-scale-v3-20261101 \
  --suite bench_kit/suites/broad-validation-v2.json --expected-suite-sha256 "$SUITE_SHA"
python3 bench_kit/verify_broad_agent.py \
  --directory bench_kit/results/parser-v3-adoption-20261101/agent-run-01 \
  --suite bench_kit/suites/broad-validation-v2.json \
  --corpus data/sec-scale-v3-evaluation-20261101/chunks.jsonl \
  --ingest-report indexes/sec-scale-v3-20261101/report.json --strict-code --enforce-gates
# --strict-code fails if any recorded implementation file changed since the run (code hashes are recorded before the first request);
# --enforce-gates also fails unless every release gate is met. Without them, code drift is only reported and gate misses do not change the exit code.

# 6. gates. Baseline = the matched v2 control re-run in this adoption (v2-control, identical profile, re-run in this adoption).
#    The older historical v2 run (dense-batching-20261031/adaptive-b8, hybrid p99 40.08 s) is NOT the comparison: it was a different session.
python3 bench_kit/compare_v3_gates.py \
  --baseline-run bench_kit/results/parser-v3-adoption-20261101/v2-control \
  --candidate-run bench_kit/results/parser-v3-adoption-20261101/v3-candidate \
  --agent-verification bench_kit/results/parser-v3-adoption-20261101/agent-run-01/independent-broad-verification.json \
  --candidate-provenance data/sec-scale-v3-evaluation-20261101/provenance.json \
  --output bench_kit/results/parser-v3-adoption-20261101/gates.json
```

Fixed by code, not by flags: Go engine admission 8, HTTP/inference admission 64 (`--max-pending` default), 50 load clients, 200
requests per endpoint, the agent bounds (2–3 subqueries, ≤ 2 retries, ≤ 5 context passages, ≤ 3 concurrent subqueries, ≤ 2 concurrent
model calls). Pass the same values to the candidate as the baseline used; `compare_v3_gates.py` fails if the two `parameters.json` files
or load query hashes differ, and exits 2 without writing a result if any input holds NaN/Infinity or another non-finite number. Models and revisions are the frozen offline cache. The `vector-sec-scale-v2-pq128-20261031` index is opened
read-only by `--reuse-index` (checksum-verified before use).

If the 50-client run is repeated, keep the first attempt: use a new `--out` and record both. Do not tune settings, queries, judgments or
suite cases to satisfy a gate.

## Pre-registered release gates

Set before any v3 run; implemented as constants in `bench_kit/compare_v3_gates.py` and not read from any candidate output.

1. All Python tests, Go tests, `-race`, `go vet` and source-integrity hashes pass.
2. Same 100 FinQA **development** queries (not held-out); reranked hybrid nDCG@10 ≥ v2 `0.5537183500706869` − 0.005; PQ recall@10 vs exact
   float32 ≥ `0.838` − 0.02; native vector payload reduction ≥ 90 %.
3. Matched 50-client load: identical configuration and queries, zero failures on both endpoints **in the baseline and the candidate**, hybrid
   p99 (all attempts) ≤ 1.10 × the matched `v2-control` value (47.812667999 s as measured by Codex in this adoption). A rerun is justified only by an identified unresolved source of noise; every attempt is preserved.
4. Agent, same 30 cases: every case verified for source/citation/filter/bounds, zero unsupported claims and zero transport/model/structural
   failures, expected-absent scopes withheld, mixed cases fully supported on the available side, ≥ 80 % available-subquery citation coverage.

## Known limitations (unchanged by this work)

* Only MS differs. A v3 result therefore cannot show that parser v3 generalises: no other titled-mode filing has ever been observed, and the
  raw bytes of the other 147 filings were not kept.
* The qrels label only FinQA passages; SEC passages have no judgments, so nDCG/PQ-recall movement from a 46-passage change is expected to
  be tiny and is not evidence about SEC retrieval quality.
* The agent suite is source-informed regression fixtures with one reviewer; coverage is structural, not answer accuracy. Model output varies
  between runs.
* The Item 8 reference stubs and the 43 skipped filings are unrelated to this change and remain open. The HPE 2024 critic gap was open at this baseline (historical); a later targeted critic follow-up is in [`hpe-critic.md`](hpe-critic.md) and [its report](../../bench_kit/results/hpe-critic-20261101/RESULTS.md).
* Hybrid-load results are finite, closed-loop, local observations on a non-isolated machine.
* The MS raw HTML used for the replacement is not the previously archived file (see Lineage).
