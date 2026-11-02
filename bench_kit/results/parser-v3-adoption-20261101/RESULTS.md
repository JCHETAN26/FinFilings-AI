# Parser-v3 adoption — Morgan Stanley targeted replacement (November 1, 2026)

**Status: measurements complete and independently verified; every pre-registered gate passes (`gates.json`, OVERALL PASS).** Retrieval/load
and the 30-case agent audit were all run on the same new corpus and verified independently by Codex (strict code check, `--enforce-gates`).
Adoption of the corpus version is validated and the reproduction tooling is ready; **published as commit `6f18c04` (recorded in Git history); no deployment performed.**
Method, guards and commands: [`docs/build/parser-v3-adoption.md`](../../../docs/build/parser-v3-adoption.md).

**Read first — two things not to over-read:** (1) **dense-only nDCG@10 fell by 0.01497489942688951** (0.48229729941572386 → 0.46732239998883435);
the fused and reranked metrics, which are the gated ones, did not fall. (2) The agent suite contains **no Morgan Stanley case**, so the agent result
does not establish the quality of the v3 MS passages or of parser v3 in general.

## What changed, and what did not

* **Mixed lineage.** `data/sec-scale-v3-evaluation-20261101` is a new corpus version of **49,406** passages (v2: 49,452). One filing,
  Morgan Stanley (`000089542126000086`), was reparsed with `deterministic-canonical-heading-v3-go`; **147 filings were not reparsed**
  and keep parser-v2 output byte for byte, as do the 19,511 baseline passages (49,091 non-MS lines, SHA-256 `3cc8e1d7…23a8e`, identical).
  The corpus is **not** "all v3".
* **MS passages:** 361 → 315: 314 byte-identical, **46 removed** (`item7#passage-000081`…`000126`, Item 7A text that v2 had filed under Item 7),
  **1 changed ID** (`…item7#passage-000080`). No MD&A coverage is gained.
* **Raw source identity.** The current SEC bytes of the MS filing are 9,714,819 bytes, SHA-256 `3002143b…78b2`. The previously archived
  acquisition was 9,714,832 bytes, `57698077…12df`; its HTML is gone and its hash is **not** claimed verified. They are different files.
  The v3 ingestion report records the current identity and keeps the previous one as history (`raw_identity_history`). Equivalence rests on the
  361 parser-v2 replay records matching the active corpus byte for byte, not on the raw files being equal.
  The cause of the 13-byte difference is unknown. No new raw files were obtained for the other 147 filings (their HTML was not retained), so they were
  neither re-fetched nor reparsed; the only source/replay method used was the targeted MS v3 replay of the current SEC bytes.
* **Vectors:** the v2 PQ128 embeddings were reused after checksum verification: **49,405 reused, 1 newly encoded**
  (`…item7#passage-000080`); Codex confirmed the 49,405 cached vectors are bit-identical. **The PQ codebook was retrained** (same seed
  20261030, 20,000 training rows, 25 iterations, 128 sub-vectors) on a corpus with 46 fewer rows, so row positions and the training sample differ.
* Models, revisions, ONNX graph, worker/thread counts, batching and admission limits are identical (`parameters.json` equal;
  inference/service/benchmark code equal to HEAD per [`verification/unchanged-inference-code.json`](verification/unchanged-inference-code.json)).
  Queries and qrels are byte-identical copies.

## Matched quality — same 100 FinQA development queries

Binary published supporting-fact labels on FinQA passages. **Development queries, not held-out; not SEC retrieval accuracy; SEC passages have no
labels.** Values are the saved reports (`*/evaluation/report.json`), recomputed independently from the ranked IDs
([`verification/independent-retrieval-and-load-check.json`](verification/independent-retrieval-and-load-check.json)).

| nDCG@10 | v2-control | v3-candidate | Δ |
|---|---:|---:|---:|
| BM25 (sparse) | 0.4960699622545427 | 0.4960699622545427 | 0 |
| Dense (PQ128) | 0.48229729941572386 | **0.46732239998883435** | **−0.01497489942688951** |
| Hybrid (RRF, k=60) | 0.50780475359511 | 0.5100210113496677 | +0.0022162577545576934 |
| Reranked (top-10) | 0.5537183500706869 | 0.5539997505715865 | +0.0002814005008995313 |
| Exact float32 dense | 0.49350279187113805 | 0.49350279187113805 | 0 |
| PQ recall@10 vs exact float32 | 0.838 | 0.841 | +0.003 |

