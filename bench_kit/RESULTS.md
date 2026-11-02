# FinFilings AI — consolidated verified benchmarks

Host: AMD Ryzen 7 5800H with Radeon Graphics, 16 logical CPUs, Linux. BGE and MiniLM inference runs on CPU. Local, non-isolated desktop; warm models/filesystem cache. Results describe the stated finite workloads, not production SLOs.

## Phase 1 — compressed sparse index

702 real SEC passages from four 10-K filings; ten rounds totaling 10,000,000 in-process BM25 searches, 50 configured workers. Observed simultaneous Search calls were 30–47.

| Metric | Verified result |
|---|---:|
| Median indexing throughput | 20,352 passages/s |
| Validated mmap Open, round medians | 0.251–0.292 ms |
| Worst round search p99 | 0.048 ms |
| Post-load benchmark process RSS | 42.0–45.0 MiB |
| Search errors / ranking mismatches | 0 / 0 |

These are warm-cache, sparse-only, in-process measurements on a small corpus. The RSS includes benchmark buffers and mapped pages. Total timed load lasted 12.34 seconds; this is not a long soak. Mmap timing includes CRC/structural validation. [Original full Phase 1 report](PHASE1_RESULTS.md) · [raw JSON](results/phase1-3051556664/report.json).

## Phase 2 — hybrid retrieval and PQ8

19,511 real financial passages; 100 fixed FinQA development supporting-fact queries. RRF k=60, Top-50 fusion and MiniLM Top-10 reranking. Published supporting facts are binary relevance labels, not exhaustive judgments.

| Metric | Verified result |
|---|---:|
| BM25 nDCG@10 | 0.4919 |
| PQ8 nDCG@10 | 0.4458 |
| Hybrid RRF nDCG@10 | 0.5196 |
| Hybrid + cross-encoder nDCG@10 | 0.5757 |
| PQ8 exact native vector payload reduction | 95.56% |
| Isolated vector-load RSS growth decrease | 92.39% |
| PQ8 recall@10 versus exact float32 | 0.685 |

Native payload: 29,968,896 → 1,329,744 bytes (codes plus centroids). Isolated median RSS growth: 30,306,304 → 2,306,048 bytes, three probes per representation.

The 95.56% saving is **vector-component payload**, not whole-application RAM. The full model sidecar used approximately 1.50 GiB RSS after the Phase 2 evaluation. Serial CPU full-pipeline p99 was 2.674 seconds. [Phase 2 details](../docs/build/phase2.md) · [raw report](results/phase2/evaluation/report.json).

## Phase 3 — bounded agent and citation grounding

- 10/10 audit prompts passed execution/filter/retry/source checks.
- **54/54 published citations verified** against exact SEC text, chunk ID, accession and source link.
- Median full-audit time: **23.34 seconds**.
- 6/8 positive audits had every subquery linked to published evidence; two AMZN audits were partial. Both unavailable-evidence tests correctly abstained.
- 32 retrieval calls; 12 retries, maximum two per subquery.

Citation correctness is not a human-labeled answer-accuracy score. Synthesis publishes verified excerpts rather than unverified calculations. AMZN cloud-cost and operating-expense retrieval gaps remain documented. [All ten audit results](results/phase3/RESULTS.md).

## Phase 4 — 50-worker HTTP load

Measured run: `run-20261031T063553Z`. Each endpoint received 200 measured requests from 50 persistent closed-loop workers, using the same 100 real Phase 2 queries twice. Five serial warmup requests per endpoint were excluded. Scenarios ran sequentially; each request used a new TCP connection to avoid keepalive monopolization of bounded server connections. Connection establishment, admission/queue delay, full response and content validation are timed. Client deadline: 180 seconds.

Go retains eight engine slots and 16 live connections. Python retains one native inference worker and an explicit 64-request admission budget; the HTTP concurrency guard is 128. Requests beyond the application budget receive HTTP 429 with Retry-After instead of entering an unbounded queue. Fifty client requests can be in flight while only one native inference job executes. Cancellation does not release the native-work slot until the actual thread finishes.

