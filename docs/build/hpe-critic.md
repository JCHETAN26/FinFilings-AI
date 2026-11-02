# HPE critic follow-up — focused runbook

Status (November 1, 2026): targeted check complete and independently verified by Codex. Report: [`bench_kit/results/hpe-critic-20261101/RESULTS.md`](../../bench_kit/results/hpe-critic-20261101/RESULTS.md). Publication recorded in Git history; no deployment performed.

Scope: a critic-only change (`py/agent/critic.py`) checked with two fresh HPE replays plus an absent-section and a mixed-availability control. Not a 30-case rerun, not a semantic-accuracy measure, and retrieval-quality/load benchmarks were intentionally not repeated. CLI defaults remain parser-v2; the v3 paths below are passed explicitly.

## Profile and pins

* Active v3 inputs: `data/sec-scale-v3-evaluation-20261101`, `indexes/sec-scale-v3-20261101/report.json`, `indexes/sparse-sec-scale-v3-20261101`, `indexes/vector-sec-scale-v3-pq128-20261101`.
* Suite `bench_kit/suites/broad-validation-v2.json`, pinned sha256 `2d87557ba1e776f6489b0174211bde7ec21fa60f9a0544f32352b2608808831d` (checked via `--expected-suite-sha256`).
* Frozen serving profile and hard bounds (unchanged): 4 inference workers × 2 threads, ONNX batch 1, dense batch 8 / trigger 8 / wait 2 ms, 8 Go admissions, 64 HTTP admissions; agent ≤5 evidence passages, ≤2 retained, ≤2 rewrites, ≤3 subqueries / concurrent retrievals, ≤2 concurrent model calls.
* Pre-registered plan and input hashes: [`pre-run-plan.json`](../../bench_kit/results/hpe-critic-20261101/pre-run-plan.json).

## Commands

Choose a fresh directory name that does not exist (here `hpe-critic-recheck`; use a different name for any further recheck) and never reuse, overwrite or edit a prior output directory. Check first with `test ! -e bench_kit/results/hpe-critic-recheck` and stop if it exists. Run once for the three cases (the first run) and once for HPE alone:

```bash
SUITE_SHA=2d87557ba1e776f6489b0174211bde7ec21fa60f9a0544f32352b2608808831d
python3 scripts/evaluate-expanded-agent.py --out bench_kit/results/hpe-critic-recheck/run-01 \
  --corpus-dir data/sec-scale-v3-evaluation-20261101 \
  --ingest-report indexes/sec-scale-v3-20261101/report.json \
  --vector-index indexes/vector-sec-scale-v3-pq128-20261101 \
  --sparse-index indexes/sparse-sec-scale-v3-20261101 \
  --suite bench_kit/suites/broad-validation-v2.json --expected-suite-sha256 "$SUITE_SHA" \
  --suite-case broad25 --suite-case broad28 --suite-case broad30
# second replay: same command, --out bench_kit/results/hpe-critic-recheck/run-02, --suite-case broad25 only
```

Strict subset verification of each selected run (the full-suite `--enforce-gates` release gate does not apply to a partial run and is not weakened):

```bash
python3 bench_kit/verify_broad_agent.py \
  --directory bench_kit/results/hpe-critic-recheck/run-01 \
  --suite bench_kit/suites/broad-validation-v2.json \
  --corpus data/sec-scale-v3-evaluation-20261101/chunks.jsonl \
  --ingest-report indexes/sec-scale-v3-20261101/report.json \
  --allow-subset --strict-code
```

`--strict-code` fails if any implementation file changed after the run recorded its hashes; `--allow-subset` accepts exactly the selected cases and reports `full_suite: false`.

## Outputs of the recorded runs

Under `bench_kit/results/hpe-critic-20261101/`: `run-01/` and `run-02/` (each with `evaluation/` JSON/Markdown per case, `records.jsonl`, `run-manifest.json`, `run-status.json`, `artifact-hashes.json`, health before/after, sidecar/bridge/evaluation logs, `independent-broad-verification.json`), `verification/` (Python and benchmark test logs, per-run verification logs, `independent-release-checks.json`) and `pre-run-plan.json`.

## Acceptance (pre-registered)

Both HPE replays cite both planned scopes with no HPE evidence-gap flag; every published citation matches source text and trusted metadata; ≤2 retries per subquery; bounds unchanged; absent-section control publishes zero claims; the mixed control cites its available side and withholds the absent one; no model/index/corpus/suite/threshold/verifier weakening; every attempt preserved. Outcome and numbers: see the report.