* **Dense-only nDCG@10 fell by 0.01497, a real drop that is disclosed here even though the fused and reranked metrics passed their gates.**
  The pre-registered gates are on reranked nDCG, PQ recall and payload, not on dense-only nDCG.
* The reranked and hybrid movements are tiny and upward; the recall movement is small. **They are not attributed to the MS fix.** Fresh PQ
  codebook training with fixed settings on a slightly different row set can change quantization and rankings, and 46 fewer MS passages change
  the candidate pool. One run per condition cannot separate those causes, and no significance claim is made. The exact float32 dense nDCG being
  unchanged to every digit is consistent with the MS change not touching the FinQA ranking among exact vectors; it says nothing more.
* Sparse nDCG is identical on the 100 queries.

### Vector payload and memory

| | v2-control | v3-candidate |
|---|---:|---:|
| Documents | 49,452 | 49,406 |
| Raw float32 payload (bytes) | 75,958,272 | 75,887,616 |
| PQ native payload (bytes) | 6,723,072 | 6,717,184 |
| Payload savings | 91.14899296287308 % | **91.14851097707431 %** |
| Median RSS delta, raw probe (bytes) | 76,292,096 | 76,222,464 |
| Median RSS delta, PQ probe (bytes) | 7,966,720 | 8,089,600 |
| Serial end-to-end query p50 / p99 (s) | 1.7127 / 2.8518 | 1.7337 / 2.9966 |

The 91.15 % savings is **native vector payload only** (codes plus FAISS buffers; no model, SQLite, allocator overhead or whole-process RSS). RSS deltas
are observed page-level process memory in isolated probes, not exact index size, and the PQ probe's delta actually rose slightly (7.97 → 8.09 MB). v3 vector manifest: `reused_embeddings 49405`, `newly_encoded_embeddings 1`, embedding 6.417 s, PQ train+add 28.345 s, total 34.772 s
(the v2 index total of 394.3 s encoded 3,981 passages and is not comparable). The serial-query figures are a 100-query sample on a non-isolated
machine.

## Matched 50-client load — 200 requests per endpoint

Hardware: **AMD Ryzen 7 5800H, 8 physical cores / 16 logical CPUs, CPU-only inference, Linux**, non-isolated desktop.
Finite closed-loop, zero think time, new TCP connection per request, 5 warm-up requests excluded, 4 inference workers × 2 threads, ONNX batch 1,
dense batch 8 / trigger 8 / wait 2 ms, Go engine admission 8, HTTP admission 64 (peak 50, 0 rejected in both runs). v2-control ran at
22:38 UTC and v3-candidate at 22:49 UTC, sequentially. Percentiles are nearest-rank over all attempts and were recomputed from the 200 raw
attempts per endpoint. **Failures: 0/200 hybrid and 0/200 dense in each run (400/400 successes each).**

| Hybrid (`/search`) | v2-control | v3-candidate | change |
|---|---:|---:|---:|
| p50 (s) | 40.738980026 | 39.277474295 | −3.6 % |
| p95 (s) | 45.927368196 | 43.206629190 | −5.9 % |
| **p99 (s)** | **47.812667999** | **44.999937571** | **−5.88 %** |
| max (s) | 48.476075372 | 46.197266930 | |
| Wall time (s) / QPS | 168.817 / 1.18472 | 160.749 / 1.24418 | +5.0 % QPS |

| Dense (`/dense`) | v2-control | v3-candidate |
|---|---:|---:|
| p50 (s) | 0.375723541 | 0.341392907 |
| p95 (s) | 0.544229498 | 0.518217658 |
| p99 (s) | 0.575851645 | 0.556806811 |
| QPS | 124.8186 | 124.5425 |

This is a single finite, closed-loop run per condition on a non-isolated desktop (8 physical / 16 logical CPUs) with other processes present. It is **not** an SLA, an
open-loop capacity test or a soak test, and a 5.9 % p99 difference between two sequential runs is not shown to be a real improvement. Hybrid
latency is dominated by cross-encoder work; the corpus change removes 46 long MS passages, which may shorten candidate text, but no causal claim is
made. The older 40.08 s hybrid p99 from `dense-batching-20261031/adaptive-b8` is a different session and is not the comparison.

## Pre-registered gates — status

