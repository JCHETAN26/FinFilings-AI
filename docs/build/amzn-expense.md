# AMZN operating-expense fix — runbook and reproduction

Status (November 2, 2026): `full-run-05` of the frozen 30-case suite was independently verified and approved by the regression comparison.
Report: [`bench_kit/results/amzn-expense-20261101/RESULTS.md`](../../bench_kit/results/amzn-expense-20261101/RESULTS.md). Publication is recorded in Git
history; no deployment was performed. Source-informed regression fixtures only — not human gold and not an answer-accuracy score.

This runbook covers what the change set did and how to reproduce its checks. It needs an **authenticated Claude CLI** (the full agent run and the critic contrast make live
Claude calls; the rank diagnostic makes none — it uses the offline embedding and reranker models only) and the **local offline artifacts** (corpus, indexes, models, `models/runtime`). No SEC fetch is required and none is performed; the private SEC
contact used for earlier acquisition is not needed here and must never be printed or committed.

## What changed

| Area | Files | Change |
|---|---|---|
| Fresh-4 context budget | `py/agent/state_machine.py`, `py/agent/models.py` | Top four verified fresh hits are reserved; ≤2 retained nominees use the remaining slots; `retained_evidence_ids` lists what was actually carried; optional `nominated_retained_ids`, `retrieved_ids`, `fresh_verified_ids` trace fields. Backend responses over 10 hits are refused before any critic call (the Plan call has already happened by then). |
| Exact opaque handles | `py/agent/synthesizer.py`, `py/agent/models.py` | Model sees handles (`E01`…), scope metadata and text only; exact dictionary resolution; forged handles dropped; legacy `ground` unchanged. |
| User goal vs search hint | `py/agent/planner.py`, `py/agent/critic.py`, `py/agent/models.py` | Extractive `goal` span (≤400 characters, validated against the prompt); critic payload separates `requested_goal`, `search_query`, `initial_search_query` and `current_scope`; scope-first contract. |
| Narrow stalled-rewrite stop | `py/agent/state_machine.py`, `py/agent/models.py`, `bench_kit/eval_agent.py` | `Hop.proposed_rewrite` and `Hop.terminal_reason`; empty-evidence, non-final, unchanged-rewrite hops stop with an explicit flag. Intentional contract change: at most 2 retries (previously always exhausted). |
| Verifier | `bench_kit/verify_broad_agent.py` | Independent recomputation of the context budget, goal span and constancy, rewrite trace and stall validity, model-call ledger allowlist; legacy traces still verify under the original rules. |
| Diagnostics | `bench_kit/diagnose_ranks.py`, `bench_kit/contrast_critic.py`, `scripts/evaluate-expanded-agent.py` | Bounded rank replay (`--rank-diagnostic`, no LLM) and a two-call critic contrast. |
| Tests | `py/test_context_budget.py`, `py/test_synthesis_handles.py`, `py/test_goal_separation.py`, `py/test_stalled_rewrite.py`, `bench_kit/test_*` | Contract tests with labelled fixtures. |

Go source, the vector index, the reranker, models, corpus, thresholds and the suite were **not** changed. Go `vet`/`test`/`-race` were **not** freshly rerun for this
change set; earlier recorded Go results are historical.

## Profile and pins

* Active v3 inputs: `data/sec-scale-v3-evaluation-20261101` (49,406 passages, SHA-256 `c62207623da1fd592d79248916ece408b5568eeceaafc1c0777a83a518f7a56f`),
  `indexes/sec-scale-v3-20261101/report.json`, `indexes/sparse-sec-scale-v3-20261101`, `indexes/vector-sec-scale-v3-pq128-20261101`.
  CLI defaults are still the parser-v2 paths, so every path below is passed explicitly.
* Suite `bench_kit/suites/broad-validation-v2.json`, pinned SHA-256 `2d87557ba1e776f6489b0174211bde7ec21fa60f9a0544f32352b2608808831d`.
* Serving profile (set by the supervisor; do not change): 4 inference workers × 2 threads, FP32 ONNX reranker batch 1, dense batch 8 / trigger 8 / wait 2 ms,
  Go search admission 8, HTTP admission 64. Agent bounds: ≤5 verified evidence passages, ≤2 retained, ≤2 retries, ≤3 concurrent subqueries, ≤2 concurrent model calls.
