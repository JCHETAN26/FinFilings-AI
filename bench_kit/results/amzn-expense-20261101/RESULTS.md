# AMZN operating-expense fix and full 30-case revalidation — November 2, 2026

**Status: independently verified and approved by the regression comparison (`publication_decision = approve`).** The full, unchanged frozen
`broad-validation-v2` suite (30 cases, no subset flags) ran as `full-run-05` and was strictly re-verified: 30/30 cases, 0 failures, 178 exact-verified
citations, every one of the 52 available planned subqueries cited, and 27 supported + 3 expected-unavailable outcomes with no partial, abstained or
failed case. Publication is recorded in Git history; **no deployment was performed.**

**Read first — what not to conclude**

* This is **not** an answer-accuracy score. The suite is source-informed regression fixtures (reviewer-recorded anchors), not independent human gold.
  The verifier checks frozen-suite membership, scope availability, source identity and bytes, quote text, bounds and mixed-availability behaviour.
* One live run per attempt, a nondeterministic model (`claude-haiku-5-5`) and, between attempts, intentionally different code. Differences in
  citations, retries or timing between attempts are observations, **not causal speed or quality claims**.
* The new empty-evidence "stalled rewrite" stop was **not exercised live**: `full-run-05` contains **0 stalled stops** (all 79 hops carry the new trace
  fields, none has a `terminal_reason`) and **0 dropped-quote flags**. The path is covered by the 81 application and 168 benchmark-contract tests only.
* The earlier attempts below were all preserved unchanged. Four fresh attempts after the held run failed their gates (see the history); none was
  replaced, rerun for luck, or tuned around. No threshold, suite, expectation, model, index or corpus was changed to produce the final result.
* Synthesis is extractive. No financial calculation is derived or checked here. No fresh 100-query retrieval or 50-client load test was run; this is an
  agent validation only.

## Result summary (`full-run-05`)

| Metric | Value |
|---|---:|
| Cases retained / executed / independently verified | 30 / 30 / 30 |
| Transport / model / structural failures | 0 / 0 / 0 |
| Source, filter or bounds violations | 0 |
| Exact-verified published citations | 178 |
| Unique source passages checked | 250 |
| Available planned subqueries cited | 52 / 52 (100 %; existing floor 80 %) |
| Outcomes | 27 supported · 0 partial gap · 0 abstained · 3 expected-unavailable (`audit10`, `broad27`, `broad28`) |
| Mixed cases (`broad29`, `broad30`) | each available side 1/1 fully cited, absent side withheld (`mixed_fully_supported`) |
| Fully covered positive cases / negative cases | 25 / 3 |
| Retrieval calls / retries / model calls | 79 / 19 / 136 |
| Median per-case elapsed | 21.032962917 s |
| AMZN `audit04` (both planned topics) | AWS sales and operating expenses both cited: 2/2 subqueries, 6 citations, 0 retries, 25.27 s |
| HPE `broad25` (both filing years) | 2/2 cited, 4 citations, 0 retries, 23.52 s |
| AMD/TXN `broad20` | 2/2 cited, 6 citations, 0 retries, 20.20 s |
| Lease `audit07` | 2/2 cited, 8 citations, 1 retry, 29.89 s |
| Dropped-quote flags / actual stalled stops | 0 / 0 |

Evidence: [`report.json`](full-run-05/evaluation/report.json) · [`independent-broad-verification.json`](full-run-05/independent-broad-verification.json) ·
[`verification/full-run-05-independent-verification.log`](verification/full-run-05-independent-verification.log) ·
[`verification/full-run-05-regression-comparison.json`](verification/full-run-05-regression-comparison.json) ·
[`verification/full-run-05-sealed-artifacts.json`](verification/full-run-05-sealed-artifacts.json) (SHA-256 of every raw artifact) ·
[`verification/full-run-05-preflight.json`](verification/full-run-05-preflight.json) · [`full-run-05-plan.json`](full-run-05-plan.json) ·
[`full-run-05/run-status.json`](full-run-05/run-status.json) · [`full-run-05/health-after.json`](full-run-05/health-after.json) ·
[`full-run-05/artifact-hashes.json`](full-run-05/artifact-hashes.json) · [frozen suite copy](full-run-05/suite.json).

## What was run and how it was bounded

