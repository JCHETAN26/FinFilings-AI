# HPE 2024 critic follow-up — targeted two-replay check (November 1, 2026)

**Scope (read first).** This is a *targeted regression check* of one critic change: two fresh real replays of the HPE case (`broad25`), one absent-section control (`broad28`) and one mixed-availability control (`broad30`). It is **not** a fresh 30-case validation, **not** a semantic-accuracy measure (the verifier checks that published quotes are exact source text with trusted metadata, not that an answer is correct), and it does **not** re-run retrieval-quality (100-query) or load (50-client) benchmarks, because only the critic changed. The earlier 30-case, 100-query and 50-load evidence in [`parser-v3-adoption-20261101`](../parser-v3-adoption-20261101/RESULTS.md) is historical and unchanged. Two replays are small evidence; model output varies between runs. This does not show that critic gaps are fixed in general and does not claim there are no remaining limitations.

## Baseline vs. fresh runs (HPE `broad25`, exact `Item 7`, filing years 2024 and 2025)

| Run | Planned scopes cited | Published citations | Retrievals / retries | Critic scores (2024 → 2025) | Model calls | Seconds | Flags |
|---|---|---|---|---|---|---|---|
| Baseline, parser-v3 `agent-run-01` ([json](../parser-v3-adoption-20261101/agent-run-01/evaluation/broad25.json) · [md](../parser-v3-adoption-20261101/agent-run-01/evaluation/broad25.md)) | **1/2** (2025 only) | 4 (all 2025) | 4 / 2 | 3, 3, 3 → 4 | 6 | 39.993750446003105 | 2024 below threshold after 2 retries |
| Fresh run-01 ([json](run-01/evaluation/broad25.json) · [md](run-01/evaluation/broad25.md)) | **2/2** | 4 (2024: passages 35, 36, 42; 2025: passage 36) | 2 / 0 | 5 → 5 | 4 | 23.04018711900062 | none |
| Fresh run-02 ([json](run-02/evaluation/broad25.json) · [md](run-02/evaluation/broad25.md)) | **2/2** | 4 (2024: passages 36, 42; 2025: passage 36 ×2) | 2 / 0 | 5 → 5 | 4 | 22.182750946998567 | none |

Baseline diagnosis (from the raw JSON; the display order and the rubric changed together, so their separate effects are not isolated): passages `000164559024000139#item7#passage-000035` ("…during fiscal 2024, we repurchased") and `…#passage-000036` ("and settled an aggregate amount of $150 million. As of October 31, 2024, we had a remaining authorization of approximately $0.8 billion…") were both already in the critic's context, but displayed in reversed order (36 before 35); the critic's recorded rationales nevertheless treated the repurchase sentence as incomplete ("cut off") and then asked for the original authorization and share counts, which the prompt did not request, and mentioned the other year's hop. In both fresh runs the HPE critic rationales recognise the 35/36 adjacency. Citations keep the passage IDs separate and quote each passage's own text; nothing is concatenated for a citation.

## Controls (run-01)

| Case | Expectation | Result |
|---|---|---|
| `broad28` ([json](run-01/evaluation/broad28.json) · [md](run-01/evaluation/broad28.md)) | absent exact section: no answer | 0 citations, no claims; 6 retrievals (2 subqueries × 3 attempts), 4 retries total (≤2 each); critic score 1 throughout; "No verified answer" flag — the required outcome |
| `broad30` ([json](run-01/evaluation/broad30.json) · [md](run-01/evaluation/broad30.md)) | mixed: KO available, AMZN absent | available KO side 1/1 fully cited; AMZN side withheld with the explicit below-threshold flag; 6 citations, 4 retrievals, 2 retries (all on the absent side) |

## What changed (method)

One production file, `py/agent/critic.py`; Codex confirmed no other implementation file differs from the `6f18c04` baseline.

* **Document view:** `review_view()` orders the (already verified, already in-context) passages by passage number *within one verified scope* — same accession, ticker, filing year and section — and emits `adjacent_pairs` for strictly consecutive numbers. Different scopes, gaps and malformed IDs are never paired. No text is merged or edited, IDs stay separate, and the helper performs no extra corpus lookups or model calls.
* **Rubric:** the instruction now says to judge only the subquery's own scope, read passages collectively, treat details the goal did not ask for as neither score-lowering nor rewrite targets, and name any genuinely missing requested element.
* **Generic:** no ticker, year, chunk-ID or section special-casing. The rubric and the view were changed together, so their separate causal contributions cannot be separated from this data.
* **Unchanged:** retrieval, reranking, models, indexes, corpora, suite (`broad-validation-v2`, sha256 `2d87557ba1e776f6489b0174211bde7ec21fa60f9a0544f32352b2608808831d`), acceptance threshold 4, the exact citation guard, and the bounds: ≤5 evidence passages per context, ≤2 retained, ≤2 rewrites, ≤3 subqueries / concurrent retrievals, ≤2 concurrent model calls.

## Tests and concurrency proof

* Codex: **50 application tests and 136 benchmark-contract tests pass** ([python](verification/python-tests.log) · [benchmark](verification/benchmark-tests.log)). The new critic tests (`py/test_critic_context.py`) cover ordering, adjacency, scope isolation, duplicate/malformed IDs, unchanged text, empty-evidence rejection, retention validation and a one-call contract. They use a recording double and tiny test-only fixtures for transport; they are not model-quality evidence — the live replays are.
* Final health for both runs: 4 inference workers × 2 threads, admission limit 64, active 0, peak 2, rejected 0 ([run-01](run-01/health-after.json) · [run-02](run-02/health-after.json)); all owned processes stopped; all index/model hashes match the baseline ([release checks](verification/independent-release-checks.json)).
* Strict independent verification passed both partial runs (`--allow-subset --strict-code`): 3 cases / 10 citations (run-01) and 1 case / 4 citations (run-02) — **14 verified citations in total, 8 of them HPE** ([run-01 log](verification/run-01-verification.log) · [run-02 log](verification/run-02-verification.log)). The full-suite release gate is not applicable to a subset and was not weakened.

## Preserved record

Only the two preregistered HPE runs were made; both passed and no attempt was discarded or tuned against ([pre-run plan](pre-run-plan.json)). Raw artifacts: [run-01](run-01/) · [run-02](run-02/). Runbook: [`docs/build/hpe-critic.md`](../../../docs/build/hpe-critic.md).

Publication recorded in Git history; no deployment performed.
