# Broad validation suite `broad-validation-v1` — live run, November 1, 2026

All **30** frozen cases ran once against the 49,452-passage expanded corpus (152 trusted SEC filings), the sparse shard router, PQ128 and the FP32 ONNX reranker. Codex independently reran the offline verifier: **30/30 cases verified, zero transport/model/structural failures, no verification problems, no evaluator-summary discrepancy.**

> **Scope.** The suite is a set of *source-informed regression fixtures*: ten historical prompts verbatim plus twenty written after reading the cited passages. It is **not** independent human gold, **not** a semantic answer-accuracy score, **not** retrieval nDCG/recall, and **not** load latency (one sequential run on a non-isolated CPU host). Outcomes below are structural and source-grounding results. **Do not read "26 supported" as 26 complete answers**: it means every planned subquery on an *available* scope has at least one independently verified published quote.

## Result

| Measure | Value |
|---|---:|
| Cases verified / frozen | **30 / 30** (zero failures: transport 0, model 0, structural 0) |
| Evidence outcomes | **26 supported** (includes the 2 mixed cases, supported on their available side), **1 partial gap**, **3 expected unavailable**, 0 abstained |
| Independently verified published citations | **172** |
| Retrieval calls / retries | **86 / 24** (breakdown below) |
| Median case time | **23.641233972500004 s** (min 18.0 s, max 47.7 s) |
| Actual Claude calls (ledger) | **143** (`claude-haiku-5-5`; 30 Plan, 86 Critique, 27 Draft) |
| Available-scope subquery coverage | **53 / 54 = 98.1481 %** (the one miss: HPE 2024) |
| Mixed cases | `broad29` available side **2/2**, `broad30` **1/1**; unavailable scopes withheld in both |
| Unique source passages re-checked against the original bytes | **263** |
| Companies with at least one verified citation | **30** = the 4 original (AAPL, MSFT, AMZN, NVDA) + 25 new positive-case companies + GS (mixed case `broad29`) |
| Reviewer anchors (informational) | 34 instances over **33 unique** anchors; 30 retrieved, 27 cited. Not recall, not accuracy |
| Run window | 2026-11-01 18:53:31 → 19:06:28 UTC (includes service start-up) |

Why "fully covered positive cases" in `evaluation/report.json` reads **24**: that evaluator field requires every planned subquery to be cited, so it excludes the two mixed cases (whose unavailable subqueries are correctly uncited). 24 + 2 mixed = the 26 supported.

The 24 retries break down as: **6 that recovered evidence** (`audit02` 1, `audit04` 1, `audit07` 2, `broad16` 1, `broad18` 1); **2 that did not** (`broad25`, both on HPE 2024); and **16 spent by correctly unavailable scopes** (`audit10` 4, `broad27` 4, `broad28` 4, `broad29` 2, `broad30` 2). The 24 searches on unavailable scopes returned no candidates, which is why the sidecar's rerank count (62) is 24 below its dense count (86).

## All 30 cases

"Avail. cited" = published-quote coverage of planned subqueries on available scopes (cited/available); unavailable scopes are required to publish nothing, so they are not in the denominator. Calls/retries counts retrieval requests and retried requests. Each ID links to the complete published answer (markdown) and the full trace JSON (plan, every hop, critic scores and rationales, carried passage IDs, citations, model-call ledger).