| Item | Value |
|---|---|
| Suite SHA-256 (pinned, unchanged) | `2d87557ba1e776f6489b0174211bde7ec21fa60f9a0544f32352b2608808831d` |
| Corpus (`chunks.jsonl`) | 49,406 passages, SHA-256 `c62207623da1fd592d79248916ece408b5568eeceaafc1c0777a83a518f7a56f`; 152 enrolled filings, mixed lineage (1 filing parser v3, 147 parser v2, original 4); same corpus as all earlier attempts |
| Hash verification | vector index 5/5, reranker model 2/2, sparse index 28/28 recomputed and equal; 56 implementation files checked in strict mode (`changed: []`, `missing: []`) |
| Run window (UTC) | 2026-11-02T04:04:07 → 04:15:55; evaluator exit 0; all owned processes stopped |
| Agent model | `claude-haiku-5-5` (authenticated Claude CLI), requested alias `haiku` |
| Hardware | AMD Ryzen 7 5800H, 8 physical / 16 logical CPUs, CPU inference, Linux 6.8, Python 3.10.12; local, non-isolated desktop |
| Serving bounds | 4 inference workers × 2 threads; FP32 ONNX reranker batch 1; dense batch 8, trigger 8, wait 2 ms; HTTP admission 64 (final active 0, **peak 2, rejected 0**); Go search admission 8 |
| Agent bounds | 3 concurrent subqueries; 2 concurrent model calls; ≤2 retries per subquery; ≤5 verified evidence passages per context, ≤2 retained nominees, top-4 fresh hits reserved |

## Attempt history (all preserved)

Counts come from each attempt's own `evaluation/report.json` and `independent-broad-verification.json`. "Verified / executed / frozen" counts cases;
"Out." is supported / partial gap / abstained / expected-unavailable; "Calls" is retrievals / retries / model calls.

| Attempt | Finished (UTC) | Verified / passed / total | Struct. fail | Citations | Unique passages | Cited / available | Outcomes (S/P/A/E) | Calls | Median s | Decision | What happened |
|---|---|---|---:|---:|---:|---|---|---|---:|---|---|
| Pre-HPE baseline (`parser-v3-adoption/agent-run-01`) | 2026-11-01T23:05:56Z | 30 / 30 / 30 | 0 | 187 | 252 | 51/52 | 26 / 1 / 0 / 3 | 85 / 25 / 142 | 24.366 | published (`6f18c04`) | HPE `broad25` partial gap (historical baseline) |
| Held post-HPE full run (`post-hpe-full/run-01`) | 2026-11-02T00:53:27Z | 30 / 30 / 30 | 0 | 187 | 246 | 51/52 | 26 / 1 / 0 / 3 | 79 / 19 / 136 | 23.887 | **HOLD** | new AMZN `audit04` partial gap (expense subquery scored 3 on all attempts) |
| `full-run-01` | 2026-11-02T01:32:34Z | 30 / 30 / 30 | 0 | 180 | 248 | 51/52 | 26 / 1 / 0 / 3 | 78 / 18 / 135 | 22.736 | **HOLD** | AMZN `audit04` supported; new AMD `broad20` partial gap — synthesis copied altered accession digits, all AMD claims rejected by the exact source guard |
| `full-run-02` | 2026-11-02T03:15:25Z | 30 / 30 / 30 | 0 | 179 | 246 | 51/52 | 26 / 1 / 0 / 3 | 79 / 19 / 136 | 22.079 | **HOLD** | AMD supported; new AMZN `audit07` partial gap — critic demanded an unasked year-by-year maturity schedule |
| `full-run-03` | 2026-11-02T03:41:52Z | 29 / 29 / 30 | 1 | 161 | 248 | 48/50 | 25 / 0 / 1 / 3 | 85 / 27 / 139 | 23.931 | **HOLD** | `audit07` supported; UNH/CVS `broad21` abstained (shared "each company" goal applied inside one-company filters); SBUX `broad22` execution failure (critic returned an unchanged rewrite) |
| `full-run-04` | 2026-11-02T03:57:08Z | 28 / 28 / 30 | 2 | 177 | 244 | 51/51 | 26 / 0 / 0 / 2 | 69 / 13 / 123 | 22.775 | **HOLD** | all 28 completed cases source-verified with no available gaps; `broad27` and `broad29` execution failures (same unchanged-rewrite exception) |
| **`full-run-05`** | 2026-11-02T04:15:54Z | 30 / 30 / 30 | 0 | 178 | 250 | 52/52 | 27 / 0 / 0 / 3 | 79 / 19 / 136 | 21.033 | **approve** (independent verifier + regression comparison) | 0 gaps, 0 failures; no stalled stop actually occurred |