| Gate | Threshold | Observed | State |
|---|---|---|---|
| Reranked nDCG@10 drop | ≤ 0.005 vs 0.5537183500706869 | +0.00028 | met (no drop) |
| PQ recall@10 drop | ≤ 0.02 vs 0.838 | +0.003 | met |
| Payload reduction | ≥ 90 % | 91.1485 % | met |
| Matched load failures | 0 in baseline and candidate | 0 / 0 | met |
| Hybrid p99 regression | ≤ 10 % | −5.88 % | met |
| Python/Go tests, race, vet | pass | 40 application, 136 benchmark-contract, race incl. real MS, vet — all pass (Codex) | met |
| Agent: same 30 cases | all source/citation/filter/bounds valid; zero unsupported claims or transport failures; absent scopes withheld; mixed available sides fully supported; ≥ 80 % available-subquery citation coverage | 30/30 verified, 0 failures; 51/52 = 98.08 %; absent scopes withheld; both mixed cases 1/1 | met |

`bench_kit/compare_v3_gates.py` was run against `v2-control`, `v3-candidate` and the agent verification: every gate in the quality, load and agent sections passes, nothing not evaluated ([`gates.json`](gates.json), [`verification/gates.log`](verification/gates.log)). Tests (Codex): 40 application, 136 benchmark-contract, `go test -race` including the real MS file, and `go vet` pass. These pre-registered gates are the adoption criteria; passing them is not a claim of improvement.

## Agent audit — `broad-validation-v2`, 30 cases (finished, strictly verified)

Run `agent-run-01` (suite SHA-256 `2d87557b…831d`, corpus `c6220762…7a56f`, 152 enrolled filings, Claude `claude-haiku-5-5`) started 22:52 UTC and finished 23:05 UTC;
service shutdown confirmed (`owned_processes_stopped: true`). Verified offline and independently by `bench_kit/verify_broad_agent.py --strict-code --enforce-gates`
([`independent-broad-verification.json`](agent-run-01/independent-broad-verification.json), [`verification/agent-independent-verification.log`](verification/agent-independent-verification.log)):
`passed: true`, no global problems, no evaluator-summary discrepancies, ingestion lineage `targeted_replacement` (147 parser-v2 / 1 parser-v3) accepted.

| | |
|---|---:|
| Cases verified (source and structure) | **30 / 30**; run_failed 0, verification_failed 0, omitted 0 |
| Transport / model / structural failures | **0 / 0 / 0** |
| Published citations, independently verified | **187** (252 unique source passages checked) |
| Retrieval calls / critic retries / model calls | 85 / 25 / 142 |
| Median seconds per case | 24.366062377000162 |
| Evidence outcomes | **26 supported, 1 partial gap, 0 abstained, 3 expected-unavailable** |
| Available-scope planned subqueries with a verified citation | **51 / 52 = 98.0769 %** (gate ≥ 80 %) |
| Mixed cases (broad29, broad30) | both `mixed_fully_supported`, available side 1/1, unavailable side withheld |
| Artifact hashes recomputed | suite, corpus, ingest report, original provenance; vector index 5/5, reranker 2/2, sparse index 28/28 |
| Code hashes (strict) | 56 files recorded, **56 unchanged**, none changed or missing |
| Final service health | admission limit 64, active 0, peak 2, rejected 0 |

"Supported" means every planned subquery on an *available* scope has a verified published quote — it is a structural outcome, **not** 26 complete or
correct answers, and the verifier is not an answer-accuracy score or independent human gold. Anchors are reviewer information only: 34 anchors, 30 retrieved, 27 cited (informational).

### All 30 cases

Scopes: ✓ = filing year (and exact section when stated) exists in the corpus, ✗ = absent by the frozen expectation. "Cited / available" counts only subqueries on available scopes.
Records link the raw per-case JSON and the published markdown in `agent-run-01/evaluation/`.