| Case | Kind | Scope | Topics | Outcome | Avail. cited | Citations | Calls / retries | Seconds | Notes, flags and gaps |
|---|---|---|---|---|---:|---:|---:|---:|---|
| [audit01](run-01/evaluation/audit01.md) · [JSON](run-01/evaluation/audit01.json) | regression | AAPL 2023; NVDA 2024 | third-party manufacturing and supply-chain risks | supported | 2/2 | 7 | 2 / 0 | 23.9 | — |
| [audit02](run-01/evaluation/audit02.md) · [JSON](run-01/evaluation/audit02.json) | regression | MSFT 2024; AMZN 2024 | cloud competition and infrastructure operating costs | supported | 2/2 | 8 | 3 / 1 | 35.9 | AMZN subquery scored 3, then 4 after one retry carrying 2 passages. This prompt had stayed partial in the earlier ten-audit run. |
| [audit03](run-01/evaluation/audit03.md) · [JSON](run-01/evaluation/audit03.json) | regression | NVDA 2024 | export-control risks; external manufacturing dependence | supported | 2/2 | 8 | 2 / 0 | 24.1 | — |
| [audit04](run-01/evaluation/audit04.md) · [JSON](run-01/evaluation/audit04.json) | regression | AMZN 2024 | AWS sales; operating expense disclosures | supported | 2/2 | 4 | 3 / 1 | 30.4 | Operating-expense subquery scored 3, then 5 after one retry carrying 2 passages. |
| [audit05](run-01/evaluation/audit05.md) · [JSON](run-01/evaluation/audit05.json) | regression | AAPL 2023 | share repurchase authorization; cash dividends | supported | 2/2 | 6 | 2 / 0 | 23.4 | — |
| [audit06](run-01/evaluation/audit06.md) · [JSON](run-01/evaluation/audit06.json) | regression | MSFT 2024 | cloud services growth; cybersecurity risks | supported | 2/2 | 7 | 2 / 0 | 20.3 | — |
| [audit07](run-01/evaluation/audit07.md) · [JSON](run-01/evaluation/audit07.json) | regression | AMZN 2024 | liquidity and cash flows; operating lease obligations | supported | 3/3 | 7 | 5 / 2 | 47.7 | Lease subquery scored 3, 3, then 5 over two retries, carrying 2 passages each time. |
| [audit08](run-01/evaluation/audit08.md) · [JSON](run-01/evaluation/audit08.json) | regression | NVDA 2024 | Data Center revenue growth; gross margin | supported | 2/2 | 5 | 2 / 0 | 20.6 | — |
| [audit09](run-01/evaluation/audit09.md) · [JSON](run-01/evaluation/audit09.json) | regression | AAPL 2025 | revenue; share repurchases | supported | 2/2 | 7 | 2 / 0 | 23.3 | — |
| [audit10](run-01/evaluation/audit10.md) · [JSON](run-01/evaluation/audit10.json) | regression | MSFT 2024 · Item 1A | cybersecurity and cloud-competition risk, exact Item 1A | expected unavailable | 0/0 | 0 | 6 / 4 | 18.0 | Expected unavailable: MSFT 2024 has no exact Item 1A passages. Both subqueries exhausted 2 retries; zero claims; no other section/year substituted. |
| [broad11](run-01/evaluation/broad11.md) · [JSON](run-01/evaluation/broad11.json) | positive | KO 2025; PG 2025 · Item 7 | foreign-currency effect on revenue/sales | supported | 2/2 | 6 | 2 / 0 | 24.9 | — |
| [broad12](run-01/evaluation/broad12.md) · [JSON](run-01/evaluation/broad12.json) | positive | PFE 2025; MRK 2025 · Item 1A | patent protection and generic/biosimilar competition risk | supported | 2/2 | 8 | 2 / 0 | 19.4 | — |
| [broad13](run-01/evaluation/broad13.md) · [JSON](run-01/evaluation/broad13.json) | positive | BAC 2026 · Item 7 | CET1 capital ratios; liquidity coverage ratio | supported | 2/2 | 6 | 2 / 0 | 22.7 | — |
| [broad14](run-01/evaluation/broad14.md) · [JSON](run-01/evaluation/broad14.json) | positive | TGT 2026 | inventory shrink estimate/sensitivity; capital expenditures | supported | 2/2 | 6 | 2 / 0 | 20.1 | — |
| [broad15](run-01/evaluation/broad15.md) · [JSON](run-01/evaluation/broad15.json) | positive | TSLA 2026 | tariff/trade-policy supply-chain cost risk; cost of revenues and gross margin | supported | 2/2 | 7 | 2 / 0 | 25.4 | One model-proposed quote for `...#item7#passage-000020` was rejected by exact-quote grounding (flag kept); 7 other quotes verified and published. The rejected text is not stored in the trace. |
| [broad16](run-01/evaluation/broad16.md) · [JSON](run-01/evaluation/broad16.json) | positive | V 2024; V 2025 · Item 1A | interchange-rate regulation risk, two filing years | supported | 2/2 | 8 | 3 / 1 | 30.9 | V 2024 subquery scored 3, then 5 after one retry carrying 2 passages. |
| [broad17](run-01/evaluation/broad17.md) · [JSON](run-01/evaluation/broad17.json) | positive | COP 2025; OXY 2025 · Item 7 | commodity-price effect on earnings, cash flows, valuation | supported | 2/2 | 8 | 2 / 0 | 28.2 | — |
| [broad18](run-01/evaluation/broad18.md) · [JSON](run-01/evaluation/broad18.json) | positive | MET 2025; AFL 2025 | MET interest-rate risk; AFL yen translation | supported | 2/2 | 8 | 3 / 1 | 32.2 | MET subquery scored 3, then 4 after one retry carrying 2 passages. |
| [broad19](run-01/evaluation/broad19.md) · [JSON](run-01/evaluation/broad19.json) | positive | CRM 2026; ADBE 2026 · Item 8 | remaining performance obligations | supported | 2/2 | 6 | 2 / 0 | 23.9 | — |
| [broad20](run-01/evaluation/broad20.md) · [JSON](run-01/evaluation/broad20.json) | positive | AMD 2026; TXN 2026 | AMD foundry dependence; TXN manufacturing capex | supported | 2/2 | 6 | 2 / 0 | 20.6 | — |
| [broad21](run-01/evaluation/broad21.md) · [JSON](run-01/evaluation/broad21.json) | positive | UNH 2025; CVS 2026 · Item 7 | medical care / medical benefit ratio | supported | 2/2 | 5 | 2 / 0 | 21.6 | — |
| [broad22](run-01/evaluation/broad22.md) · [JSON](run-01/evaluation/broad22.json) | positive | SBUX 2024; SBUX 2025 · Item 7 | comparable store sales and new stores, two filing years | supported | 2/2 | 7 | 2 / 0 | 32.0 | — |
| [broad23](run-01/evaluation/broad23.md) · [JSON](run-01/evaluation/broad23.json) | positive | ISRG 2026 · Item 7 | tariff product-cost impact; da Vinci procedure growth | supported | 2/2 | 7 | 2 / 0 | 22.5 | — |
| [broad24](run-01/evaluation/broad24.md) · [JSON](run-01/evaluation/broad24.json) | positive | LLY 2026; JNJ 2026 · Item 7 | LLY product revenue drivers; JNJ segment sales | supported | 2/2 | 6 | 2 / 0 | 21.2 | — |
| [broad25](run-01/evaluation/broad25.md) · [JSON](run-01/evaluation/broad25.json) | positive | HPE 2024; HPE 2025 · Item 7 | share repurchase authorization/activity, two filing years | **partial gap** | 1/2 | 2 | 4 / 2 | 38.5 | **Partial.** HPE 2024 subquery (index 0) stayed at score 3 through both retries and has no published claim; HPE 2025 cited (2 quotes). See [HPE 2024 gap](#hpe-2024-partial-case). |
| [broad26](run-01/evaluation/broad26.md) · [JSON](run-01/evaluation/broad26.json) | positive | HD 2026; WMT 2026 · Item 8 | segment reporting | supported | 2/2 | 6 | 2 / 0 | 20.8 | — |
| [broad27](run-01/evaluation/broad27.md) · [JSON](run-01/evaluation/broad27.json) | negative | TSLA 2024 | automotive gross margin; tariff/supply-chain risk (filing year absent) | expected unavailable | 0/0 | 0 | 6 / 4 | 18.4 | Expected unavailable: TSLA filing year 2024 was never enrolled. Zero claims. |
| [broad28](run-01/evaluation/broad28.md) · [JSON](run-01/evaluation/broad28.json) | negative | NVDA 2024 · Item 7 | Data Center revenue; supply cost commentary, exact Item 7 (section absent) | expected unavailable | 0/0 | 0 | 6 / 4 | 19.9 | Expected unavailable: NVDA 2024 is unclassified legacy text; no exact Item 7. Zero claims. |
| [broad29](run-01/evaluation/broad29.md) · [JSON](run-01/evaluation/broad29.json) | mixed | GS 2026; BAC 2025 | regulatory capital ratios (GS available, BAC 2025 absent) | supported (available side) | 2/2 | 6 | 5 / 2 | 35.5 | Mixed. GS 2026: both available subqueries cited. BAC 2025 (subquery 2) is unavailable: 3 searches, no evidence, zero claims. |
| [broad30](run-01/evaluation/broad30.md) · [JSON](run-01/evaluation/broad30.json) | mixed | KO 2025; AMZN 2024 · Item 7 | foreign-currency impact, exact Item 7 (KO available, AMZN 2024 section absent) | supported (available side) | 1/1 | 5 | 4 / 2 | 25.0 | Mixed. KO 2025 Item 7 cited. AMZN 2024 exact Item 7 unavailable (legacy unclassified filing): 3 searches, no evidence, zero claims. |

Totals recomputed from the table rows: citations 172, retrieval calls 86, retries 24, model calls 143.

## HPE 2024 partial case

`broad25` ("share repurchase authorization and repurchase activity" for HPE filing years 2024 and 2025, exact Item 7) is the one available-scope subquery without a published claim. **This is a retrieval/critic-threshold outcome, not evidence that the disclosure is absent.**

- The HPE 2024 subquery scored **3 on all three attempts** (initial + two retries) and was never accepted. The HPE 2025 subquery was accepted at score 4 on its first search (2 quotes: $202 million repurchased and settled; about $3.6 billion remaining authorization).
- The retrieved HPE 2024 context contained adjacent passages `000164559024000139#item7#passage-000035` and `…#passage-000036`, retained on both retries. Passage 35 ends mid-sentence ("…in connection with the share repurchase program previously authorized by our Board of Directors, during fiscal 2024, we repurchased") and passage 36 begins "and settled an aggregate amount of $150 million. As of October 31, 2024, we had a remaining authorization of approximately $0.8 billion for future share repurchases." So the activity amount and remaining authorization are in the corpus, split across a passage boundary.
- The critic's rationales (see the `broad25` JSON hops) kept asking for the **original authorization size and the share count / total shares repurchased**, which those passages do not state, and treated the split sentence as incomplete. The agent therefore published nothing for this subquery and flagged `Subquery 0 has no evidence meeting threshold after 2 retries.`
- Nothing was tuned or rerun to hide this. Plausible contributors (not tested here): the sentence is split across the passage boundary, and the critic asked for more than the subquery required. Any change to chunking or critic behaviour must be a new, versioned evaluation.

## Other notes kept visible

- **`broad15` (TSLA):** the synthesizer proposed one quote referencing `000162828026003952#item7#passage-000020` that did not match the source verbatim and was dropped (`Dropped unverified claim…`). No unverified claim was published; all 7 published TSLA quotes verify.
- **Unavailable scopes** (`audit10`, `broad27`, `broad28`, and the BAC 2025 / AMZN 2024-Item 7 sides of `broad29`/`broad30`) each used all three searches, returned no evidence, and published zero claims; the expected-unavailable result is the required behaviour, not a failure.
- **Run provenance:** `git_head` recorded as `1c333b4` with 22 uncommitted paths at run time. The code hashes (53 files incl. sidecar and Go sources) are in [`artifact-hashes.json`](run-01/artifact-hashes.json); strict re-verification found no drift.

## Setup and reproduction

Services (supervised by `scripts/evaluate-expanded-agent.py`, owned processes stopped afterwards): sidecar `py/service.py` with **four inference workers × two threads**, FP32 ONNX reranker batch 1, dense batch 8 with pressure trigger 8; Go bridge `cmd/finfilings` over the sparse shard index (`indexes/sparse-sec-scale-v2-20261031`, 25 segment files), dense `indexes/vector-sec-scale-v2-pq128-20261031`, reranker `models/reranker-onnx-fp32-optimized-20261031`. Agent bounds unchanged: 2–3 subqueries, ≤2 retries each, ≤5 context passages, ≤2 carried, ≤3 concurrent subqueries, ≤2 concurrent Claude calls. Requested model alias `haiku`; observed `claude-haiku-5-5`. Python 3.10.12.

```bash
export PYTHONPATH="$PWD/models/runtime:$PWD/py" HF_HOME="$PWD/models/hf-cache" HF_HUB_OFFLINE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2 GOCACHE=/tmp/finfilings-go-cache
SUITE_SHA=$(cut -d' ' -f1 bench_kit/suites/broad-validation-v1.sha256)   # 0e8c02d31b8f4cef03f39adc43335f3bc4a34986a37d987e7c9cabdad7ed4fae
python3 scripts/evaluate-expanded-agent.py --out bench_kit/results/broad-validation-RERUN \
  --suite bench_kit/suites/broad-validation-v1.json --expected-suite-sha256 "$SUITE_SHA"   # fresh --out; needs an authenticated Claude CLI
python3 bench_kit/verify_broad_agent.py --directory bench_kit/results/broad-validation-RERUN --enforce-gates --strict-code
```

Offline re-verification of this run (no services or model needed; `--strict-code` passes only while the recorded code files are unchanged, otherwise omit it to get drift reported separately):

```bash
python3 bench_kit/verify_broad_agent.py --directory bench_kit/results/broad-validation-20261101/run-01 --output /tmp/reverify.json --enforce-gates
```

The existing `independent-broad-verification.json` was written by the verifier and is never overwritten. A rerun is a new sample: model outputs vary.

## Release gates (set before inference)

Gates are quoted in [`release-gates.md`](release-gates.md) and in `bench_kit/suites/README.md`.

| Gate | Outcome | Evidence |
|---|---|---|
| Suite and checksums frozen before inference; all 30 outcomes retained | **Met** — suite SHA `0e8c02d3…ed4fae` pinned; 30/30 verified, none omitted | [`run-01/suite.sha256`](run-01/suite.sha256), [verification JSON](run-01/independent-broad-verification.json), [log](verification/independent-broad-verification.log) |
| No missing cases, transport/model exceptions, citation/provenance violations, filter leakage, retry/context bound violations, fabricated claims | **Met** within what is checkable offline — 0 failures, 0 problems; one dropped unverified proposal kept visible. Concurrency bounds are **not provable from traces** (no start times); the final sidecar counters show admission active 0, peak 3, rejected 0 | [health-after](run-01/health-after.json), [evaluation log](run-01/evaluation.log) |
| Unavailable scopes publish zero claims; mixed cases preserve the unavailable scope and fully support the available side | **Met** — `audit10`, `broad27`, `broad28` zero claims; `broad29` 2/2 and `broad30` 1/1 available side cited, unavailable sides withheld | verification JSON `mixed_cases`, `release_gates` |
| ≥80 % of planned available-scope subqueries have verified citation coverage (structural, not semantic accuracy) | **Met: 53/54 = 98.1481 %**. Denominator = planned subqueries on available scopes (the verifier's reading of "supported planned subqueries"). The one miss is HPE 2024 | `release_gates.citation_coverage_of_available_subqueries` |
| Complete Python suite and applicable Go race/vet pass | **Met** (Codex runs) — 40 application tests, 50 bench-contract tests (90 total; the 50 include the 48 new broad-suite tests), Go race ok, `go vet` empty (clean) | [python](verification/python-tests.log), [bench contract](verification/benchmark-contract-tests.log), [go race](verification/go-race.log), [go vet](verification/go-vet.log) |
| Public snapshot has no weights, datasets, credentials or private contact information | **Met** (Codex check, 2026-11-01 19:11 UTC): 157 modified/non-ignored untracked files intended for this checkpoint (2,924,536 bytes) scanned; no matches for GitHub/Anthropic token formats, private-key headers or the authorized private SEC contact, and no forbidden local artifacts. The scan excludes its own record and covers only that file set. No push has been made | [public-snapshot-check.json](verification/public-snapshot-check.json) |

Strict artifact verification (`--strict-code`) passed: vector index 5/5, reranker 2/2 and sparse index 28/28 files re-hashed and matched, suite/corpus/ingestion-report/original-provenance hashes matched, 53 code files unchanged, none missing. Raw HTML of the 148 parser-v2 filings is not stored locally, so raw-document identity rests on the ingestion report's recorded URLs and SHA-256 values.

## Limitations

- Source-informed, single-reviewer fixtures on one corpus snapshot and one model sample; not human-labelled, not an accuracy or recall measure. "Supported" means exact, verified quotes cover each planned available subquery, not that an answer is complete or that the critic's judgement is correct (see HPE 2024, where the critic's demand exceeded what the question needed).
- Coverage is structural: a subquery counts as covered with a single published quote.
- Thirty cases (28 with two planned subqueries, 2 with three: `audit07`, `broad29`) are too few for percentile or load claims; times are sequential, warm-service, host-shared.
- Concurrency limits are enforced in code and observed in sidecar counters, not provable from saved traces.
- The corpus gaps documented in `bench_kit/suites/README.md` (Item 8 cross-reference stubs, 43 skipped filings, unclassified legacy filings) are unchanged and unfixed.

## Evidence

[Run status](run-01/run-status.json) · [evaluation report](run-01/evaluation/report.json) · [records ledger](run-01/evaluation/records.jsonl) · [run manifest](run-01/evaluation/run-manifest.json) · [artifact hashes](run-01/artifact-hashes.json) · [enrolled sources](run-01/agent-provenance.json) · [sidecar log](run-01/sidecar.log) · [bridge log](run-01/bridge.log) · [supervisor log](run-01-supervisor.log) · [health before](run-01/health-before.json) / [after](run-01/health-after.json) · frozen suite [`../../suites/broad-validation-v1.json`](../../suites/broad-validation-v1.json)