**Partial-attempt caveat.** For `full-run-03` (1 failed case, `broad22`) and `full-run-04` (2 failed cases, `broad27`, `broad29`) the verified, citation, available-subquery (48/50, 51/51), retrieval/retry/model-call and median-latency figures cover **only the completed cases** (29 and 28 of 30), because a failed case saved no trace.
Their failed-case contributions are **unknown, not zero**, and those rows must not be read as complete 30-case coverage or compared as like-for-like totals with the other rows (the available-subquery denominators 50 and 51 also omit the failed cases' scopes).
The per-attempt figures are regenerated from the saved reports in [`verification/attempt-summary.json`](verification/attempt-summary.json), whose `metric_scope` field states what each row covers.

All rows share the frozen suite, corpus and indexes. The two earliest rows are the baseline and the held run documented in
[`parser-v3-adoption-20261101/RESULTS.md`](../parser-v3-adoption-20261101/RESULTS.md) and
[`post-hpe-full-20261101/RESULTS.md`](../post-hpe-full-20261101/RESULTS.md). Comparisons:
[`full-run-01`](verification/full-run-01-regression-comparison.json) ·
[`02`](verification/full-run-02-regression-comparison.json) · [`03`](verification/full-run-03-regression-comparison.json) ·
[`04`](verification/full-run-04-regression-comparison.json) · [`05`](verification/full-run-05-regression-comparison.json) ·
sealed artifact hashes for [`01`](verification/full-run-01-sealed-artifacts.json), [`02`](verification/full-run-02-sealed-artifacts.json),
[`03`](verification/full-run-03-sealed-artifacts.json), [`04`](verification/full-run-04-sealed-artifacts.json),
[`05`](verification/full-run-05-sealed-artifacts.json).

### The four failed fresh attempts, in order

1. **`full-run-01` (hold).** AMZN `audit04` was supported, but AMD `broad20` became a partial gap: the synthesizer reproduced long accession-bearing chunk IDs
   with altered digits (`…016`, `…017`, `…023` instead of the real `…018`), so every AMD claim failed the exact source guard and was correctly rejected.
   Retrieval and the critic had both scored the AMD evidence 5. The saved flags prove the invalid ID references; the original rejected quote text was not
   stored, and none is reconstructed here.
   Fix: opaque handles (below).
2. **`full-run-02` (hold).** AMD supported; AMZN `audit07` (liquidity and operating-lease obligations) became a partial gap. The planner had expanded the lease
   topic with "future minimum lease payments maturities" and the critic treated that search vocabulary as a requirement, demanding a year-by-year
   maturity schedule the audit never asked for, although the aggregate lease liability passages (120/121) were in context.
   Fix: user goal separated from search hint.
3. **`full-run-03` (hold).** `audit07` supported, but UNH/CVS `broad21` abstained and SBUX `broad22` failed. Both UNH and CVS subqueries carried the literal shared goal
   "…each company…"; each filter can only return its own company, and each critic scored 3 asking for the other company's evidence. SBUX `broad22` died with
   `ValueError: critic failed to rewrite a below-threshold query`; that failure left **no saved hop trace**, so the scope-related mechanism for `broad22` is an
   inference from the similar prompt wording, **not proven**.
   Fix: scope-first critic contract.
4. **`full-run-04` (hold).** The 28 completed cases were source-verified with no available gaps, but **two** cases failed with the same unchanged-rewrite exception:
   `broad27` (TSLA 2024, absent filing year) and `broad29` (mixed GS/BAC). The failure files contain no hop trace, so it is **not known which side of `broad29`** (available or absent)
   raised it; `broad27` is an unavailable scope with empty evidence.
   Fix: narrow typed empty-evidence stop.

## The original AMZN operating-expense diagnostic

The held post-HPE run left AMZN `audit04` as a partial gap, and the raw pre-truncation rankings were not captured. A bounded, LLM-free diagnostic replayed the
five saved real queries (three from the held run, two from the baseline) against the real hybrid bridge, sequentially, and re-ran the production context selection
([raw before](diagnostic-before/rank-diagnostic.json) · [after](diagnostic-after/rank-diagnostic.json) ·
[independent comparison](verification/independent-diagnostic-comparison.json)). The replayed "before" contexts matched all five recorded contexts exactly, and rankings and scores were unchanged between "before" and "after".

Review-only target (never sent to retrieval): the operating-expense table passage `000101872424000008#passage-000064`. Ranks are 1-based.

| Replay | Query (abbrev.) | Sparse | Dense | RRF | Rerank | Target in context (before) |
|---|---|---:|---:|---:|---:|---|
| Held run, attempt 0 | "operating expenses breakdown by category" | 1 | 4 | 1 | 8 | no (outside top 5) |
| Held run, attempt 1 | "Amazon operating expense amounts for cost of sales, fulfillment, technology…" | 1 | 7 | 3 | **4** | **no — inside the fresh top 5 but displaced by 2 retained passages (086, 069)** |
| Held run, attempt 2 | "Amazon consolidated statements of operations cost of sales…" | 1 | 16 | 6 | 6 | no (outside top 5) |
| Baseline, attempt 0 | "operating expenses breakdown disclosure" | 2 | 3 | 1 | not in top 10 | no |
| Baseline, attempt 1 | "cost of sales fulfillment technology and infrastructure sales and marketing…" | 1 | 8 | 3 | 2 | yes (kept) |

Finding: the table was retrieved and ranked well by sparse, dense and RRF; in the held run's second attempt it was **reranked 4th, then lost to carried passages**, i.e. a context-budget
effect, not a retrieval miss. With the fresh-top-4 budget the same replay places it in the merged context ([after](diagnostic-after/rank-diagnostic.json); actually carried
passage: 086 only, dropped nominee: 069).

**Diagnostic target versus the final run.** Passage 064 was a *review-only comparison target*, never a runtime input and never an acceptance criterion. In `full-run-05`
the AMZN expense subquery was accepted on its **first** attempt (score 4, 0 retries) from passages 106, 086, 150, 151, 069, and `audit04` cited passages
062, 151, 086, 151, 069 and 066 — **064 was not cited**. So the final pass does not demonstrate the displacement path; that evidence is the
diagnostic replay above and the unit tests, not the full run.

## General fixes (no company, year, ID or case special-casing)

| # | Fix | Where | What it does |
|---|---|---|---|
| 1 | **Fresh-4 context budget** | `py/agent/state_machine.py`, `models.py`, `bench_kit/verify_broad_agent.py` | The best four verified fresh hits are always kept; the ≤2 retained nominees use remaining slots; carried passages are listed first and `retained_evidence_ids` records only what was actually carried. Trace fields (nominees, raw top-10, fresh verified) let the verifier recompute the budget independently. |
| 2 | **Exact opaque handles** | `py/agent/synthesizer.py`, `models.py` | Verified evidence gets deterministic short handles (`E01`…, ≤15). The canonical citation metadata fields (accession, tag, URL, chunk ID) are withheld from the model; it sees the handle, light scope metadata (ticker, filing year, form, section) and the raw passage text, and returns a handle plus a verbatim quote. Passage text is preserved verbatim and is not scrubbed, so it may naturally contain identifiers that appear in the filing. Resolution is an exact dictionary lookup; unknown or forged handles are dropped and flagged; the legacy `ground` API stays strict; quote verification is unchanged. |
| 3 | **User goal vs search hint, scope first** | `py/agent/planner.py`, `critic.py`, `models.py`, verifier | Each subquery carries an extractive `goal` that must be a contiguous span of the user prompt (≤400 characters). The critic payload separates `requested_goal`, `search_query`, `initial_search_query` and an immutable `current_scope` taken from the filters; the scope contract has top priority so shared "each company/year" wording applies only inside the current scope, while explicit user qualifiers stay required and search vocabulary never becomes a requirement. The threshold of 4 and the "missing element scores ≤3" rule are unchanged. |
| 4 | **Narrow empty-evidence stalled-rewrite stop** | `py/agent/state_machine.py`, `models.py`, `bench_kit/eval_agent.py`, verifier | Only when a hop has no verified evidence (score 1), is not the final attempt, and the critic's rewrite is unchanged after strip/casefold, the subquery stops with `terminal_reason = "stalled_rewrite"` and the critic's actual proposal recorded. It adds an explicit stop-reason flag next to the true retry-count flag, makes no extra model call or retry, and publishes no evidence. An unchanged rewrite *with* evidence still raises; a final-attempt unchanged rewrite is ordinary exhaustion. |

**Intentional execution-contract change.** The contract moved from "an unavailable scope always exhausts 2 retries" to "**at most 2 retries**, plus the one verified early stop above".
Legacy traces without the new fields are still verified under the original exhaustion rule. No source, citation, filter, bound, coverage or
available-topic-gap gate was lowered; a stalled available scope would still be an unpublished, flagged gap. That fails this task's stricter all-available-topics publication comparison; the base verifier's aggregate coverage floor stays at 80 %, so an isolated partial case does not by itself fail every base gate.

Bounded real-model checks of the critic changes (authenticated Claude calls, sequential: 2 per lease-contrast run, 6 for the scope probes; scores are as returned, not forced; regression probes, not accuracy):

* **Lease contrast** on the saved `full-run-02` `audit07` context, same search phrase and same source-verified evidence: goal A "operating lease obligations" scored **5**; goal B (an explicit year-by-year
  maturity schedule) scored **2**. The contract (A ≥ 4, B < 4) was met in both saved runs ([`contrast-audit07.json`](contrast-audit07.json), [`contrast-audit07-scope.json`](contrast-audit07-scope.json)).
* **Scope probes** replaying the production critic on saved evidence ([`scope-replay.json`](scope-replay.json), `passed: true`, 6 calls): UNH 5 and CVS 5 on the shared "each company" goal (`broad21`), SBUX 2024 4 and SBUX 2025 4 (`broad22`), HPE 2024 5 and HPE 2025 5 (`broad25`).
  The probe harness was an independent replay and is not a repository script; its raw output is the saved file.

## Tests (code frozen before the final run)

* **81 application tests** and **168 benchmark-contract tests** passed ([`python-tests-stall-final.log`](verification/python-tests-stall-final.log),
  [`benchmark-tests-stall-final.log`](verification/benchmark-tests-stall-final.log); the benchmark log contains deliberate negative-fixture output). They cover
  the stalled-stop path with fixtures only — not live evidence.
* Earlier intermediate logs are retained in [`verification/`](verification/) (65/152, 72/160 and intermediate stages).
* **Go source was not changed in this work.** Go race and vet were **not** freshly rerun here; the earlier recorded results are historical.
* Test fixtures are labelled test doubles; they establish contracts, not model quality.

## All 30 cases (`full-run-05`)

Prompt column shows the first 110 characters; the full prompt is in each raw JSON and in the [frozen suite copy](full-run-05/suite.json).
"Cited / available" counts available planned subqueries with an independently verified citation; expected-unavailable scopes publish none. Flags are the
published evidence-gap notices; `—` means none.

| # | Case | Kind | Prompt (start) | Requested filings, section, availability | Citations | Cited / available | Retries | Seconds | Outcome | Flags | Raw |
|---:|---|---|---|---|---:|---|---:|---:|---|---|---|
| 1 | `audit01` | regression | Compare AAPL filing year 2023 and NVDA filing year 2024: find the disclosed dependence on third-party manufact… | AAPL filing year 2023, whole filing — available (120 passages)<br>NVDA filing year 2024, whole filing — available (204 passages) | 8 | 2/2 | 0 | 23.01 | supported | — | [json](full-run-05/evaluation/audit01.json) · [md](full-run-05/evaluation/audit01.md) |
| 2 | `audit02` | regression | Compare MSFT and AMZN, both filing year 2024: retrieve evidence on cloud business competition and the operatin… | MSFT filing year 2024, whole filing — available (210 passages)<br>AMZN filing year 2024, whole filing — available (168 passages) | 8 | 2/2 | 2 | 47.10 | supported | — | [json](full-run-05/evaluation/audit02.json) · [md](full-run-05/evaluation/audit02.md) |
| 3 | `audit03` | regression | Audit NVDA filing year 2024: separately retrieve export control risks and dependence on external manufacturing… | NVDA filing year 2024, whole filing — available (204 passages) | 8 | 2/2 | 0 | 20.25 | supported | — | [json](full-run-05/evaluation/audit03.json) · [md](full-run-05/evaluation/audit03.md) |
| 4 | `audit04` | regression | Audit AMZN filing year 2024: separately retrieve AWS sales disclosures and operating expense disclosures. Supp… | AMZN filing year 2024, whole filing — available (168 passages) | 6 | 2/2 | 0 | 25.27 | supported | — | [json](full-run-05/evaluation/audit04.json) · [md](full-run-05/evaluation/audit04.md) |
| 5 | `audit05` | regression | Audit AAPL filing year 2023: separately retrieve share repurchase authorization disclosures and cash dividend… | AAPL filing year 2023, whole filing — available (120 passages) | 5 | 2/2 | 0 | 16.97 | supported | — | [json](full-run-05/evaluation/audit05.json) · [md](full-run-05/evaluation/audit05.md) |
| 6 | `audit06` | regression | Audit MSFT filing year 2024: separately retrieve cloud services growth disclosures and cybersecurity risks. Gr… | MSFT filing year 2024, whole filing — available (210 passages) | 7 | 2/2 | 0 | 19.35 | supported | — | [json](full-run-05/evaluation/audit06.json) · [md](full-run-05/evaluation/audit06.md) |
| 7 | `audit07` | regression | Audit AMZN filing year 2024: separately retrieve liquidity and cash-flow disclosures and operating lease oblig… | AMZN filing year 2024, whole filing — available (168 passages) | 8 | 2/2 | 1 | 29.89 | supported | — | [json](full-run-05/evaluation/audit07.json) · [md](full-run-05/evaluation/audit07.md) |
| 8 | `audit08` | regression | Audit NVDA filing year 2024: separately retrieve Data Center revenue growth disclosures and gross margin discl… | NVDA filing year 2024, whole filing — available (204 passages) | 7 | 2/2 | 0 | 21.11 | supported | — | [json](full-run-05/evaluation/audit08.json) · [md](full-run-05/evaluation/audit08.md) |
| 9 | `audit09` | regression | Audit AAPL filing year 2025 only: separately retrieve revenue disclosures and share repurchase disclosures. Do… | AAPL filing year 2025, whole filing — available (84 passages) | 8 | 2/2 | 0 | 22.72 | supported | — | [json](full-run-05/evaluation/audit09.json) · [md](full-run-05/evaluation/audit09.md) |
| 10 | `audit10` | regression | Audit MSFT filing year 2024, with the exact SectionItem filter "Item 1A": separately retrieve cybersecurity ri… | MSFT filing year 2024, Item 1A — **exact_section_absent** (0 passages) | 0 | 0/0 | 4 | 18.23 | expected_unavailable | no evidence after 2 retries (expected, unavailable side); abstention notice | [json](full-run-05/evaluation/audit10.json) · [md](full-run-05/evaluation/audit10.md) |
| 11 | `broad11` | positive | Compare KO and PG, both filing year 2025, with the exact SectionItem filter "Item 7": retrieve each company's… | KO filing year 2025, Item 7 — available (68 passages)<br>PG filing year 2025, Item 7 — available (54 passages) | 5 | 2/2 | 0 | 26.34 | supported | — | [json](full-run-05/evaluation/broad11.json) · [md](full-run-05/evaluation/broad11.md) |
| 12 | `broad12` | positive | Compare PFE and MRK, both filing year 2025, with the exact SectionItem filter "Item 1A": retrieve each company… | PFE filing year 2025, Item 1A — available (45 passages)<br>MRK filing year 2025, Item 1A — available (43 passages) | 7 | 2/2 | 0 | 20.42 | supported | — | [json](full-run-05/evaluation/broad12.json) · [md](full-run-05/evaluation/broad12.md) |
| 13 | `broad13` | positive | Audit BAC filing year 2026, with the exact SectionItem filter "Item 7": separately retrieve regulatory capital… | BAC filing year 2026, Item 7 — available (164 passages) | 6 | 2/2 | 0 | 24.07 | supported | — | [json](full-run-05/evaluation/broad13.json) · [md](full-run-05/evaluation/broad13.md) |
| 14 | `broad14` | positive | Audit TGT filing year 2026: separately retrieve the inventory shrink estimate and its sensitivity, and the cap… | TGT filing year 2026, whole filing — available (99 passages) | 5 | 2/2 | 0 | 20.22 | supported | — | [json](full-run-05/evaluation/broad14.json) · [md](full-run-05/evaluation/broad14.md) |
| 15 | `broad15` | positive | Audit TSLA filing year 2026: separately retrieve tariff and trade-policy risks affecting supply chain costs, a… | TSLA filing year 2026, whole filing — available (181 passages) | 8 | 2/2 | 0 | 28.13 | supported | — | [json](full-run-05/evaluation/broad15.json) · [md](full-run-05/evaluation/broad15.md) |
| 16 | `broad16` | positive | Compare V filing year 2024 and V filing year 2025, with the exact SectionItem filter "Item 1A": retrieve the d… | V filing year 2024, Item 1A — available (46 passages)<br>V filing year 2025, Item 1A — available (48 passages) | 8 | 2/2 | 0 | 19.62 | supported | — | [json](full-run-05/evaluation/broad16.json) · [md](full-run-05/evaluation/broad16.md) |
| 17 | `broad17` | positive | Compare COP and OXY, both filing year 2025, with the exact SectionItem filter "Item 7": retrieve each company'… | COP filing year 2025, Item 7 — available (70 passages)<br>OXY filing year 2025, Item 7 — available (74 passages) | 6 | 2/2 | 0 | 22.74 | supported | — | [json](full-run-05/evaluation/broad17.json) · [md](full-run-05/evaluation/broad17.md) |
| 18 | `broad18` | positive | Compare MET and AFL, both filing year 2025: retrieve MET's interest rate risk disclosures and AFL's yen and fo… | MET filing year 2025, whole filing — available (490 passages)<br>AFL filing year 2025, whole filing — available (306 passages) | 7 | 2/2 | 0 | 18.74 | supported | — | [json](full-run-05/evaluation/broad18.json) · [md](full-run-05/evaluation/broad18.md) |
| 19 | `broad19` | positive | Compare CRM and ADBE, both filing year 2026, with the exact SectionItem filter "Item 8": retrieve each company… | CRM filing year 2026, Item 8 — available (74 passages)<br>ADBE filing year 2026, Item 8 — available (72 passages) | 6 | 2/2 | 0 | 20.31 | supported | — | [json](full-run-05/evaluation/broad19.json) · [md](full-run-05/evaluation/broad19.md) |
| 20 | `broad20` | positive | Compare AMD and TXN, both filing year 2026: retrieve AMD's dependence on third-party foundries such as TSMC an… | AMD filing year 2026, whole filing — available (165 passages)<br>TXN filing year 2026, whole filing — available (76 passages) | 6 | 2/2 | 0 | 20.20 | supported | — | [json](full-run-05/evaluation/broad20.json) · [md](full-run-05/evaluation/broad20.md) |
| 21 | `broad21` | positive | Compare UNH filing year 2025 and CVS filing year 2026, both with the exact SectionItem filter "Item 7": retrie… | UNH filing year 2025, Item 7 — available (25 passages)<br>CVS filing year 2026, Item 7 — available (56 passages) | 5 | 2/2 | 0 | 19.16 | supported | — | [json](full-run-05/evaluation/broad21.json) · [md](full-run-05/evaluation/broad21.md) |
| 22 | `broad22` | positive | Compare SBUX filing year 2024 and SBUX filing year 2025, with the exact SectionItem filter "Item 7": retrieve… | SBUX filing year 2024, Item 7 — available (32 passages)<br>SBUX filing year 2025, Item 7 — available (34 passages) | 8 | 2/2 | 0 | 34.46 | supported | — | [json](full-run-05/evaluation/broad22.json) · [md](full-run-05/evaluation/broad22.md) |
| 23 | `broad23` | positive | Audit ISRG filing year 2026, with the exact SectionItem filter "Item 7": separately retrieve tariff impacts on… | ISRG filing year 2026, Item 7 — available (63 passages) | 6 | 2/2 | 0 | 20.96 | supported | — | [json](full-run-05/evaluation/broad23.json) · [md](full-run-05/evaluation/broad23.md) |
| 24 | `broad24` | positive | Compare LLY and JNJ, both filing year 2026, with the exact SectionItem filter "Item 7": retrieve LLY's product… | LLY filing year 2026, Item 7 — available (28 passages)<br>JNJ filing year 2026, Item 7 — available (37 passages) | 7 | 2/2 | 0 | 20.05 | supported | — | [json](full-run-05/evaluation/broad24.json) · [md](full-run-05/evaluation/broad24.md) |
| 25 | `broad25` | positive | Compare HPE filing year 2024 and HPE filing year 2025, with the exact SectionItem filter "Item 7": retrieve sh… | HPE filing year 2024, Item 7 — available (61 passages)<br>HPE filing year 2025, Item 7 — available (61 passages) | 4 | 2/2 | 0 | 23.52 | supported | — | [json](full-run-05/evaluation/broad25.json) · [md](full-run-05/evaluation/broad25.md) |
| 26 | `broad26` | positive | Compare HD and WMT, both filing year 2026, with the exact SectionItem filter "Item 8": retrieve each company's… | HD filing year 2026, Item 8 — available (67 passages)<br>WMT filing year 2026, Item 8 — available (61 passages) | 8 | 2/2 | 0 | 25.63 | supported | — | [json](full-run-05/evaluation/broad26.json) · [md](full-run-05/evaluation/broad26.md) |
| 27 | `broad27` | negative | Audit TSLA filing year 2024 only: separately retrieve automotive gross margin disclosures and tariff or supply… | TSLA filing year 2024, whole filing — **filing_year_absent** (0 passages) | 0 | 0/0 | 4 | 20.09 | expected_unavailable | no evidence after 2 retries (expected, unavailable side); abstention notice | [json](full-run-05/evaluation/broad27.json) · [md](full-run-05/evaluation/broad27.md) |
| 28 | `broad28` | negative | Audit NVDA filing year 2024, with the exact SectionItem filter "Item 7": separately retrieve Data Center reven… | NVDA filing year 2024, Item 7 — **exact_section_absent** (0 passages) | 0 | 0/0 | 4 | 20.09 | expected_unavailable | no evidence after 2 retries (expected, unavailable side); abstention notice | [json](full-run-05/evaluation/broad28.json) · [md](full-run-05/evaluation/broad28.md) |
| 29 | `broad29` | mixed | Compare GS filing year 2026 and BAC filing year 2025: retrieve each firm's regulatory capital ratio disclosure… | GS filing year 2026, whole filing — available (507 passages)<br>BAC filing year 2025, whole filing — **filing_year_absent** (0 passages) | 5 | 1/1 | 2 | 28.48 | supported | no evidence after 2 retries (expected, unavailable side) | [json](full-run-05/evaluation/broad29.json) · [md](full-run-05/evaluation/broad29.md) |
| 30 | `broad30` | mixed | Compare KO filing year 2025 and AMZN filing year 2024, both with the exact SectionItem filter "Item 7": retrie… | KO filing year 2025, Item 7 — available (68 passages)<br>AMZN filing year 2024, Item 7 — **exact_section_absent** (0 passages) | 6 | 1/1 | 2 | 25.57 | supported | no evidence after 2 retries (expected, unavailable side) | [json](full-run-05/evaluation/broad30.json) · [md](full-run-05/evaluation/broad30.md) |

Column totals: **178** citations, **52/52** cited/available, **19** retries, 79 retrievals, 136 model calls. Two raw-file notes: the three expected-unavailable cases exhaust two retries on their absent scopes
(their gap flags are expected by the suite); the mixed cases `broad29` and `broad30` each flag only the absent side.

## Known limitations and unresolved items

1. Source-informed fixtures, not human gold; no answer-accuracy, financial-correctness or production-quality claim. Extractive quotes only.
2. One live run per attempt with a nondeterministic LLM; the final pass is an observation, not a guarantee of identical behaviour on a rerun.
3. The stalled-rewrite stop is verified by tests and the independent verifier's mutation checks but **has not been observed in a live run**.
4. The `broad22` (full-run-03) and `broad29` (full-run-04) failures left no hop traces; their exact mechanisms are unknown. The scope explanation for `broad22` is an inference.
5. The corpus is mixed-lineage (parser v3 only for one filing); the suite has no Morgan Stanley case.
6. Not fresh here: the 100-query retrieval benchmark, the 50-client HTTP load benchmark and Go race/vet. The public-snapshot hygiene check is not part of the strict verifier; it is a separate independent check recorded in [`verification/public-snapshot-check.json`](verification/public-snapshot-check.json).
7. Local, loopback-bound, non-isolated desktop; no tenant isolation or availability claims.

No deployment was performed. Reproduction commands: [`docs/build/amzn-expense.md`](../../../docs/build/amzn-expense.md).