| Endpoint / measured population | N | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---:|---:|---:|---:|
| Go /search: BM25 + PQ8 + RRF + rerank, all attempts | 200 | 28025.468989 | 30092.246525 | 30675.532280 |
| Go /search: BM25 + PQ8 + RRF + rerank, successful responses | 200 | 28025.468989 | 30092.246525 | 30675.532280 |
| Python /dense: BGE + PQ8, all attempts | 200 | 831.335249 | 841.401287 | 843.958092 |
| Python /dense: BGE + PQ8, successful responses | 200 | 831.335249 | 841.401287 | 843.958092 |

| Endpoint | Success / attempts | Failure rate | Successful QPS | Completed attempts/s | Peak client in-flight |
|---|---:|---:|---:|---:|---:|
| hybrid | 200/200 | 0.00% | 1.756374 | 1.756374 | 50 |
| dense | 200/200 | 0.00% | 59.521518 | 59.521518 | 50 |

Percentiles use exact empirical nearest rank `ceil(p × N)` over integer nanosecond observations; six decimal places in milliseconds preserve those integers. Both all-attempt and successful-only distributions are shown so quick overload errors cannot masquerade as faster useful retrieval. QPS uses the entire measured wall interval, including client ramp/drain and observation handling. Finite closed-loop results do not characterize open-loop arrivals, long-term reliability or a capacity limit.

- hybrid: elapsed 113.870936 s; HTTP/status counts `{"200": 200}`.
- dense: elapsed 3.360129 s; HTTP/status counts `{"200": 200}`.

Full hybrid failures: 0/200; direct dense failures: 0/200. These are finite closed-loop results, not a sustained capacity or production-availability certification. Dense before/after latency populations differ: the baseline quickly rejected 189 requests, while the optimized service queues admitted work. Successful-only distributions and status counts must accompany comparisons.

[Raw load report](results/phase4/run-20261031T063553Z/load/report.json) · [Terminal log](results/phase4/run-20261031T063553Z/terminal.log) · [Independent CSV/quantile checks](results/phase4/run-20261031T063553Z/independent-checks.json). All 400 per-request observations, including failures, are retained locally as CSV. One-second process RSS samples are in the same run directory; these are observed samples, not exact instantaneous peak memory.

## Optimization — verified before/after

Same CPU models, FAISS index, 100-query catalog, 200 requests per endpoint, 50 clients, new-connection policy and 180-second deadline. Baseline records remain unchanged. Reranking now tokenizes at most 50 pairs once, groups them by token length, and pads each eight-pair microbatch only to its own maximum. No candidate pruning, result cache, lower precision or model replacement is used.

| Metric | Original | Optimized |
|---|---:|---:|
| Hybrid p50 | 50.752549 s | 28.025469 s |
| Hybrid p95 | 56.491527 s | 30.092247 s |
| Hybrid p99 | 57.278598 s | 30.675532 s |
| Hybrid successful QPS | 0.976870 | 1.756374 |
| Hybrid failures | 0/200 | 0/200 |
| Dense successes | 11/200 | 200/200 |
| Dense failures | 189/200 | 0/200 |

Hybrid throughput ratio: **1.798×**; p99 decrease: **46.45%**. Non-isolated local observations, not a statistical significance claim.

Quality replay: 100 original RRF Top-50 candidate sets, actual pinned cross-encoder. nDCG@10 remained 0.575744; 2 Top-10 orders changed from floating-point padding effects (maximum shared score difference 0.00000572). This replays reranking, not a fresh end-to-end relevance benchmark. [Quality log](results/optimization/rerank-quality.json).

Measured admission peak: 50/64; rejected by application budget: 0. Native inference workers: 1. Per-stage work and queue totals: [post-load health](results/phase4/run-20261031T063553Z/health-after.json).

Validation: 17 Python tests pass, including admission overflow/cancellation and the original provenance/PQ contracts. Live model integration and the raw CSV/percentile recheck are saved with the optimization results. Original Go tests/race/vet remain applicable; Go search and load code were unchanged.

[Original load report](results/phase4/run-20261031T062424Z/load/report.json) · [CPU micro-profile](results/optimization/cpu-profile.json). The three-query profile favored the original four CPU threads and eight-pair batch size; those settings were retained.

## Portfolio / resume supplement

