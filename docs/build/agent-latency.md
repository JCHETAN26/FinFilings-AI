# Agent latency: fast execution mode

**Status (2026-11-02):** implemented, unit-tested with labeled test doubles, and live-verified by the reviewer (see "Results" below and
[`RESULTS.md`](../../bench_kit/results/audit-latency-20261102/RESULTS.md)). The paired gate passed on its second, fresh run; the first run failed and is
preserved. The full 30-case fast check is a **recovered composite of an interrupted run plus a continuation**, not one uninterrupted run. Unit-test
timings in this repo are not benchmarks; the numbers in "Results" come from the saved live artifacts only. Verification and release gates passed; Git publication is recorded in history and the UI launch command is available. These benchmark figures are not new live UI measurements or a deployment. Latest tests: 192 application / 216 benchmark (including 25 recovery-verifier tests; see
[`benchmark-tests-04.log`](../../bench_kit/results/audit-latency-20261102/verification/benchmark-tests-04.log)). Recovery outputs:
[`full-30-recovery-verification.json`](../../bench_kit/results/audit-latency-20261102/verification/full-30-recovery-verification.json), [`full-30-recovery-verifier.log`](../../bench_kit/results/audit-latency-20261102/verification/full-30-recovery-verifier.log).

## Why

A recorded real UI audit (AMZN, two topics, one critic retry) spent its time on Claude CLI calls: Plan, two critics (parallel), one retried critic,
and a final `HandleDraft`. The final call re-reads passages the critics had just read, only to copy quotes out of them. Each structured call also
costs 2-3 provider turns. Those numbers are the user's observation, not a benchmark.

## Modes (`Agent(..., mode=)`)

| | `legacy` | `fast` (API default) |
|---|---|---|
| Plan | 1 call (`Plan`) | same |
| Per search attempt | `Critique` | `CritiqueWithQuotes` (same grading fields + up to 4 quote proposals) |
| Synthesis | 1 `HandleDraft` call over all accepted passages | **host formatting, no model call**, unless the fallback applies |
| Ledger | `Plan`, `Critique` x hops, `HandleDraft` (0 if nothing accepted) | `Plan`, `CritiqueWithQuotes` x hops, optional 1 `HandleDraft` |
| `synthesis_route` | `legacy` / `none` | `host_quotes` / `fallback` / `none` |

`AgentResult.execution_mode` and `synthesis_route` record what **actually ran**. The ledger holds only real calls. Results saved before this change lack
both keys and are read as legacy. The Synthesize state still runs in fast mode; it is real host formatting.

### How fast mode keeps the guarantees

* The critic's instruction is built by one shared function (`critic.build_instruction`). Fast mode changes only the retention field name/noun
  (`retain_refs`, "refs") and appends the quote and identifier contract. The relevance rubric, scope contract, date and adjacency rules and the
  acceptance threshold are the same text and the same host check. A quote may not rescue a score. Legacy's `Critique` schema, prompt, payload and
  validation are unchanged (a unit test pins them).
* **One identifier namespace (fast mode).** The model sees passages only as `E01`..`E05` in `evidence`, `adjacent_pairs`, `retain_refs` and quote
  `claims`; it is never shown or asked to copy a canonical passage ID. The wire schema `CritiqueWithQuotes` (ledger name unchanged) has `retain_refs`
  (<= 2) and no `retain_ids`. The host resolves each retained handle by exact dictionary key to the canonical ID and builds a separate typed
  `CriticVerdict` (`retain_ids` canonical) that the controller consumes, so existing retention/context logic and hop traces keep canonical IDs.
  An unknown handle (including wrong case or a canonical ID) or a repeated handle rejects the whole verdict: nothing is dropped, guessed or accepted
  as a partial result, and there is no extra retry or model call. The error reports counts only (`N unknown, M duplicate`), never returned values.
* Each passage shown to the fast critic carries a short handle (`E01`..`E05`). Quotes are untrusted: the host resolves the handle by exact dictionary
  lookup (`ground_handles`, reused), requires the whitespace-normalized quote to occur verbatim in that passage, and enforces 40..2000 characters.
  Citation tag, accession and SEC URL come from the verified `Evidence`, never from the model.
* Proposals count only when the hop itself is accepted (`score >= threshold`). Below-threshold proposals are discarded unseen: not recorded, not
  published. Acceptance is never forced by having quotes.