| Case | Kind | Scopes (✓ available / ✗ absent) | Topic | Citations | Retrievals / retries | Cited / available subqueries | Outcome | Flags | Records |
|---|---|---|---|---:|---:|---:|---|---|---|
| audit01 | regression | AAPL 2023 ✓, NVDA 2024 ✓ | Compare AAPL filing year 2023 and NVDA filing year 2024: find the disc… | 8 | 3 / 1 | 2/2 | supported | — | [json](agent-run-01/evaluation/audit01.json) · [md](agent-run-01/evaluation/audit01.md) |
| audit02 | regression | MSFT 2024 ✓, AMZN 2024 ✓ | Compare MSFT and AMZN, both filing year 2024: retrieve evidence on clo… | 8 | 4 / 2 | 2/2 | supported | — | [json](agent-run-01/evaluation/audit02.json) · [md](agent-run-01/evaluation/audit02.md) |
| audit03 | regression | NVDA 2024 ✓ | Audit NVDA filing year 2024: separately retrieve export control risks… | 8 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/audit03.json) · [md](agent-run-01/evaluation/audit03.md) |
| audit04 | regression | AMZN 2024 ✓ | Audit AMZN filing year 2024: separately retrieve AWS sales disclosures… | 7 | 3 / 1 | 2/2 | supported | — | [json](agent-run-01/evaluation/audit04.json) · [md](agent-run-01/evaluation/audit04.md) |
| audit05 | regression | AAPL 2023 ✓ | Audit AAPL filing year 2023: separately retrieve share repurchase auth… | 8 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/audit05.json) · [md](agent-run-01/evaluation/audit05.md) |
| audit06 | regression | MSFT 2024 ✓ | Audit MSFT filing year 2024: separately retrieve cloud services growth… | 7 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/audit06.json) · [md](agent-run-01/evaluation/audit06.md) |
| audit07 | regression | AMZN 2024 ✓ | Audit AMZN filing year 2024: separately retrieve liquidity and cash-fl… | 6 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/audit07.json) · [md](agent-run-01/evaluation/audit07.md) |
| audit08 | regression | NVDA 2024 ✓ | Audit NVDA filing year 2024: separately retrieve Data Center revenue g… | 6 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/audit08.json) · [md](agent-run-01/evaluation/audit08.md) |
| audit09 | regression | AAPL 2025 ✓ | Audit AAPL filing year 2025 only: separately retrieve revenue disclosu… | 8 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/audit09.json) · [md](agent-run-01/evaluation/audit09.md) |
| audit10 | regression | MSFT 2024 ✗ · exact “Item 1A” | Audit MSFT filing year 2024, with the exact SectionItem filter "Item 1… | 0 | 6 / 4 | 0/0 | expected unavailable | Q0 below threshold after 2 retries; Q1 below threshold after 2 retries; no verified answer | [json](agent-run-01/evaluation/audit10.json) · [md](agent-run-01/evaluation/audit10.md) |
| broad11 | positive | KO 2025 ✓, PG 2025 ✓ · exact “Item 7” | FX effect on revenue / sales | 8 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad11.json) · [md](agent-run-01/evaluation/broad11.md) |
| broad12 | positive | PFE 2025 ✓, MRK 2025 ✓ · exact “Item 1A” | patent protection, generic/biosimilar risk | 7 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad12.json) · [md](agent-run-01/evaluation/broad12.md) |
| broad13 | positive | BAC 2026 ✓ · exact “Item 7” | CET1 ratio; liquidity coverage ratio | 7 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad13.json) · [md](agent-run-01/evaluation/broad13.md) |
| broad14 | positive | TGT 2026 ✓ | shrink estimate; capital expenditures | 6 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad14.json) · [md](agent-run-01/evaluation/broad14.md) |
| broad15 | positive | TSLA 2026 ✓ | tariff/supply-chain risk; cost of revenues and gross margin | 7 | 2 / 0 | 2/2 | supported | dropped 1 unverified proposed claim (ref. …item7#passage-000020) | [json](agent-run-01/evaluation/broad15.json) · [md](agent-run-01/evaluation/broad15.md) |
| broad16 | positive | V 2024 ✓, V 2025 ✓ · exact “Item 1A” | interchange regulation risk, two filing years | 8 | 3 / 1 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad16.json) · [md](agent-run-01/evaluation/broad16.md) |
| broad17 | positive | COP 2025 ✓, OXY 2025 ✓ · exact “Item 7” | commodity-price sensitivity | 8 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad17.json) · [md](agent-run-01/evaluation/broad17.md) |
| broad18 | positive | MET 2025 ✓, AFL 2025 ✓ | MET interest-rate risk; AFL yen translation | 7 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad18.json) · [md](agent-run-01/evaluation/broad18.md) |
| broad19 | positive | CRM 2026 ✓, ADBE 2026 ✓ · exact “Item 8” | remaining performance obligations | 6 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad19.json) · [md](agent-run-01/evaluation/broad19.md) |
| broad20 | positive | AMD 2026 ✓, TXN 2026 ✓ | AMD foundry dependence; TXN capex | 6 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad20.json) · [md](agent-run-01/evaluation/broad20.md) |
| broad21 | positive | UNH 2025 ✓, CVS 2026 ✓ · exact “Item 7” | medical care / benefit ratio | 5 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad21.json) · [md](agent-run-01/evaluation/broad21.md) |
| broad22 | positive | SBUX 2024 ✓, SBUX 2025 ✓ · exact “Item 7” | comparable store sales; new stores, two years | 8 | 4 / 2 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad22.json) · [md](agent-run-01/evaluation/broad22.md) |
| broad23 | positive | ISRG 2026 ✓ · exact “Item 7” | tariff product cost; da Vinci procedure growth | 8 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad23.json) · [md](agent-run-01/evaluation/broad23.md) |
| broad24 | positive | LLY 2026 ✓, JNJ 2026 ✓ · exact “Item 7” | LLY product drivers; JNJ segment sales | 7 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad24.json) · [md](agent-run-01/evaluation/broad24.md) |
| broad25 | positive | HPE 2024 ✓, HPE 2025 ✓ · exact “Item 7” | share repurchase authorization, two years | 4 | 4 / 2 | 1/2 | partial gap | Q0 below threshold after 2 retries | [json](agent-run-01/evaluation/broad25.json) · [md](agent-run-01/evaluation/broad25.md) |
| broad26 | positive | HD 2026 ✓, WMT 2026 ✓ · exact “Item 8” | segment reporting | 8 | 2 / 0 | 2/2 | supported | — | [json](agent-run-01/evaluation/broad26.json) · [md](agent-run-01/evaluation/broad26.md) |
| broad27 | negative | TSLA 2024 ✗ | absent filing year (TSLA 2024) — must withhold | 0 | 6 / 4 | 0/0 | expected unavailable | Q0 below threshold after 2 retries; Q1 below threshold after 2 retries; no verified answer | [json](agent-run-01/evaluation/broad27.json) · [md](agent-run-01/evaluation/broad27.md) |
| broad28 | negative | NVDA 2024 ✗ · exact “Item 7” | absent exact section (NVDA 2024 Item 7) — must withhold | 0 | 6 / 4 | 0/0 | expected unavailable | Q0 below threshold after 2 retries; Q1 below threshold after 2 retries; no verified answer | [json](agent-run-01/evaluation/broad28.json) · [md](agent-run-01/evaluation/broad28.md) |
| broad29 | mixed | GS 2026 ✓, BAC 2025 ✗ | regulatory capital ratios; BAC 2025 not enrolled | 5 | 4 / 2 | 1/1 | supported | Q1 below threshold after 2 retries | [json](agent-run-01/evaluation/broad29.json) · [md](agent-run-01/evaluation/broad29.md) |
| broad30 | mixed | KO 2025 ✓, AMZN 2024 ✗ · exact “Item 7” | FX impact; AMZN 2024 has no exact Item 7 | 6 | 4 / 2 | 1/1 | supported | Q1 below threshold after 2 retries | [json](agent-run-01/evaluation/broad30.json) · [md](agent-run-01/evaluation/broad30.md) |