- Built a disk-backed Go SEC search engine with PFOR compression and mmap; measured 20,352 passages/s median indexing and 0.048 ms sparse p99 on 702 warm-cache passages.
- Achieved 0.5757 nDCG@10 on 100 fixed FinQA support-fact queries using BM25/PQ8 RRF and cross-encoder reranking; reduced native vector payload by 95.56%.
- Implemented a bounded multi-hop audit controller with 54/54 verified published citations across ten audits; median 23.34 s, with partial answers and abstentions explicitly flagged.
- Load-tested the full hybrid HTTP pipeline with 50 clients: 1.76 successful QPS, 30.676 s all-attempt p99, and 0.00% failures across 200 measured attempts.

## Verification and reproduction

Go tests, race checks and vet passed; the new quantile test checks known nearest-rank values. Prior phases retain their own verification logs. Model revisions, original corpus/query fingerprints, HTTP statuses and raw timings are preserved. No race-instrumented timing is presented. Generated fixture benchmarks are excluded from the headline metrics.

```bash
export PYTHONPATH="$PWD/models/runtime:$PWD/py"
export GOCACHE=/tmp/finfilings-go-cache
export CCACHE_DIR=/tmp/finfilings-ccache CCACHE_TEMPDIR=/tmp/finfilings-ccache-tmp
go build -o models/bin/finfilings ./cmd/finfilings
go build -o models/bin/loadtest ./bench_kit/load
python3 scripts/verify-phase4.py
python3 bench_kit/consolidate.py
```

The isolated project runtime and pinned model cache must already be installed (see the Phase 2/3 setup reports). The supervisor owns both real services and stops them after measurement. Builders and the load tool refuse existing output directories. No tenant-isolation or production-availability claim is made.

## Expanded real SEC corpus (2026-10-31)

Live ingestion: 138 accepted filings, 71/100 companies, 26,412 new passages; 53 explicitly rejected filings. Expanded retrieval collection: 45,923 passages; original 100 FinQA queries/qrels unchanged.

nDCG@10: BM25 0.494313, PQ8 0.379442, RRF 0.463680, cross-encoder 0.539313. PQ8/exact recall@10 0.535000; native vector payload reduction 96.3175%.

[Detailed verified results and limitations](results/ingest/scale-20261031/RESULTS.md). The new SEC passages are unlabeled; this measures frozen-judgment retrieval under corpus expansion, not SEC-only relevance. The earlier phase results above remain historical evidence.

| Expanded corpus, 50 workers | p50 s | p95 s | p99 s | Successful QPS | Failures |
|---|---:|---:|---:|---:|---:|
| hybrid, 1 inference worker | 92.301864 | 98.975711 | 100.366191 | 0.535769 | 0/200 |
| dense, 1 inference worker | 0.890718 | 0.907672 | 0.911780 | 55.477333 | 0/200 |
| hybrid, 4 inference workers | 58.048755 | 63.561236 | 65.151895 | 0.847348 | 0/200 |
| dense, 4 inference workers | 0.608027 | 0.920768 | 0.946094 | 70.254031 | 0/200 |

The hybrid slowdown is caused by rerank cost, not admission. New SEC passages average 254 words, against 38 for the original passages, and make up half of the cross-encoder's Top-50 input. That raises native rerank time per call from 0.549 s to 1.851 s.

