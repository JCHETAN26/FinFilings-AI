# Dense tail-latency fix with bounded query batching — 2026-10-31

The optimized serving profile enables **pressure-triggered BGE query batching**, while retaining **four native workers × two threads** and the existing single-pair FP32 cross-encoder. Up to eight queries share one real BGE forward pass; each keeps its own PQ search depth, filters, source IDs and response. Queries at or below eight live dense requests follow the immediate single-query path. Above that threshold, up to four asynchronous coordinators gather groups with a maximum two-millisecond collection window and submit them through the same completion-owned model slots.

Enable this profile explicitly with `--dense-batch-size 8 --dense-batch-trigger 8`. Batching remains disabled by default (`--dense-batch-size 1`) for existing launch commands. No new model, vector quantization, query/result cache or candidate reduction is introduced.

## Longer dense-only verification

The previous 200-request dense tests lasted only a few seconds and had variable tails. Two fresh sidecar instances therefore each ran **2,000 actual requests at 50 clients**, using the same 100 queries twenty times, plus five excluded warmups. No hybrid scenario runs during these longer probes; checkpoint, index, model footprint, worker/thread bounds and connection policy match. The runs are finite probes, not soak tests or production SLO guarantees.

| Dense-only probe | p50 s | p95 s | p99 s | Successful QPS | Failures |
|---|---:|---:|---:|---:|---:|
| Single query | 0.670014 | 0.804961 | 1.255924 | 73.295173 | 0/2000 |
| Pressure batching | 0.382852 | 0.503825 | 0.552580 | 129.848810 | 0/2000 |

Dense p99 decreases **56.00%**, and throughput increases **77.16%**. All **4,000/4,000** measured probe requests succeed, with zero 503s or other failures. Native calls, including five warmups, decrease **2005 → 262** for the same **2,005 logical requests**. [Independent CSV/health/ranking verification](dense-probe-comparison.json).

After each timed probe, a separate **100-query, 50-client concurrent HTTP replay** verifies actual coordinator batching. Both replays retain every Top-100 ID and ordering from the frozen serial reference. Maximum shared score difference is **0** for the control and **0.000000238419** for batching. These quality requests are excluded from probe timing and separately accounted for in final health. Raw reference-file SHA-256, actual hits and query/corpus fingerprints are saved. [Control concurrent rankings](probe-single/concurrent-quality.json), [batched concurrent rankings](probe-adaptive/concurrent-quality.json).

## Standard hybrid + dense benchmark

The standard comparison still runs 100 end-to-end quality queries, then 200 measured requests per endpoint at 50 clients with five excluded warmups. Hybrid and dense scenarios run sequentially. The fresh control uses the existing one-pair reranker with query batching disabled.

| Endpoint / configuration | p50 s | p95 s | p99 s | Successful QPS | Failures |
|---|---:|---:|---:|---:|---:|
| hybrid / single-query control | 35.240801 | 38.628898 | 39.590239 | 1.382198 | 0/200 |
| hybrid / pressure batching | 35.256223 | 38.446506 | 40.077777 | 1.384325 | 0/200 |
| dense / single-query control | 0.656305 | 0.690839 | 1.234895 | 74.647152 | 0/200 |
| dense / pressure batching | 0.345712 | 0.512783 | 0.521500 | 129.953149 | 0/200 |

All **800/800** measured requests across this standard pair succeed. Dense p99 decreases **57.77%**. Hybrid throughput remains about **1.38 QPS**; p50 and p95 are similar, while observed p99 rises **1.23%** (**39.590239 → 40.077777 s**) in this pair. The earlier verified one-pair run was **39.916006 s**, so the approximately 40-second hybrid result is retained; this is not a claim that the tail improved or that statistical equivalence was established. These finite local runs have no repeated-trial confidence interval.

Serial end-to-end p50 is **1.687207 → 1.700206 s**. All 100 queries retain identical sparse, dense, RRF and reranked ID order. Upstream scores and returned text/metadata are unchanged. Every query retains its nDCG: BM25 **0.496070**, dense **0.482297**, RRF **0.507805**, reranked **0.553718**; serial PQ recall remains **0.838**. The published FinQA judgments incompletely cover the added SEC passages; these are collection-specific retrieval checks, not general SEC audit correctness. Phase 3 was not rerun.

## How hybrid traffic is protected

The Go engine admits eight search requests. Each issues at most one dense call at a time, so its normal eight-request pipeline stays below the batching trigger and uses the original immediate path. The collection window is only added above eight live dense requests, as in the standalone 50-client burst. The same four completion-owned slots serve both BGE and reranker jobs; batching does not add native workers or reserve reranker capacity.

Final standard-run counters record **418 immediate calls** and **192 grouped queries in 24 batches of eight**, totaling the same **610 logical dense calls** as the control. This accounting includes 200 quality calls, 205 hybrid calls including warmups, and 205 standalone dense calls including warmups. The expected 405 quality/hybrid calls, five dense warmups and first eight burst calls use the immediate path. No queues or active requests remain at the final snapshot.

An initial always-collect batching trial also passed 400/400 requests and preserved quality. It measured dense p99 **0.488367 s**, hybrid p99 **39.985526 s**, and hybrid QPS **1.369051**. The pressure-triggered policy is selected for its immediate-path invariant under the existing hybrid admission cap, rather than for a claim that one small tail difference establishes causality. [Initial trial raw load](optimized-b8/load/report.json). To reproduce the always-collect policy with current code, set `--dense-batch-trigger 0`.