* Invalid proposals (unknown handle, text not in that passage, bad length) are dropped with a bounded flag (`Critic quote (subquery N): ...`) that never
  contains the rejected text. At most 4 claims per hop; at most 12 final citations per job; deduplicated; cap priority is (1) every verified critic quote, (2) one
  fallback representative per goal that has none, (3) remaining fallback quotes; with one gap there are at most 8 critic quotes, so none is ever
  displaced by the fallback.
* **Fallback.** If any *accepted* subquery has zero valid quotes, one existing `HandleDraft` call runs over only those subqueries' accepted contexts
  (<= 15 passages). Its grounded citations are merged with the quotes already verified; valid quotes are never discarded. If a goal still has no
  published quote, the existing "retrieved evidence but no published supporting quote" gap flag is published. At most one extra call; the per-audit maximum
  stays 11. For an identical hop trace, `host_quotes` saves exactly the final synthesis call and `fallback` costs the same as legacy. Each mode plans
  and retries independently, so real runs can differ in queries, retries and call counts; the paired comparison reports the actual counts and there is
  no per-prompt guarantee that fast makes fewer calls. A fallback transport/model failure is raised like any other failure, never replaced by a canned answer.
* Hops record `quote_selections` (host-verified, accepted hops only) and `quote_flags`. Nothing unverified is stored.

### Tradeoffs (stated, not measured)

* The critic call carries a longer instruction and a longer structured output; this may make an individual critic call slower. Whether the removed
  final call outweighs that is exactly what the paired benchmark tests.
* Quotes are chosen when the critic judges a single context, not after seeing all subqueries together. Cross-topic selection is therefore less global
  than the legacy synthesis; the fallback covers a missing goal, not a *weaker* choice. Coverage is checked by the verifier, quote quality is not.
* Fast output is the same extractive format as legacy but typically fewer and more topic-local excerpts.
* No credential or direct-API assumption is made: both modes use the existing tool-disabled Claude CLI provider with its timeouts, size bounds and
  process-group cancellation, unchanged. Concurrency is unchanged (model 2 slots, agent 3, one active UI audit).

## Selecting a mode

| Surface | Default | Switch |
|---|---|---|
| `Agent(...)` API | `fast` | `mode='legacy'` |
| `python -m agent.state_machine` | `fast` | `--agent-mode legacy` |
| UI (`scripts/serve-audit-ui.py`, `python -m audit_web`) | `fast` | `--agent-mode legacy` |
| `bench_kit/eval_agent.py`, `scripts/evaluate-expanded-agent.py` | **`legacy`** (historical commands stay reproducible) | `--agent-mode fast` |

The mode is written to the pre-run manifest, the raw `AgentResult` JSON, per-case records and reports; the verifier cross-checks all three.
The UI shows it under "Run details (technical)". A running UI process keeps the code it imported; restart it to pick up this change.

## Independent verification (`bench_kit/verify_broad_agent.py`)

Legacy traces keep their historic meaning (ledger `Plan`, `Critique` x hops, 0/1 synthesis; the 80% coverage gate is unchanged). Fast traces are
verified separately: allowed schemas `Plan` + `CritiqueWithQuotes` + at most one `HandleDraft`; route must equal what the trace implies (`none` /
`host_quotes` / `fallback`) with exactly the matching call count; quote selections only on accepted hops, verbatim in that hop's own evidence from the
original corpus bytes; flags match the bounded pattern and appear in the published flags; published quotes equal the verified selections (or, on
fallback, lie in the gap contexts); <= 12 citations; no accepted goal loses its verified quotes. Retry/context/retention bounds, scope, source
identity and goal coverage use the shared checks.

## Paired benchmark (reviewer runs; uses live Claude calls)

The frozen `broad-validation-v2` suite is tied to the **v3** corpus; the tools' historical defaults still point at v2 artifacts, so always pass the
v3 paths explicitly (a mismatch aborts before any service starts):