Running four model replicas (`--inference-workers 4`) gave 1.58× hybrid throughput and a 35% lower p99, with nDCG@10 identical to the single-worker run. The gain stays below 4× because 16 intra-op threads compete for 8 physical cores. [Diagnosis and rerun](results/ingest/scale-20261031/RESULTS.md#hybrid-slowdown-diagnosis-and-4-worker-rerun).

Quality and full-pipeline latency regressed under corpus expansion. Raw rankings and all 400 load observations were independently checked; the dense endpoint had no 503 failures. The supervised services were stopped after evaluation.

## Expanded corpus with parser v2 (2026-10-31)

Re-ingesting the same cohort with parser v2 gave 148 accepted filings (77/100 companies) and 29,941 SEC passages. Expanded retrieval collection: 49,452 passages. The 100 FinQA queries and judgments are unchanged.

nDCG@10:
- BM25: 0.496070
- PQ8: 0.351491
- RRF: 0.466944
- Cross-encoder: 0.537220

PQ8/exact recall@10 is 0.531, and native vector payload is reduced by 96.3573%. Reranked quality is within 0.002 of the 45,923-passage run. The additional passages are unlabeled, so this is not an SEC-only relevance measure.

| Parser-v2 corpus, 50 workers, 1 inference worker | p50 s | p95 s | p99 s | Successful QPS | Failures |
|---|---:|---:|---:|---:|---:|
| hybrid | 94.658585 | 102.151134 | 102.891273 | 0.522096 | 0/200 |
| dense | 0.895732 | 0.921024 | 0.925285 | 55.330507 | 0/200 |

Hybrid throughput is bounded by CPU reranking of long SEC passages, which make up 53.3% of the Top-50 candidates; rerank time is 1.901 s per call. [Detailed results and three-corpus comparison](results/ingest/scale-v2-20261031/RESULTS.md).

## Parser-v2 runtime: four workers × two threads (2026-10-31)

On the fixed 49,452-passage corpus, hybrid throughput increased from 0.522096 to 0.949196 QPS; p99 decreased from 102.891273 to 58.173420 seconds at 50 clients. All 400 measured requests succeeded. All 100 queries retained identical ID rankings across all four stages; reranked nDCG@10 remains 0.537220 and PQ8 recall@10 remains 0.531.

Dense p99 increased to 1.205311 seconds and serial-query latency also increased. This is an optional configuration for hybrid-heavy concurrency, with four model replicas and a nominal eight-thread intra-op budget. It does not address the remaining dense-quality or parser-coverage gaps.

[Detailed controlled comparison and trade-offs](results/ingest/scale-v2-20261031/benchmark-workers4-threads2/RESULTS.md).

## Dense retrieval quality: wider PQ8 codes (2026-10-31)

On the same 49,452 passages and 100 financial queries, increasing PQ8 from 48 to 128 sub-vectors improved recall against exact float32 from **0.531 to 0.838**. Dense nDCG@10 increased **0.351491 → 0.482297**; RRF **0.466944 → 0.507805**; reranked **0.537220 → 0.553718**. BM25 remains **0.496070**. Exact float32 dense nDCG@10 is **0.493503**. Code width was selected using separate non-training document-vector queries without reading financial relevance labels.

Native vector payload rises **2,766,912 → 6,723,072 bytes**, retaining **91.1490%** savings against **75,958,272** float32 bytes. This is vector payload, not whole-service memory. Embeddings, store, training rows, seed and model checkpoints are unchanged. New builds default to 128 sub-vectors; 48 remains an explicit compact option.

With the same four inference workers × two threads and 50 clients, all **400/400** measured requests succeeded. Hybrid p50/p95/p99: **48.538136 / 52.334358 / 54.353373 s**, **1.011818 QPS**. Dense: **0.692504 / 0.748435 / 0.772436 s**, **69.176475 QPS**. These are finite local measurements; newly added SEC passages remain incompletely judged by the frozen FinQA labels.

[Detailed report, raw observations and independent verification](results/ingest/scale-v2-20261031/benchmark-pq128-workers4-threads2/RESULTS.md) · [Label-free PQ configuration comparison](results/ingest/scale-v2-20261031/pq-quality-20261031/RESULTS.md).

## Cross-encoder latency: FP32 graph fusion and smaller batches (2026-10-31)

An explicit FP32 ONNX reranker option retains the pinned MiniLM checkpoint, all 50 candidates and 512-token inputs, with batches of two and fused BERT operations. At four workers × two threads, serial full-query p50 improves **2.432633 → 1.823550 s** (25.04%). All 100 queries retain their nDCG; reranked nDCG@10 remains **0.553718**, PQ recall **0.838**, and upstream scores/rankings are identical. Two order changes only swap identical-text source IDs; Top-10 sets are unchanged.

| 50-client endpoint | p50 s | p95 s | p99 s | Successful QPS | Failures |
|---|---:|---:|---:|---:|---:|
| Hybrid, FP32 ONNX 4×2 | 46.819551 | 51.084855 | 52.733752 | 1.053630 | 0/200 |
| Dense, same service | 0.777015 | 0.885906 | 0.896412 | 63.459618 | 0/200 |

The measured hybrid load gain is modest: p99 **54.353373 → 52.733752 s**, QPS **1.011818 → 1.053630**. Dense p99 increased. A separate bounded **8 workers × 1 thread** run preserved quality and passed all 400 requests but regressed hybrid p99 to **60.089434 s**; it is not recommended. Both new runs used the same nominal eight-thread budget, and all **800/800** requests succeeded without 503s. These are finite local observations, not sustained capacity guarantees. CPU reranking remains the dominant constraint at 50 clients.

All 31 Python tests, Go race tests and Go vet pass. [Full experiment report, native work/queue times, independent checks and reproduction](results/optimization/cross-encoder-20261031/RESULTS.md).

## Further CPU and queue profiling: single-pair FP32 reranking (2026-10-31)

A fresh batch-two baseline measured **42.495622 s** hybrid p99 and **1.301409 QPS**, already faster than the older 52.733752 s run. The measured gain attributed to batch width uses this fresh control. Single-pair ONNX inference, at the same **4 workers × 2 threads**, reduces p99 to **39.916006 s** (**6.07%**) and raises throughput to **1.378062 QPS** (**5.89%**). p50/p95: **35.335991 / 38.633175 s**. All **400/400** optimized requests succeeded; both fresh runs total **800/800**, zero 503s.

Every one of the 100 queries retains identical Top-10 ordering and per-query nDCG; reranked nDCG@10 remains **0.553718**, PQ recall **0.838**. Serial p50 is essentially unchanged, **1.743773 → 1.746030 s**. Dense p99 increased **0.694040 → 0.882777 s**, with **72.812789 QPS** in the optimized service. These finite local runs do not establish sustained capacity or a confidence interval.

The concurrent native profile reduced CPU seconds/query **5.509448 → 4.805314**. A traced call attributes about **49%** of kernel duration to attention and **45%** to feed-forward nodes; model inference is over **99%** of measured native rank work. In the control, mean outside-engine time is **21.913646 s** and admission wait **5.829518 s**, against **33.800252 s** mean client time. Those waits shrink with lower CPU work; existing concurrency and connection bounds remain intact. Outside-engine time includes acceptance, HTTP handling and client work; it is not pure network latency.

Single-pair batches are now the ONNX default; explicit overrides remain supported and PyTorch keeps its historical default. All **32 Python tests**, Go race tests and vet pass. [Full profile, raw observations, queue decomposition and independent verification](results/optimization/pair-reuse-20261031/RESULTS.md).

## Dense p99 follow-up: pressure-triggered BGE batching (2026-10-31)

The optimized profile batches up to eight waiting BGE queries above **eight live dense requests**, while lower pressure keeps the immediate single-query path. It uses the same **four native workers × two threads**, one-pair FP32 reranker, model checkpoints, PQ128 vectors and retrieval budgets. Enable with `--dense-batch-size 8 --dense-batch-trigger 8`; existing launch defaults remain single-query.

In matched **2,000-request, 50-client dense-only probes**, p50/p95/p99 improve **0.670014 / 0.804961 / 1.255924 s → 0.382852 / 0.503825 / 0.552580 s**. Successful throughput rises **73.295173 → 129.848810 QPS**; p99 decreases **56.00%**. All **4,000/4,000** probe requests succeeded. Separate 100-query, 50-client HTTP quality replays retain identical Top-100 ordering; the largest score difference is **0.000000238419**.

The standard 200-request-per-endpoint run gives dense p99 **1.234895 → 0.521500 s**, QPS **74.647152 → 129.953149**. Hybrid throughput is essentially unchanged (**1.382198 → 1.384325 QPS**). Hybrid p99 is **39.590239 → 40.077777 s**, a measured **1.23% increase** in this pair; this is still approximately the prior 40-second result, not a claim of improved hybrid tails or statistical equivalence. All **800/800** standard comparison requests succeeded. Every evaluated query retains its Top-10 order and nDCG; reranked nDCG@10 remains **0.553718**, dense **0.482297**, PQ recall **0.838**.

The trigger preserves the immediate path within the Go engine’s eight-request hybrid cap. Final counters show **418 immediate calls + 192 grouped queries in 24 batches**, matching the original **610 logical dense calls**. All queues drain with no rejected admissions. **37 Python tests**, all Go race tests and vet pass. [Detailed results, native profile, bounds, raw observations and independent verification](results/optimization/dense-batching-20261031/RESULTS.md).

## Expanded-corpus agent revalidation — October 31, 2026

The same ten audit prompts now run against the frozen **49,452-passage** corpus with **152 trusted SEC sources** (four original + 148 newly acquired filings). The PQ128/FP32 ONNX service keeps four workers × two threads; agent retrieval and model concurrency remain bounded. **10/10 execution/filter/retry/source checks passed, 61/61 published citations independently verified**, with **28 retrieval calls, seven retries**, and median audit time **22.697032 s**. Eight audits have every subquery linked to published evidence, one cloud comparison is partial due to AMZN retrieval failure, and one exact MSFT 2024 Item 1A audit correctly abstains. AAPL 2025 is now available and produces eight verified citations from new section-tagged passages.

These are structural/provenance checks and extractive quotes, not a human answer-accuracy score or broad coverage across all newly ingested companies. Original corpus results remain separate. All 39 Python tests pass. [All ten prompts, citation checks, retries and gaps](results/phase3-expanded-20261031/RESULTS.md) · [Independent trace/source checks](results/phase3-expanded-20261031/independent-verification.json).

## AMZN cloud audit follow-up — bounded evidence carryover

The affected MSFT/AMZN cloud competition and infrastructure-cost audit now covers both company subqueries with **8/8 independently verified citations (four per company)**, **three retrieval calls, one retry**, elapsed **37.849243 s**, and no unresolved evidence-gap flags. Previously AMZN failed after two retries and all eight citations were MSFT-only. A bounded critic-selected carryover retains up to two useful passages across a missing-facet rewrite, keeping the complete model context at five passages and preserving all filing filters. Rewrites avoid filing identifiers and unsupported added requirements. Four inference workers × two threads, agent concurrency and retry caps are unchanged. All 40 Python tests pass.

This is one affected-audit rerun, not a rerun of all ten or a statistically controlled speed comparison. [Diagnosis, actual query/retention trace, citations, limits and reproduction](results/agent-cloud-gap-20261031/RESULTS.md).

## Broad agent validation — frozen 30-case suite (November 1, 2026)

**30/30 cases verified** (zero transport/model/structural failures): **26 supported, 1 partial gap, 3 expected-unavailable**; **172** independently verified published citations, 86 retrieval calls, 24 retries, median **23.64 s**, 143 Claude calls (`claude-haiku-5-5`). "Supported" means every planned subquery on an *available* scope has a verified published quote; it does **not** mean 26 complete answers. Both mixed cases have their available side fully cited (2/2, 1/1) with the unavailable scope withheld; available-scope coverage is **53/54 = 98.1481 %**. The one gap is HPE filing year 2024 (stayed at critic score 3 through two retries; the amount and remaining authorization sit in adjacent passages 35/36 split mid-sentence, so this is not evidence the disclosure is absent). One TSLA model-proposed quote was rejected by exact-quote grounding. The suite is ten historical prompts plus twenty source-informed regression fixtures over 30 companies with citations; it is not independent human gold, not a semantic accuracy score, and not load latency. Strict artifact re-verification (index, model, suite, corpus and 53 code files) passed; the 40 application + 50 bench-contract Python tests and Go race/vet logs passed in Codex's independent run. Earlier ten-audit and AMZN results above are unchanged and keep their own scope. [All 30 cases with complete answers/traces, gaps, setup and gates](results/broad-validation-20261101/RESULTS.md).

## Parser correctness follow-up — Morgan Stanley Item 7/7A boundary (November 1, 2026)

Morgan Stanley titled-heading boundary fix (parser `deterministic-canonical-heading-v3-go`, default for new ingestions), independently verified by Codex on a fresh replay. Real-source diagnosis: the old "3,000 lost MD&A words" note was wrong — Item 7 (v2: 32,278 words / 127 chunks) over-ran into Item 7A; v3 closes it at the Item 7A heading (20,497 words / 81 chunks, **11,781 Item 7A words removed**). There is no MD&A coverage gain. Item 1A and Item 8 are identical; of 361 v2 passages, 315 remain in v3, 46 IDs exist only in v2, and one ID (`…item7#passage-000080`) changed. Equivalence claim is limited to the 361 stored indexed passages matching the frozen corpus in full text and metadata; the current SEC document (9,714,819 bytes, SHA-256 `3002143b…78b2`) is **not** the archived 9,714,832-byte copy, whose raw HTML is gone, so no full-visible-text claim is made. Codex-verified: 40 application + 50 bench tests, `go vet`, `go test -race ./...` with the real 9.7 MB file, original four filings identical, shard-router reopen with stable descriptor/goroutine counters. Offline recheck of the saved broad run still passes 30/30 and 172 citations with all source/index/model hashes matching (code drift only in `pkg/ingest/parser.go`); the broad agent was **not** rerun. The active 49,452-passage corpus/indexes still hold the v2 Morgan Stanley Item 7; adopting v3 needs a separately versioned rebuild and re-benchmark (not done). The HPE 2024 critic gap is unchanged. [Evidence, developer vs verified replays, tests and limitations](results/parser-boundaries-20261101/RESULTS.md) · [revised diagnosis](../docs/build/edgar-ingest.md).

## Parser-v3 mixed-lineage corpus (November 1, 2026) — gates pass; published as `6f18c04`

A new, separately versioned corpus (`data/sec-scale-v3-evaluation-20261101`, 49,406 passages vs 49,452): only Morgan Stanley was reparsed with parser v3 (361 → 315 passages: 314 identical, 46 removed Item 7A text, 1 changed ID); **147 filings were not reparsed**, no new raw files were fetched for them, and everything else is byte-identical. The current MS source (SHA-256 `3002143b…78b2`, 9,714,819 bytes) is not the archived file (`57698077…12df`, 9,714,832 bytes, gone); the cause of the 13-byte difference is unknown and the report records both identities. New sparse and PQ128 indexes were built (49,405 vectors reused bit-identically, 1 encoded, PQ codebook retrained). v2 artifacts, `broad-validation-v1` and all earlier evidence are unchanged.

Matched run (same 100 FinQA **development** queries, 4 inference workers × 2 threads, 50 clients, 200 requests/endpoint, zero failures in 400/400 requests per run, every nDCG and latency recomputed independently by Codex): reranked nDCG@10 **0.5537183500706869 → 0.5539997505715865**, hybrid 0.50780475359511 → 0.5100210113496677, PQ recall@10 0.838 → 0.841, native vector payload savings 91.1485 % (payload only, not whole-process RSS), hybrid p99 **47.812667999 s → 44.999937571 s**, dense p99 0.575851645 → 0.556806811 s. **Disclosed regression: dense-only nDCG@10 fell 0.48229729941572386 → 0.46732239998883435 (−0.01497).** The small upward movements are not attributed to the MS fix (the PQ codebook was retrained and the candidate pool changed). These are development-set regression checks — not held-out, not SEC retrieval accuracy; load figures are single finite closed-loop runs on a non-isolated Ryzen 7 5800H (8 physical / 16 logical CPUs, CPU inference), not an SLA.

**Agent audit (`broad-validation-v2`, same 30 cases): 30/30 verified by the strict independent verifier, 0 transport/model/structural failures, 187 verified citations, 51/52 = 98.08 % available-subquery coverage, 26 supported + 1 partial gap (HPE 2024, unchanged from v1) + 3 expected-unavailable, both mixed cases fully cited on the available side with the absent side withheld.** One TSLA proposed claim was dropped by the publication guard (cause not stored). The suite contains no Morgan Stanley case, so this does not establish MS or general parser quality, and the v1→v2 count differences (53/54→51/52, 172→187 citations) are not accuracy changes. All pre-registered gates pass. The corpus version is validated and the tooling is ready; **published as `6f18c04`; recorded in Git history**. CLI defaults remain v2 and v3 is selected only by explicit paths; no deployment was performed. [All 30 case rows and evidence](bench_kit/results/parser-v3-adoption-20261101/RESULTS.md) · [runbook](docs/build/parser-v3-adoption.md)

## HPE 2024 critic follow-up (November 1, 2026) — targeted check, not a 30-case rerun

A generic change to the critic prompt and to the passage view it receives (document order plus adjacency markers within one verified filing section) was checked with two fresh real HPE `broad25` replays and two controls (`broad28`, `broad30`) on the unchanged parser-v3 indexes. Both HPE replays cited both planned scopes with no evidence-gap flags (4 verified citations each, 2 retrievals, 0 retries, vs. the baseline's 1/2 scopes, 4 retrievals, 2 retries). Not a fresh 30-case result and not a semantic-accuracy measure. Details, raw links and limits: [report](results/hpe-critic-20261101/RESULTS.md) · [runbook](../docs/build/hpe-critic.md). The earlier HPE-gap text above is the historical baseline and is unchanged. Publication recorded in Git history; no deployment performed.
