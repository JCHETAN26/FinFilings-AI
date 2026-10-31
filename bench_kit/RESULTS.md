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