Audit prompts `audit01`–`audit10` are the historical regression prompts verbatim; their topics are in the linked records. `audit10` (MSFT 2024, exact Item 1A) is an
expected-unavailable case because that filing is only stored as unclassified text.

### Evidence gaps, negative and mixed behaviour

* **Negative / absent scopes (3):** `audit10` (exact section absent), `broad27` (TSLA 2024, filing year never enrolled), `broad28` (NVDA 2024 exact Item 7, filing is unclassified).
  Each used its full retrieval budget (6 searches, 4 retries), found no evidence, published **zero claims**, did not substitute another year, company or section,
  and flagged "no verified answer". This is the required outcome, not a failure.
* **Mixed (2):** `broad29` (GS 2026 ✓ / BAC 2025 ✗) and `broad30` (KO 2025 Item 7 ✓ / AMZN 2024 Item 7 ✗) cited the available side (1/1 each) and withheld the unavailable side,
  which still carries an explicit "below threshold after 2 retries" flag. That flag is deliberate evidence of the gap, not silent success.
* **HPE 2024 (`broad25`) — partial gap (historical baseline; a later critic follow-up is recorded in [`../hpe-critic-20261101/RESULTS.md`](../hpe-critic-20261101/RESULTS.md) and this section is not rewritten), not full coverage and not a transport failure.** The HPE 2024 subquery scored **3 on all three attempts** (initial plus two retries; critic
  threshold 4), was never accepted and has no published claim; HPE 2025 scored 4 on the first attempt and is cited. The retrieved context held the adjacent passages
  `000164559024000139#item7#passage-000035` and `…#passage-000036`, in which the repurchase sentence is split across the passage boundary; the critic rationales
  repeatedly asked for the original authorization size and share counts, which were not explicitly asked for in the prompt, so this looks like a critic-threshold outcome on a boundary-split sentence,
  not proof that the disclosure is absent. This gap is unchanged from v1 and is unrelated to Morgan Stanley or parser v3. It is flagged `Subquery 0 has no evidence meeting threshold after 2 retries.`
