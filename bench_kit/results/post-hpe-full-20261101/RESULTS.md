# Post-HPE-fix full 30-case validation — November 1, 2026

**Status: historical HOLD decision — preserved unchanged alongside the later passing fix.** The structural release gates passed, but the preregistered
*no-new-gap* case-by-case regression check **failed**: `audit04` (AMZN, filing year 2024) is a **new partial gap**, while the HPE gap (`broad25`)
is **resolved**. `all_preregistered_checks_passed = false`, `publication_decision = "hold"`
([`verification/regression-comparison.json`](verification/regression-comparison.json)). That hold decision stands for this attempt and its raw metrics below are unchanged.
The AMZN gap was subsequently diagnosed and fixed, and a later full run passed independent verification and was approved:
see the [latest report](../amzn-expense-20261101/RESULTS.md) (attempt history, diagnostic and all 30 case rows). Any publication of that work is recorded in Git history; no deployment was performed.
This was a quality hold, not a transport/model/structural failure.

**Read first — what not to conclude**

* Do **not** read this as "all gates pass", "30/30 answers correct" or "accuracy improved". The verifier checks frozen-suite membership,
  scope availability, source identity and bytes, citation text, bounds and mixed-availability behaviour. It is **not** a human-labelled
  answer-accuracy score; the suite is source-informed regression fixtures, not independent gold.
* The aggregate coverage is the same as before the HPE fix (**51/52 = 98.0769 %** in both runs). That sameness **conceals a shifted failure**:
  the one uncovered subquery moved from HPE (`broad25`) to AMZN (`audit04`).
* No causal claim is made that the critic change caused the AMZN gap. The planner and rewrite outputs also differ between the runs,
  and the model is nondeterministic. Only one live run exists for each side.
* No threshold was relaxed, no case was rerun to replace this result, and no workaround was added. The new gap stays visible.

## What was run

The full, unchanged frozen suite (`broad-validation-v2`, 30 cases; no `--suite-case`, no `--allow-subset`) at code state `ee6ec35`
(the HPE critic fix, already independently verified), on the unchanged parser-v3 mixed-lineage corpus.

| Item | Value |
|---|---|
| Suite SHA-256 (pinned) | `2d87557ba1e776f6489b0174211bde7ec21fa60f9a0544f32352b2608808831d` |
| Corpus SHA-256 (`chunks.jsonl`, 49,406 passages) | `c62207623da1fd592d79248916ece408b5568eeceaafc1c0777a83a518f7a56f` |
| Trusted filings / lineage | 152; mixed: 1 filing parser v3, 147 parser v2 (byte-for-byte), plus the original 4 unchanged |
| Code | 56 implementation files, strict check: unchanged since `ee6ec35` (`changed: []`, `missing: []`) |
| Hashes | vector index 5/5, reranker model 2/2, sparse index 28/28 recomputed and verified; model/index/code hashes equal those of the previously verified HPE fix |
| Run window (UTC) | 2026-11-02T00:41:08 → 00:53:27; evaluator exit 0; all owned processes stopped |

Bounds (identical to the pre-run plan): 4 inference workers × 2 threads; HTTP admission 64 (final active 0, peak 2, rejected 0);
Go search admission 8; agent 3 concurrent subqueries / 2 concurrent model calls, ≤2 retries per subquery, 5 evidence passages per context,
2 retained passages. Evidence: [`pre-run-plan.json`](pre-run-plan.json) ·
[`independent-preflight.json`](independent-preflight.json) · [`run-01/run-status.json`](run-01/run-status.json) ·
[`run-01/evaluation/report.json`](run-01/evaluation/report.json) ·
[`run-01/independent-broad-verification.json`](run-01/independent-broad-verification.json) ·
[`verification/independent-verification.log`](verification/independent-verification.log) ·
[`verification/regression-comparison.json`](verification/regression-comparison.json) ·
[`amzn-gap-source-check.json`](amzn-gap-source-check.json).

## Result summary

| Metric | Value |
|---|---:|
| Cases retained / executed / independently verified | 30 / 30 / 30 |
| Transport / model / structural failures | 0 / 0 / 0 |
| Source, filter or bounds violations | 0 |
| Exact-verified published citations | 187 |
| Unique source passages checked | 246 |
| Available planned subqueries cited | 51 / 52 (98.0769 %; existing floor 80 %) |
| Outcomes | 26 supported · 1 partial gap (`audit04`) · 3 expected-unavailable (`audit10`, `broad27`, `broad28`) |
| Mixed cases (`broad29`, `broad30`) | each available side 1/1 fully cited, absent side withheld |
| Retrieval calls / retries / model calls | 79 / 19 / 136 |
| Median per-case elapsed | 23.88654944549853 s |
| HPE `broad25` | both years 2/2 cited, 3 citations, 0 retries, no flags, 19.4102128409977 s |