```
V3="--corpus-dir data/sec-scale-v3-evaluation-20261101 --ingest-report indexes/sec-scale-v3-20261101/report.json \
    --vector-index indexes/vector-sec-scale-v3-pq128-20261101 --sparse-index indexes/sparse-sec-scale-v3-20261101"
SUITE_SHA=$(cut -d' ' -f1 bench_kit/suites/broad-validation-v2.sha256)
RUN=bench_kit/results/audit-latency-20261102/paired-01        # must not exist yet

# paired legacy-vs-fast latency (24 live runs maximum)
python3 scripts/evaluate-expanded-agent.py --paired-latency --out $RUN $V3 \
  --suite bench_kit/suites/broad-validation-v2.json --expected-suite-sha256 "$SUITE_SHA" [--repeats 2] [--suite-case audit04 ...]
python3 bench_kit/eval_agent_latency.py verify --directory $RUN/latency      # defaults are the v3 corpus/report and the v2 suite

# full frozen 30-case fast candidate, then the existing independent verifier
python3 scripts/evaluate-expanded-agent.py --out bench_kit/results/audit-latency-20261102/full30-fast $V3 \
  --suite bench_kit/suites/broad-validation-v2.json --expected-suite-sha256 "$SUITE_SHA" --agent-mode fast
python3 bench_kit/verify_broad_agent.py --directory bench_kit/results/audit-latency-20261102/full30-fast \
  --suite bench_kit/suites/broad-validation-v2.json --corpus data/sec-scale-v3-evaluation-20261101/chunks.jsonl \
  --ingest-report indexes/sec-scale-v3-20261101/report.json --strict-code --enforce-gates
```

* Subset (preregistered in `verification/pre-run-plan.json`): six cases, two repeats, both modes = 24 runs, the hard cap. **Primary supported cases:**
  `audit04` (AMZN expense), `audit07` (AMZN lease), `broad25` (HPE), `broad21` (UNH/CVS). **Descriptive cases:** `audit10` (exact missing section),
  `broad30` (mixed). Prompts are the frozen suite prompts, unchanged.
* **Bounds (all concurrent, not totals):** at most 2 model calls in flight, 3 retrieval subqueries in flight, one audit active, one UI audit, the
  4x2 sidecar. Per audit the **total** is at most 11 model calls (1 Plan + up to 9 critic attempts [3 subqueries x (1 + 2 retries)] + 1 synthesis or
  fallback), identical for both modes.
* One shared sidecar/bridge/2-slot model pool for both modes; pairs alternate AB/BA; order and ordinal are recorded and the order effect reported.
  No result cache exists; a retrieval-side cache warming over repeats is why order alternates. Stop any running UI first.
* Every run is kept: raw `latency/runs/<NN>-<mode>-<case>-r<k>.json|.md`, `.failure.json`, `records.jsonl`, `run-manifest.json` (written before the first
  request). No retries, no re-runs.
* **Latency gate (actual, preregistered):** `1 - median(fast seconds) / median(legacy seconds)` over the **eight** primary runs per mode
  (4 cases x 2 repeats), required >= 15%. It fails closed: a missing, unverified, or non-`supported` primary run (either mode) fails the gate with a
  stated problem; nothing is filtered out to rescue the median. Per-pair fractional improvements and their median are reported under `descriptive`
  and do **not** decide the gate. Also reported (computed, not decided): model calls not increased in any pair, coverage not reduced in any pair,
  expected-absent scopes withheld, failures. No p99 claim.
* **What `verify` audits beyond the traces:** every raw result through the shared mode-aware `audit_case`; `records.jsonl` against the schedule and
  the raw results (ids, order, mode/route/call counts, `agent_seconds` equal to the raw `elapsed_seconds`, `wall_seconds` >= and within 5 s of it,
  failure rows against failure records); the supervisor's `artifact-hashes.json` (suite/corpus/report/provenance hashes, immutable index and model
  trees, required code files) with **strict** current-code comparison (run it before editing code; `--no-strict-code` only reports drift), the
  manifest code hashes versus the supervisor's, ingestion lineage, `run-status.json` (finished, owned processes stopped), and `health-before/after`
  (sidecar and bridge ready, corpus fingerprint, **4 workers x 2 threads**, no admission rejections). Any failure fails the whole run even if every
  record passed. The enrollment is rebuilt independently and compared.
* If the gates are not met, hold the release; do not rerun unchanged for luck.
* Run directories used: `paired-run-01` (failed, preserved) and `paired-run-02` (passed), both under `bench_kit/results/audit-latency-20261102/`.

## Results (measured, verified 2026-11-02)