* **TSLA (`broad15`) — one proposed claim dropped.** The publication guard removed one unverified proposed claim (reference `000162828026003952#item7#passage-000020`) and the case still
  published 7 verified citations, supported on both subqueries. The rejected text was not stored, so its exact cause is **not** known; what is verified is that the guard dropped it
  and nothing unverified was published.
* No case was silently counted as full coverage, and none failed on transport.

### Comparison with the v1 run (structural only)

| | v1 (v2 corpus, `broad-validation-20261101/run-01`) | v2 suite on the v3 corpus (`agent-run-01`) |
|---|---:|---:|
| Verified cases | 30 / 30 | 30 / 30 |
| Outcomes | 26 supported, 1 partial, 3 expected-unavailable | 26 supported, 1 partial, 3 expected-unavailable |
| Available-scope subqueries cited | 53 / 54 = 98.1481 % | 51 / 52 = 98.0769 % |
| Published citations | 172 | 187 |
| Retrieval calls / retries / model calls | 86 / 24 / 143 | 85 / 25 / 142 |
| Median seconds | 23.64 | 24.366 |

The differences are **not accuracy improvements or regressions and have no parser causality**: the planner's subquery count varies between runs (54 vs 52 available subqueries), the
citation count follows how many quotes the model chooses to publish, and the median moves with model latency. Both runs are single samples of a non-deterministic model.
The 30 prompts, scopes and expectations are identical; the suite has no Morgan Stanley case, so neither this run nor the v1 comparison tests the changed MS passages.

## Scope and caveats (carried over)

* The agent suite has no MS case; its pass establishes the verifier-checked grounding behaviour on the v3 corpus, not MS quality or general parser accuracy.
* Only one filing differs; v3 parsing has not been observed on any other titled-mode filing, and the other 147 filings' raw bytes were not kept.
* FinQA labels cover FinQA passages only; the 29,895 SEC passages are unlabeled candidates. The quality numbers are a development-set regression
  check, not SEC retrieval quality, not held-out accuracy and not answer accuracy.
* Item 8 reference stubs and 43 skipped filings are unrelated and unresolved. The HPE 2024 critic gap was open in this baseline; see the [follow-up](../hpe-critic-20261101/RESULTS.md) for the later targeted critic change and its two-replay check.
* Local, loopback, warm, single-machine measurements; not production load latency or multi-tenant isolation. CLI defaults remain v2 and v3 is selected only by explicit paths; no deployment was performed, and nothing is claimed about any running service.

## Evidence

[`pre-run-inputs-and-gates.json`](pre-run-inputs-and-gates.json) · [`independent-migration-check.json`](independent-migration-check.json) ·
[`verification/independent-vector-check.json`](verification/independent-vector-check.json) ·
[`verification/independent-retrieval-and-load-check.json`](verification/independent-retrieval-and-load-check.json) ·
[v2-control report](v2-control/evaluation/report.json) / [load](v2-control/load/report.json) ·
[v3-candidate report](v3-candidate/evaluation/report.json) / [load](v3-candidate/load/report.json) ·
[gates.json](gates.json) · [gates log](verification/gates.log) · [agent verification](agent-run-01/independent-broad-verification.json) / [log](verification/agent-independent-verification.log) · [agent report](agent-run-01/evaluation/report.json) · [agent health](agent-run-01/health-after.json) ·
[sparse build](sparse-build.log) · [vector build](vector-build.log) · [python tests](verification/python-tests.log) ·
[final benchmark tests](verification/benchmark-tests-final.log) · [go race](verification/go-race.log) · [go vet](verification/go-vet.log).
Per-request CSVs stay local (git-ignored).