* Hardware used for the recorded run: AMD Ryzen 7 5800H, 8 physical / 16 logical CPUs, CPU inference.

## Environment

Run from the repository root. The supervisor sets its own offline environment for the services; export the same values for the test and diagnostic commands.

```bash
export PYTHONPATH="$PWD/models/runtime:$PWD/py:$PWD/bench_kit"
export HF_HOME="$PWD/models/hf-cache" HF_HUB_OFFLINE=1
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2 NUMBA_NUM_THREADS=4
```

## Tests

Recorded result: **81 application tests** and **168 benchmark-contract tests**, all passing
([`python-tests-stall-final.log`](../../bench_kit/results/amzn-expense-20261101/verification/python-tests-stall-final.log),
[`benchmark-tests-stall-final.log`](../../bench_kit/results/amzn-expense-20261101/verification/benchmark-tests-stall-final.log)).
The benchmark log includes deliberate negative-fixture output (for example an invalid-input gate message); the suite result is the final `Ran 168 tests … OK`.
The stalled-rewrite path is exercised by these tests only; the live full run contained no stalled stop.

```bash
python3 -m unittest discover -s py -p 'test_*.py'
python3 -m unittest discover -s bench_kit -p 'test_*.py'
```

## Full 30-case run

Choose an output directory that does not exist, stop if it does, and never overwrite or edit a prior attempt. Do not tune thresholds, cases, models or prompts to change a result.

```bash
OUT="bench_kit/results/amzn-expense-repro-01"
test ! -e "$OUT" || { echo "refusing to reuse $OUT" >&2; exit 1; }
SUITE_SHA="2d87557ba1e776f6489b0174211bde7ec21fa60f9a0544f32352b2608808831d"

python3 scripts/evaluate-expanded-agent.py --out "$OUT" \
  --corpus-dir "data/sec-scale-v3-evaluation-20261101" \
  --ingest-report "indexes/sec-scale-v3-20261101/report.json" \
  --vector-index "indexes/vector-sec-scale-v3-pq128-20261101" \
  --sparse-index "indexes/sparse-sec-scale-v3-20261101" \
  --suite "bench_kit/suites/broad-validation-v2.json" --expected-suite-sha256 "$SUITE_SHA"
```

The supervisor starts the sidecar and the Go bridge on free loopback ports, writes `suite.json`, hashes, health files and logs before the first request, runs the
evaluator, and stops everything it owns. Expect roughly twelve minutes.

## Strict independent verification

The verifier's defaults are the v1 suite and the v2 corpus, so pass the v2 suite, the v3 corpus and the v3 ingest report explicitly. `--strict-code` fails if any recorded
implementation file changed since the run; `--enforce-gates` fails unless every release gate is met. Its output is written to the run directory and is never overwritten.

```bash
python3 bench_kit/verify_broad_agent.py \
  --directory "$OUT" \
  --suite "bench_kit/suites/broad-validation-v2.json" \
  --expected-suite-sha256 "$SUITE_SHA" \
  --corpus "data/sec-scale-v3-evaluation-20261101/chunks.jsonl" \
  --ingest-report "indexes/sec-scale-v3-20261101/report.json" \
  --strict-code --enforce-gates
```

The verifier does not perform the case-by-case no-new-gap comparison with a baseline; compare each case's `evidence_outcome` with the baseline `report.json` yourself
(see the attempt history in the report). It also does not check the full Python suite or Go race/vet. Public-snapshot hygiene is a separate independent check, recorded for this run in [`verification/public-snapshot-check.json`](../../bench_kit/results/amzn-expense-20261101/verification/public-snapshot-check.json).

To confirm the recorded run's raw artifacts are intact, compare the SHA-256 values in
[`verification/full-run-05-sealed-artifacts.json`](../../bench_kit/results/amzn-expense-20261101/verification/full-run-05-sealed-artifacts.json) with the files:

