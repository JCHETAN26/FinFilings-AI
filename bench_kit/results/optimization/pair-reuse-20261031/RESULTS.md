# Further hybrid CPU and queueing optimization — 2026-10-31

The recommended FP32 ONNX reranker now uses **one query/passage pair per model call**, with the same **four inference workers × two intra-op threads**. It scores all 50 fused candidates, retains the pinned float32 checkpoint and 512-token inputs, and returns the same Top-10 ordering for all 100 evaluation queries. Explicit batch-size overrides remain available; PyTorch retains its historical default of eight.

## CPU profile and selection

The first twelve saved real Top-50 sets were replayed under four-worker contention, without reading relevance judgments. Each worker owned its tokenizer and model/session; the executor and replica counts were both bounded to four. All configurations used two threads per job.

| Execution | Native QPS | Process CPU seconds/query | Average busy CPU cores |
|---|---:|---:|---:|
| onnx-fp32, batch 2 | 1.293917 | 5.509448 | 7.129 |
| torch, batch 2 | 1.173993 | 6.025861 | 7.074 |
| torch, batch 1 | 1.157325 | 6.021909 | 6.969 |
| onnx-fp32, batch 1 | 1.457853 | 4.805314 | 7.005 |

One-pair ONNX improves native replay throughput **12.67%** and reduces process CPU seconds/query **12.78%**. Actual input tokens remain **127,406** across the twelve calls; padded tokens decrease **129,972 → 127,406**. Per-call batch dimensions and attention/activation working data shrink, while call count increases. Cache pressure was not measured with hardware counters, so its contribution is not claimed as established. These short native profiles choose settings; the full HTTP results are below.

A separate traced real Top-50 call attributes **48.93%** of Node kernel duration to attention and **44.53%** to feed-forward nodes. ORT event durations are wall-clock microseconds, not summed helper-thread CPU time; tracing overhead excludes this sample from latency claims. [Operator report](operator-profile/report.json), [compressed raw trace](operator-profile/operator-trace.json.gz).

An input audit found 147 repeated tokenized pairs among 5,000 candidates, covering only 8,865 of 1,142,051 input tokens. That work reduction is below 1%; no cross-request cache or candidate removal was introduced. All Top-50 candidates still receive actual model inference. [Input audit](input-profile.json).

## Fresh controlled end-to-end comparison

The older batch-two run reported 52.733752 s hybrid p99. A fresh instrumented batch-two baseline on this host measured 42.495622 s **before changing batch width**. The improvement attributed to this change uses the fresh baseline, not the older number. Both fresh runs use the same corpus, vectors, sparse index, model/graph hashes, 100 queries, published judgments, RRF depth and candidate budgets.

50 clients, 200 measured requests per endpoint, five excluded warmups; hybrid and dense scenarios run sequentially. Percentiles are empirical nearest-rank values from raw nanosecond observations.

| Endpoint / configuration | p50 s | p95 s | p99 s | Successful QPS | Failures |
|---|---:|---:|---:|---:|---:|
| hybrid / batch 2 baseline | 37.522780 | 41.078433 | 42.495622 | 1.301409 | 0/200 |
| hybrid / batch 1 optimized | 35.335991 | 38.633175 | 39.916006 | 1.378062 | 0/200 |
| dense / batch 2 baseline | 0.661118 | 0.685984 | 0.694040 | 74.875258 | 0/200 |
| dense / batch 1 optimized | 0.656550 | 0.748193 | 0.882777 | 72.812789 | 0/200 |

Hybrid p99 improves **6.07%** and QPS **5.89%** against the fresh control. Serial p50 is essentially unchanged: **1.743773 → 1.746030 s**. Dense p99 increases **0.694040 → 0.882777 s**; these isolated dense runs last under three seconds and the increase remains part of the reported trade-off. All **800/800** measured requests across the fresh pair succeeded, with zero 503s or other failures. The optimized run alone passed **400/400**.

All 100 queries retain identical sparse, dense, RRF and reranked ID ordering. Upstream scores are also identical, all Top-10 text/metadata is unchanged, and every query retains its nDCG. Aggregate nDCG@10 remains BM25 **0.496070**, dense **0.482297**, RRF **0.507805**, reranked **0.553718**; PQ recall stays **0.838**. Tiny reranker logit differences from batch shapes are retained in the raw evidence. Published FinQA judgments incompletely cover the added SEC passages, so these measurements do not establish general SEC audit accuracy. No agent evaluation was rerun.

## Measured queueing

Per-request engine timings and raw CSV columns separate admission, sparse/dense retrieval, fusion and rerank round trips. Engine total excludes TCP acceptance, HTTP request decoding and response transmission. The residual `client − engine_total` includes time before application acceptance, connection/HTTP handling and client encode/decode; it is not interpreted as pure network latency. Sparse and dense run in parallel, so their per-request maximum enters the decomposition.