## Native CPU profile and quality

The fixed real 100-query native replay uses four private encoder replicas and two threads per job. It reads no relevance judgments for performance selection. Each response uses the same FAISS PQ128 index, original per-request search depth and filing predicate, with SQLite-backed ID lookup.

| Query batch | Native QPS | CPU seconds/query | Changed Top-100 orders | Maximum shared score difference |
|---|---:|---:|---:|---:|
| 1 | 87.069324 | 0.080960 | 0 | 0.000000000000 |
| 2 | 123.890959 | 0.055064 | 0 | 0.000000238419 |
| 4 | 146.443171 | 0.045739 | 0 | 0.000000238419 |
| 8 | 161.424092 | 0.042155 | 0 | 0.000000238419 |

Batching amortizes real query-encoding work; all query texts still pass through the pinned BGE model. The native replay is separate from HTTP timing. [Native replay and actual rankings](native-profile.json), [native terminal log](native-profile.log).

## Bounds and tests

- Native model jobs remain capped at **four**, with **two intra-op threads** per job (nominal budget eight, not an OS-thread-count claim). Each job owns its model/tokenizer replica until native completion.
- Batch size is capped at **eight**; at most **four asynchronous coordinators** exist. Their pending queue is capped at **64**. Each coordinator holds at most one group of eight. HTTP admission remains **64** and the Uvicorn guard **128**. These separate bounds also limit retained cancelled-request data; cancellation does not permit overlapping use of a model replica.
- The Go engine admission cap remains **eight**, and live listener connections **sixteen**. No load clients, candidate depths, token limits or failure thresholds were relaxed.
- **37 Python tests**, all Go race tests and Go vet pass. New tests cover batch/output correspondence, partial groups, overflow/retry accounting, cancellation while native work is still active, immediate low-pressure handling, and real mixed filtered/unfiltered ranking/vector equivalence.

The eight-query default trigger is twice the native worker count for this four-worker profile. The collection window caps intentional batch assembly wait, not total request/model-slot queue time. Batching is explicitly enabled in the recommended profile; existing launch commands keep the single-query path.

## Reproduction and raw evidence

```bash
# With installed dependencies and the existing pinned model/index artifacts:
export PYTHONPATH="$PWD/py"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2 NUMBA_NUM_THREADS=4
python scripts/benchmark-expanded.py \
  --data data/sec-scale-v2-evaluation-20261031 \
  --vectors indexes/vector-sec-scale-v2-pq128-20261031 \
  --sparse indexes/sparse-sec-scale-v2-20261031 \
  --reranker-onnx models/reranker-onnx-fp32-optimized-20261031 \
  --inference-workers 4 --threads 2 --rerank-batch-size 1 \
  --dense-batch-size 8 --dense-batch-trigger 8 --dense-batch-wait-ms 2 \
  --out bench_kit/results/optimization/dense-batch-rerun

python bench_kit/probe_dense.py \
  --index indexes/vector-sec-scale-v2-pq128-20261031 \
  --queries data/sec-scale-v2-evaluation-20261031/queries.jsonl \
  --reference bench_kit/results/optimization/dense-batching-20261031/baseline-single/evaluation/per-query.jsonl \
  --onnx models/reranker-onnx-fp32-optimized-20261031 \
  --dense-batch-size 8 --requests 2000 \
  --out bench_kit/results/optimization/dense-probe-rerun
```

Use fresh output directories; repeat with `--dense-batch-size 1` for a control. `bench_kit/load` now accepts `-scenario all|hybrid|dense`, retaining `all` as its default. `verify_dense_probe.py` recomputes all long-probe counts/statuses/percentiles/QPS and independently compares concurrent response IDs/scores to the frozen reference. `compare_reranker.py` verifies all standard rankings, nDCG, load observations and model hashes. `analyze_queueing.py` now distinguishes individual and grouped native calls from logical request counts.

[Standard independent comparison](adaptive-b8/independent-comparison.json) · [Standard optimized load](adaptive-b8/load/report.json) · [Standard control](baseline-single/load/report.json) · [Optimized hybrid CSV](adaptive-b8/load/hybrid-requests.csv) · [Optimized dense CSV](adaptive-b8/load/dense-requests.csv) · [Queueing analysis](adaptive-b8/queueing-analysis.json) · [Final standard health](adaptive-b8/health-after.json).

[Long control CSV](probe-single/load/dense-requests.csv) · [Long batched CSV](probe-adaptive/load/dense-requests.csv) · [Long-probe independent checks](dense-probe-comparison.json) · [Control quality reference](baseline-single/evaluation/per-query.jsonl) · [Batched concurrent HTTP quality](probe-adaptive/concurrent-quality.json) · [Health after the timed probe](probe-adaptive/health-after.json) · [Health after concurrent quality](probe-adaptive/health-after-quality.json).

[Python tests](python-tests.log) · [Go race tests](go-tests.log) · [Go vet](go-vet.log) · [Standard control log](baseline-single.log) · [Adaptive log](adaptive-b8.log) · [Long control log](probe-single.log) · [Long adaptive log](probe-adaptive.log). All owned services were stopped after their runs. No commit or push was performed.