```bash
python3 - <<'PY'
import hashlib, json, pathlib
sealed = json.load(open("bench_kit/results/amzn-expense-20261101/verification/full-run-05-sealed-artifacts.json"))
bad = [p for p, digest in sealed.items() if hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() != digest]
print("mismatched:", bad or "none", "of", len(sealed))
PY
```

## Original AMZN rank diagnostic (no LLM)

Replays the saved real queries of subquery 1 against the real hybrid bridge, sequentially, and reports sparse/dense/RRF/rerank ranks plus production context selection.
The passage ID after `--review-target` is a **review-only** comparison target: it is reported, never sent to retrieval and never used to select anything.

```bash
DIAG="bench_kit/results/amzn-expense-diag-repro-01"
test ! -e "$DIAG" || { echo "refusing to reuse $DIAG" >&2; exit 1; }
python3 scripts/evaluate-expanded-agent.py --out "$DIAG" \
  --corpus-dir "data/sec-scale-v3-evaluation-20261101" \
  --ingest-report "indexes/sec-scale-v3-20261101/report.json" \
  --vector-index "indexes/vector-sec-scale-v3-pq128-20261101" \
  --sparse-index "indexes/sparse-sec-scale-v3-20261101" \
  --rank-diagnostic --replay-subquery 1 \
  --replay "bench_kit/results/post-hpe-full-20261101/run-01/evaluation/audit04.json" \
  --replay "bench_kit/results/parser-v3-adoption-20261101/agent-run-01/evaluation/audit04.json" \
  --review-target "000101872424000008#passage-000064"
```

Output: `rank-diagnostic.json` in `$DIAG` (existing outputs are refused). Recorded results: [before](../../bench_kit/results/amzn-expense-20261101/diagnostic-before/rank-diagnostic.json),
[after](../../bench_kit/results/amzn-expense-20261101/diagnostic-after/rank-diagnostic.json). "Before" is the pre-fix selection; run the command against the current code to see the fresh-4 budget.

## Critic contrast (two real model calls)

Replays one saved hop's source-verified evidence and the same search phrase through the production critic with goal A (a span of the original prompt) and goal B (an explicit extra
requirement). Grades are recorded as returned and never forced; the expected contract is A ≥ 4 and B < 4. The output file is created exclusively.

```bash
CONTRAST="bench_kit/results/amzn-expense-contrast-repro-01.json"
test ! -e "$CONTRAST" || { echo "refusing to reuse $CONTRAST" >&2; exit 1; }
python3 bench_kit/contrast_critic.py \
  --case "bench_kit/results/amzn-expense-20261101/full-run-02/evaluation/audit07.json" \
  --subquery 1 --hop-index -1 \
  --goal-a "operating lease obligations" \
  --goal-b "a year-by-year schedule of future minimum operating lease payments (maturities by year)" \
  --corpus "data/sec-scale-v3-evaluation-20261101/chunks.jsonl" \
  --provenance "bench_kit/results/amzn-expense-20261101/full-run-02/agent-provenance.json" \
  --output "$CONTRAST"
```

Recorded: A scored 5 and B scored 2 in both saved runs ([`contrast-audit07.json`](../../bench_kit/results/amzn-expense-20261101/contrast-audit07.json),
[`contrast-audit07-scope.json`](../../bench_kit/results/amzn-expense-20261101/contrast-audit07-scope.json)). The saved case predates the `goal` field, so the script injects each goal explicitly.
The six-call scope probes ([`scope-replay.json`](../../bench_kit/results/amzn-expense-20261101/scope-replay.json)) were produced by an independent replay harness that is not a script in this
repository; only its raw output is saved.

## Limits

* Single live run per attempt; nondeterministic model; source-informed fixtures; extractive quotes only; no financial calculation.
* Not rerun for this change set: Go race/vet/tests, the 100-query retrieval benchmark and the 50-client load benchmark.
* `full-run-03` and `full-run-04` totals cover completed cases only (failed cases saved no trace); see `verification/attempt-summary.json`.
* Earlier attempts (`full-run-01` … `full-run-04`) and the held post-HPE run failed their gates and are preserved unchanged next to the passing `full-run-05`.