| Mean client component | Baseline s | Optimized s |
|---|---:|---:|
| Client total | 33.800252 | 31.884501 |
| Outside engine | 21.913646 | 20.675040 |
| Engine admission wait | 5.829518 | 5.501723 |
| Parallel retrieval wall time | 1.568749 | 1.434595 |
| Sparse only (overlaps dense) | 0.004545 | 0.004528 |
| Rerank round trip | 4.488220 | 4.273027 |
| Engine total | 11.886607 | 11.209461 |

In the baseline, outside-engine time contributes **64.83%** of mean client time, and engine admission **17.25%**. Reducing native service work lowers both waits while preserving the same request population and bounds. The decomposition conserves mean client time; stage percentiles cannot be added. Its outside-engine component alone does not identify a specific kernel queue or network cause.

Native load differences come from health snapshots before and after the load harness. They include five warmups per endpoint: 205 rank calls and 410 dense calls, with dense covering both scenarios.

| Mean native reranker component | Baseline s | Optimized s |
|---|---:|---:|
| Native rank work | 2.956438 | 2.791625 |
| Sidecar worker-slot wait | 1.457191 | 1.412173 |
| Passage lookup | 0.004317 | 0.004051 |
| Tokenization | 0.008768 | 0.007929 |
| Batch preparation | 0.006500 | 0.010031 |
| Model inference | 2.930946 | 2.763592 |
| Output metadata | 0.001270 | 0.001167 |

This measurement identifies model inference as the dominant native work; sparse retrieval and SQLite lookup are small. CPU contention remains a capacity limit at 50 clients. Queue timing is observational; the run does not establish a production SLO, sustained capacity, or confidence interval from repeated A/B trials.

## Bounds, verification and reproduction

Native inference slots stay **four**, intra-op threads **two per job**, nominal CPU budget **eight**. Sidecar admission remains **64**; the Go engine admits **eight** requests and the listener limits live connections to **sixteen**. Completion-owned slots remain held until native work finishes after HTTP cancellation. Profiling counters store totals rather than an unbounded per-request list. Final health shows zero active requests and no rejected admissions; both owned service processes stopped cleanly.

**32 Python tests**, all Go race tests and Go vet passed. New coverage checks the automatic ONNX batch-one setting, preserved PyTorch batch-eight setting, actual source-ID coverage, no batch padding and phase-time conservation on real candidates. CLI overrides remain available. Source defaults were updated after the explicit `--rerank-batch-size 1` benchmark; the tested inference configuration is unchanged.

[Independent quality/load/model checks](optimized-b1/independent-comparison.json) · [Baseline queue analysis](baseline-b2/queueing-analysis.json) · [Optimized queue analysis](optimized-b1/queueing-analysis.json) · [Baseline load](baseline-b2/load/report.json) · [Optimized load](optimized-b1/load/report.json) · [Optimized raw queries](optimized-b1/evaluation/per-query.jsonl) · [Baseline raw queries](baseline-b2/evaluation/per-query.jsonl) · [Baseline hybrid CSV](baseline-b2/load/hybrid-requests.csv) · [Optimized hybrid CSV](optimized-b1/load/hybrid-requests.csv) · [Dense CSV](optimized-b1/load/dense-requests.csv) · [Final health](optimized-b1/health-after.json).

[Final Python tests](python-tests-final.log) · [Go race tests](go-tests.log) · [Go vet](go-vet.log) · [Baseline terminal log](baseline-b2.log) · [Optimized terminal log](optimized-b1.log). The generated `listen-queue-snapshot.json` was captured after the hybrid scenario and is not used to infer hybrid listen backlog.

```bash
# With acquired local models, corpus, indexes and optional ONNX dependencies:
export PYTHONPATH="$PWD/py"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2 NUMBA_NUM_THREADS=4
python scripts/benchmark-expanded.py \
  --data data/sec-scale-v2-evaluation-20261031 \
  --vectors indexes/vector-sec-scale-v2-pq128-20261031 \
  --sparse indexes/sparse-sec-scale-v2-20261031 \
  --reranker-onnx models/reranker-onnx-fp32-optimized-20261031 \
  --inference-workers 4 --threads 2 --rerank-batch-size 1 \
  --out bench_kit/results/optimization/cpu-queue-rerun
python bench_kit/analyze_queueing.py bench_kit/results/optimization/cpu-queue-rerun
```

Use a fresh output directory. For a fresh control, repeat with `--rerank-batch-size 2` and another fresh directory. `profile_parallel_rerank.py` reproduces the bounded native comparison, `profile_onnx_ops.py` produces operator attribution, `compare_reranker.py` independently verifies ranking/quality/load evidence, and `analyze_queueing.py` validates timing decomposition. No commit or push was performed.