* **Paired gate (`paired-run-02`): passed.** 24/24 runs strictly verified. Median over the eight primary supported runs per mode:
  legacy 29.71211002200289 s → fast 23.503144278001855 s, a **20.8970878857 %** reduction (preregistered threshold 15 %; reduction of medians, not
  median of per-pair percentages). Over 12 runs per mode: structured CLI invocations 59 → 49 (CLI-reported turns 118 → 104), 37 retrievals and 13 retries
  in each mode, 10 legacy syntheses removed, fast routes 10 `host_quotes` + 2 `none`, all available topics cited in both modes. One fast audit (31.8 s) was
  slower than its legacy pair (30.1 s); provider-turn jitter and genuine retries exist, so there is no universal SLA. Small N: no p99, throughput or cost claim.
* **Full frozen 30-case fast check: recovered composite.** `full-run-01` was interrupted by the execution environment after 18 saved cases (`run-status`
  still `started`; original files untouched; unsaved in-flight usage unknown). `full-run-02-remaining` selected only the 12 unfinished cases and completed 12/12.
  Union: 30/30 verified, 140 citations, 52/52 available subqueries cited, 27 supported + 3 expected-unavailable, 77 retrievals, 17 retries, 107 CLI
  invocations (224 reported turns), 27 `host_quotes` + 3 `none`, 0 fallbacks, 244 unique source passages; completed-case median 20.35 s, max 47.21 s (descriptive;
  not a causal comparison with the historical 21.03 s run). The fallback was not exercised live.
* Both modes verified structurally (source, quotes, scope, bounds), **not** by human semantic judgement.

### Reproducing the composite recovery verification (offline; starts no services)

```
R=bench_kit/results/audit-latency-20261102
python3 -I bench_kit/verify_agent_recovery.py \
  --interrupted-directory $R/full-run-01 --continuation-directory $R/full-run-02-remaining \
  --interruption-record $R/verification/full-run-01-interruption.json \
  --suite bench_kit/suites/broad-validation-v2.json --expected-suite-sha256 2d87557ba1e776f6489b0174211bde7ec21fa60f9a0544f32352b2608808831d \
  --corpus data/sec-scale-v3-evaluation-20261101/chunks.jsonl --expected-corpus-sha256 c62207623da1fd592d79248916ece408b5568eeceaafc1c0777a83a518f7a56f \
  --ingest-report indexes/sec-scale-v3-20261101/report.json --expected-ingest-report-sha256 5364f6c5e7f6027391fcf90ff0b0db0c4adb7538b55e9e697e5d68f662c7233e \
  --output <new file; created exclusively>
```

It runs the existing verifier in memory with strict code comparison on both directories, never rewrites the raw records or statuses, and fails closed
unless the interrupted batch is an exact recorded prefix (the only tolerated problems are the known unfinished `run-status` and the two truncated-run
summary discrepancies), the continuation is exactly the recorded suffix and strictly clean, the union is the exact frozen suite with disjoint batches,
all hashes match, and every case passes raw-source verification with full available coverage. It cannot show process cleanup or create a normal
30-case run; its summary says so. Its own tests (`bench_kit/test_agent_recovery_verification.py`) use labelled test doubles only.

## Observed failure in paired run 01 (contract fixed afterwards)

`paired-run-01` (preserved, not edited) contains `11-fast-broad25-r2.failure.json`: the fast critic's response was rejected with
`ValueError: critic retained unknown or duplicate evidence`. The raw returned values were not saved, so it is **not known** whether a handle/ID was
unknown or duplicated. What the code showed: the fast critic was given two identifier namespaces (canonical IDs for `retain_ids` and
`adjacent_pairs`, `E01` handles for quotes), which is a plausible source of an invalid copy but is not proven to be the cause. The contract above removes
the second namespace and makes the rejection reason countable (unknown vs duplicate) without storing untrusted values. The fresh `paired-run-02` (24/24 verified) and the 30 completed full-suite cases showed no such rejection; run 01's artifacts are not evidence for the new code.

## Known limitations

* Live fast-mode compliance is measured only over the 24 paired and 30 full-suite runs above (no fallback occurred); behavior on other inputs is not claimed.
  The fallback has been exercised only with test doubles.
* Quote quality is checked structurally (verbatim, in scope, accepted), not semantically.
* The critic still receives the full filing catalog in every call; scoping it is a possible later, separately measured change.
* Unit-test timings are not benchmarks.