### Structural gates passed; the no-new-gap check failed

Two different kinds of check are recorded and they disagree, on purpose:

1. **Structural release gates — PASSED** (`structural_release_gates_passed: true`; the verifier's `passed: true`, run with `--strict-code --enforce-gates`):
   all 30 frozen cases retained; no failures or violations; unavailable scopes publish zero claims; mixed cases keep the absent side
   withheld and fully support the available side; available-subquery coverage 51/52 ≥ the 0.80 floor; code, index and model hashes match.
2. **Preregistered no-new-gap case-by-case regression check — FAILED** (`no_new_gap_regression_check_passed: false`): the pre-run plan required
   "no newly partial or abstained available cases relative to the pre-fix baseline". `audit04` was `supported` in the baseline and is
   `partial_gap` now. The verifier's own gates (`independent-verification.log`) do not include this comparison; it is recorded in `regression-comparison.json`
   from the per-case records (`new_gap_cases: ["audit04"]`, `resolved_gap_cases: ["broad25"]`).

The coverage floor is an aggregate threshold, so it passes with one gap whichever case holds it. The case-level check is what caught the shift.

## Baseline comparison (same suite, same v3 corpus, before the HPE fix)

Baseline: [`parser-v3-adoption-20261101/agent-run-01`](../parser-v3-adoption-20261101/RESULTS.md) (code before `ee6ec35`; same suite and corpus hashes).
Counts below were read from that run's `report.json` and independent verification JSON.

| Metric | Baseline (`agent-run-01`) | This run (`post-hpe-full`) |
|---|---:|---:|
| Verified cases | 30/30 | 30/30 |
| Verified citations | 187 | 187 |
| Unique source passages checked | 252 | 246 |
| Available subqueries cited | 51/52 | 51/52 |
| Retrieval calls | 85 | 79 |
| Retries | 25 | 19 |
| Model calls | 142 | 136 |
| Median elapsed (s) | 24.366062377000162 | 23.88654944549853 |
| Outcomes | 26 supported · 1 partial · 3 expected-unavailable | 26 supported · 1 partial · 3 expected-unavailable |
| Case holding the partial gap | `broad25` (HPE) | `audit04` (AMZN) |

| Case | Baseline | This run |
|---|---|---|
| `broad25` HPE filing 2024 + filing 2025 (Item 7) | partial gap: 4 citations, 2 retries, 39.99 s, "Subquery 0 has no evidence meeting threshold after 2 retries." | **resolved**: supported, 3 citations, 0 retries, 19.41 s, no flags |
| `audit04` AMZN filing 2024 (whole filing) | supported: 7 citations, 1 retry, 35.17 s, no flags | **new gap**: partial, 8 citations (all under subquery 0), 2 retries, 43.67 s, "Subquery 1 has no evidence meeting threshold after 2 retries." |

Identical counts in the headline rows do not mean identical behaviour: one gap closed and a different one opened. Differences in retrievals,
retries, model calls and median time are single-run, nondeterministic live-model numbers and are not a speed or accuracy claim.
(Separately, the baseline `broad15` carried one "Dropped unverified claim" flag that this run does not; the cause was not stored and is not investigated here.)

## The AMZN `audit04` gap

* **Prompt (exact):** "Audit AMZN filing year 2024: separately retrieve AWS sales disclosures and operating expense disclosures. Support both topics with direct excerpts."
* **Subquery 0 — "AWS segment net sales disclosure":** score 5 on attempt 0, accepted, cited (8 citations). Supported.
* **Subquery 1 — "operating expenses breakdown by category":** the critic scored 3 on **all 3 attempts** (attempt 0 plus 2 rewrites). No verified quote was accepted for it, so
  the case publishes the AWS side only and flags "Subquery 1 has no evidence meeting threshold after 2 retries." Rewrites asked for the amounts of cost of sales,
  fulfillment, technology and infrastructure, and sales and marketing; the critic's rationale each time was that those line amounts were not in the five supplied passages.
* **Known supporting source:** `000101872424000008#passage-000064` (AMZN, 10-K, filing year 2024, section "Full filing (item not classified)", text SHA-256
  `d0cd876d3c7bafa6bf09b364fe797968b3b4ebc1dbbfffa61d79ba60605a6198`) is an operating-expense table chunk. It **exists in the unchanged corpus**, was in the baseline's
  critic context for subquery 1 on attempt 1 (where the critic scored 5), and is **absent from all current five-passage critic contexts**
  ([`amzn-gap-source-check.json`](amzn-gap-source-check.json)).
* **What is not known:** the raw pre-truncation Top-10 rankings were not captured, so whether retrieval did not surface that passage or the five-passage context selection dropped it
  is **unresolved**. This report does not claim undisclosed data, a critic bug, or a retrieval defect. No threshold change or workaround was applied.

## All 30 cases

"Filing year" is the SEC filing year, not a fiscal-year label. Each scope shows ticker, filing year and the exact section filter from the frozen suite's `expected_scopes`. Unavailable scopes carry the suite's reason: `filing_year_absent` (no filing for that ticker and filing year in the corpus) or `exact_section_absent` (the filing exists, but the exact section filter matches nothing; no other section is substituted). Both are expected by the frozen suite. Coverage is cited / available planned subqueries.
Elapsed is per-case wall time from `report.json`. Outcomes are evidence outcomes, not answer-accuracy labels.

| # | Case | Kind | Topic scopes | Citations | Cited / available | Retries | Elapsed (s) | Outcome | Flags | Raw |
|---:|---|---|---|---:|---:|---:|---:|---|---|---|
| 1 | `audit01` | regression | AAPL filing 2023, whole filing — available; NVDA filing 2024, whole filing — available | 7 | 2/2 | 0 | 26.97 | supported | — | [json](run-01/evaluation/audit01.json) · [md](run-01/evaluation/audit01.md) |
| 2 | `audit02` | regression | MSFT filing 2024, whole filing — available; AMZN filing 2024, whole filing — available | 8 | 2/2 | 1 | 42.23 | supported | — | [json](run-01/evaluation/audit02.json) · [md](run-01/evaluation/audit02.md) |
| 3 | `audit03` | regression | NVDA filing 2024, whole filing — available | 8 | 2/2 | 0 | 21.51 | supported | — | [json](run-01/evaluation/audit03.json) · [md](run-01/evaluation/audit03.md) |
| 4 | `audit04` | regression | AMZN filing 2024, whole filing — available | 8 | 1/2 | 2 | 43.67 | **PARTIAL GAP (new)** | Subquery 1 has no evidence meeting threshold after 2 retries. | [json](run-01/evaluation/audit04.json) · [md](run-01/evaluation/audit04.md) |
| 5 | `audit05` | regression | AAPL filing 2023, whole filing — available | 7 | 2/2 | 0 | 24.16 | supported | — | [json](run-01/evaluation/audit05.json) · [md](run-01/evaluation/audit05.md) |
| 6 | `audit06` | regression | MSFT filing 2024, whole filing — available | 8 | 2/2 | 0 | 24.84 | supported | — | [json](run-01/evaluation/audit06.json) · [md](run-01/evaluation/audit06.md) |
| 7 | `audit07` | regression | AMZN filing 2024, whole filing — available | 7 | 2/2 | 0 | 26.81 | supported | — | [json](run-01/evaluation/audit07.json) · [md](run-01/evaluation/audit07.md) |
| 8 | `audit08` | regression | NVDA filing 2024, whole filing — available | 5 | 2/2 | 0 | 17.84 | supported | — | [json](run-01/evaluation/audit08.json) · [md](run-01/evaluation/audit08.md) |
| 9 | `audit09` | regression | AAPL filing 2025, whole filing — available | 8 | 2/2 | 0 | 25.84 | supported | — | [json](run-01/evaluation/audit09.json) · [md](run-01/evaluation/audit09.md) |
| 10 | `audit10` | regression | MSFT filing 2024, section Item 1A — **exact_section_absent** | 0 | 0/0 (none available) | 4 | 17.00 | expected unavailable | no-evidence flags on both subqueries (expected) | [json](run-01/evaluation/audit10.json) · [md](run-01/evaluation/audit10.md) |
| 11 | `broad11` | positive | KO filing 2025, section Item 7 — available; PG filing 2025, section Item 7 — available | 5 | 2/2 | 0 | 24.86 | supported | — | [json](run-01/evaluation/broad11.json) · [md](run-01/evaluation/broad11.md) |
| 12 | `broad12` | positive | PFE filing 2025, section Item 1A — available; MRK filing 2025, section Item 1A — available | 8 | 2/2 | 0 | 20.03 | supported | — | [json](run-01/evaluation/broad12.json) · [md](run-01/evaluation/broad12.md) |
| 13 | `broad13` | positive | BAC filing 2026, section Item 7 — available | 7 | 2/2 | 0 | 23.59 | supported | — | [json](run-01/evaluation/broad13.json) · [md](run-01/evaluation/broad13.md) |
| 14 | `broad14` | positive | TGT filing 2026, whole filing — available | 7 | 2/2 | 0 | 21.08 | supported | — | [json](run-01/evaluation/broad14.json) · [md](run-01/evaluation/broad14.md) |
| 15 | `broad15` | positive | TSLA filing 2026, whole filing — available | 8 | 2/2 | 0 | 26.83 | supported | — | [json](run-01/evaluation/broad15.json) · [md](run-01/evaluation/broad15.md) |
| 16 | `broad16` | positive | V filing 2024, section Item 1A — available; V filing 2025, section Item 1A — available | 8 | 2/2 | 0 | 30.32 | supported | — | [json](run-01/evaluation/broad16.json) · [md](run-01/evaluation/broad16.md) |
| 17 | `broad17` | positive | COP filing 2025, section Item 7 — available; OXY filing 2025, section Item 7 — available | 8 | 2/2 | 0 | 26.74 | supported | — | [json](run-01/evaluation/broad17.json) · [md](run-01/evaluation/broad17.md) |
| 18 | `broad18` | positive | MET filing 2025, whole filing — available; AFL filing 2025, whole filing — available | 7 | 2/2 | 0 | 23.61 | supported | — | [json](run-01/evaluation/broad18.json) · [md](run-01/evaluation/broad18.md) |
| 19 | `broad19` | positive | CRM filing 2026, section Item 8 — available; ADBE filing 2026, section Item 8 — available | 6 | 2/2 | 0 | 20.41 | supported | — | [json](run-01/evaluation/broad19.json) · [md](run-01/evaluation/broad19.md) |
| 20 | `broad20` | positive | AMD filing 2026, whole filing — available; TXN filing 2026, whole filing — available | 6 | 2/2 | 0 | 20.51 | supported | — | [json](run-01/evaluation/broad20.json) · [md](run-01/evaluation/broad20.md) |
| 21 | `broad21` | positive | UNH filing 2025, section Item 7 — available; CVS filing 2026, section Item 7 — available | 5 | 2/2 | 0 | 21.39 | supported | — | [json](run-01/evaluation/broad21.json) · [md](run-01/evaluation/broad21.md) |
| 22 | `broad22` | positive | SBUX filing 2024, section Item 7 — available; SBUX filing 2025, section Item 7 — available | 10 | 2/2 | 0 | 25.96 | supported | — | [json](run-01/evaluation/broad22.json) · [md](run-01/evaluation/broad22.md) |
| 23 | `broad23` | positive | ISRG filing 2026, section Item 7 — available | 7 | 2/2 | 0 | 20.16 | supported | — | [json](run-01/evaluation/broad23.json) · [md](run-01/evaluation/broad23.md) |
| 24 | `broad24` | positive | LLY filing 2026, section Item 7 — available; JNJ filing 2026, section Item 7 — available | 7 | 2/2 | 0 | 19.98 | supported | — | [json](run-01/evaluation/broad24.json) · [md](run-01/evaluation/broad24.md) |
| 25 | `broad25` | positive | HPE filing 2024, section Item 7 — available; HPE filing 2025, section Item 7 — available | 3 | 2/2 | 0 | 19.41 | supported (**gap resolved**) | — | [json](run-01/evaluation/broad25.json) · [md](run-01/evaluation/broad25.md) |
| 26 | `broad26` | positive | HD filing 2026, section Item 8 — available; WMT filing 2026, section Item 8 — available | 8 | 2/2 | 0 | 25.98 | supported | — | [json](run-01/evaluation/broad26.json) · [md](run-01/evaluation/broad26.md) |
| 27 | `broad27` | negative | TSLA filing 2024, whole filing — **filing_year_absent** | 0 | 0/0 (none available) | 4 | 20.16 | expected unavailable | no-evidence flags on both subqueries (expected) | [json](run-01/evaluation/broad27.json) · [md](run-01/evaluation/broad27.md) |
| 28 | `broad28` | negative | NVDA filing 2024, section Item 7 — **exact_section_absent** | 0 | 0/0 (none available) | 4 | 15.76 | expected unavailable | no-evidence flags on both subqueries (expected) | [json](run-01/evaluation/broad28.json) · [md](run-01/evaluation/broad28.md) |
| 29 | `broad29` | mixed | GS filing 2026, whole filing — available; BAC filing 2025, whole filing — **filing_year_absent** | 5 | 1/1 | 2 | 29.15 | supported on available side; absent side withheld | Subquery 1 (absent side) no evidence after 2 retries (expected) | [json](run-01/evaluation/broad29.json) · [md](run-01/evaluation/broad29.md) |
| 30 | `broad30` | mixed | KO filing 2025, section Item 7 — available; AMZN filing 2024, section Item 7 — **exact_section_absent** | 6 | 1/1 | 2 | 27.82 | supported on available side; absent side withheld | Subquery 1 (absent side) no evidence after 2 retries (expected) | [json](run-01/evaluation/broad30.json) · [md](run-01/evaluation/broad30.md) |

Column totals: citations 187, cited/available 51/52, retries 19. Topic text for each case is in
[`run-01/suite.json`](run-01/suite.json) (the frozen suite copy); the table shows scopes.

## What was and was not tested

* **Fresh in this validation:** the live 30-case agent run and the strict independent verifier (`--strict-code --enforce-gates` semantics).
* **No source code was modified** in this validation. The existing 50 application tests and 136 benchmark-contract tests from `ee6ec35` are *prior* verified test
  evidence; they were **not rerun** here, and no fresh test-suite execution is claimed.
* **Not rerun:** the 100-query retrieval benchmark and the 50-client load benchmark. Their unchanged artifacts were rehashed only; no new retrieval-quality or search-load latency numbers are claimed (the per-case agent elapsed times in this report are freshly measured).
* Not checked by the verifier (it says so): the full Python suite, Go race/vet, and the public-snapshot hygiene check.
* Limits: one live run; source-informed regression fixtures, not independent human gold; local non-isolated desktop; v3 corpus is mixed-lineage and the suite has no Morgan Stanley case.

## Reproduction

Use a **fresh output path**; never reuse or overwrite `run-01`. Keep the first attempt and record any repeat as a separate directory. Do not tune thresholds, cases, models or the critic to change a result.

```bash
# Run the full pinned suite (no --suite-case, no --allow-subset). The output path is new on purpose.
SUITE_SHA=$(cut -d' ' -f1 bench_kit/suites/broad-validation-v2.sha256)
OUT="bench_kit/results/post-hpe-full-repro-01"
python3 scripts/evaluate-expanded-agent.py --out "$OUT" \
  --corpus-dir data/sec-scale-v3-evaluation-20261101 \
  --ingest-report indexes/sec-scale-v3-20261101/report.json \
  --vector-index indexes/vector-sec-scale-v3-pq128-20261101 \
  --sparse-index indexes/sparse-sec-scale-v3-20261101 \
  --suite bench_kit/suites/broad-validation-v2.json --expected-suite-sha256 "$SUITE_SHA"

# Independent strict verification of the fresh output
python3 bench_kit/verify_broad_agent.py \
  --directory "$OUT" \
  --suite bench_kit/suites/broad-validation-v2.json \
  --corpus data/sec-scale-v3-evaluation-20261101/chunks.jsonl \
  --ingest-report indexes/sec-scale-v3-20261101/report.json --strict-code --enforce-gates
```

`--strict-code` fails if any recorded implementation file changed since the run; `--enforce-gates` fails unless every release gate is met. The verifier's release gates do
**not** include the case-by-case no-new-gap comparison: compare each case's `evidence_outcome` with the baseline `report.json` yourself, as done above.
A structural pass with a new gap is a hold, not a pass. Requires the local models/indexes and an authenticated Claude CLI; it makes live model calls.

## Known limitations and unresolved items

1. **Unresolved — AMZN `audit04`:** retrieval versus five-passage context selection as the cause of the missing operating-expense table is undetermined
   (pre-truncation rankings not captured). No fix is made or claimed.
2. Single live run per side; model nondeterminism and differing planner/rewrite outputs mean the HPE improvement and the AMZN regression are each observations, not established effects.
3. Same-aggregate coverage can hide case-level shifts; the case-level comparison is a manual step outside the verifier.
4. Fixtures are source-informed regression cases, not human gold; no answer-accuracy claim.
5. The v3 corpus is mixed-lineage; the suite contains no Morgan Stanley case.

No deployment was performed. This directory is the preserved record of the held attempt; do not edit its raw artifacts.
