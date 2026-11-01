# Cross-encoder latency optimization — 2026-10-31

Recommendation: **four inference workers × two threads**, FP32 fused ONNX MiniLM, length-bucketed batches of two. Serial latency improves substantially; the measured 50-client gain is small. Keep this an explicit serving option. The **eight workers × one thread** experiment regressed hybrid tail latency and is not recommended. Defaults remain conservative; no source or evidence was committed or pushed during this work.

## What changed

The pinned `cross-encoder/ms-marco-MiniLM-L6-v2` checkpoint is exported with dynamic batch/sequence dimensions and exact GELU, then BERT-specific graph fusion combines six attention blocks, six bias/GELU operations and twelve residual/layer-normalization blocks. Weights and outputs remain float32. Sessions execute on CPU with bounded intra-op threads, one inter-op thread, sequential graph execution and idle spinning disabled. Each admitted worker owns its session and tokenizer. Source model/weight hashes and graph SHA-256 are verified at startup; `/health` records the backend and graph hash.

Length-sorted batches shrink from eight to two. In the six-query real-candidate profile, unpadded tokens stay **58,936**, while padded tokens fall **69,406 → 60,328**. PyTorch/8 averages **2.011305 s** per rerank, PyTorch/2 **1.799339 s**, and trial fused ONNX/2 **1.469037 s**. This initial profile selects execution settings without relevance labels; it is not the concurrent endpoint result. [Raw batch profile](batch-profile.json), [raw fused profile](fused-profile.json), [trial graph manifest](fused-profile-artifact-manifest.json), [final reproducible export manifest](onnx-manifest.json).

## Frozen retrieval and quality

All runs use the same 49,452 passages, raw embeddings, PQ128 index, 100 financial queries, published judgments, BGE and MiniLM source revisions, RRF k=60, sparse/dense depths 100, fused Top-50 and reranked Top-10. Pair truncation stays at 512 tokens. All 50 candidates receive actual inference. No response cache, candidate reduction or weight quantization is introduced.

All sparse, dense and RRF scores and rankings remain identical. **Every query retains its original nDCG@10** in both new runs. Aggregate values remain BM25 **0.496070**, dense **0.482297**, RRF **0.507805**, reranked **0.553718**; PQ recall remains **0.838**. Each new run changes two Top-10 orders, swapping positions 2/3 between identical-text IDs with tiny floating-point score differences. All Top-10 sets remain identical. [Four-worker changed IDs/scores/text hashes](changed-orders.json), [eight-worker changes](changed-orders-workers8.json). These judgments incompletely cover the added SEC passages; they do not establish SEC audit answer accuracy. Phase 3 was not rerun.

## Measured HTTP load

50 clients, 200 measured requests per endpoint, five excluded warmups; hybrid and dense run sequentially. All three configurations have a nominal eight-thread intra-op budget. Inference workers are capped at eight, pending sidecar requests at 64, the Go engine at eight and live Go connections at sixteen. Actual OS thread counts include idle runtime pools and are not bounded to eight by this configuration.

| Endpoint / configuration | p50 s | p95 s | p99 s | Successful QPS | Failures |
|---|---:|---:|---:|---:|---:|
| hybrid / PyTorch/8, 4×2 | 48.538136 | 52.334358 | 54.353373 | 1.011818 | 0/200 |
| hybrid / ONNX/2, 4×2 | 46.819551 | 51.084855 | 52.733752 | 1.053630 | 0/200 |
| hybrid / ONNX/2, 8×1 | 49.459848 | 56.971155 | 60.089434 | 0.978849 | 0/200 |
| dense / PyTorch/8, 4×2 | 0.692504 | 0.748435 | 0.772436 | 69.176475 | 0/200 |
| dense / ONNX/2, 4×2 | 0.777015 | 0.885906 | 0.896412 | 63.459618 | 0/200 |
| dense / ONNX/2, 8×1 | 0.558642 | 0.589443 | 1.021931 | 88.412450 | 0/200 |

Both new configurations completed **400/400** measured requests, with no 503s or other failures. Sessions and worker slots drained; supervised services were stopped after each run.

| Serial end-to-end query | p50 s | p99 s |
|---|---:|---:|
| PyTorch/8, 4×2 | 2.432633 | 3.990647 |
| ONNX/2, 4×2 | 1.823550 | 3.430892 |
| ONNX/2, 8×1 | 3.214308 | 5.580106 |

Four-worker ONNX reduces serial p50 by **25.04%**. At 50 clients, measured p99 improves **2.98%** and throughput **4.13%**. Dense p99 increases **0.772436 → 0.896412 s**; the dense test is only a few seconds long. Mean native rerank work over the same 305 calls (quality, warmups and load) falls **3.340840 → 3.035063 s**; this mixed-load mean is distinct from serial profiling. Full native work and wait records are preserved in the independent reports.

The eight-worker experiment eliminates most sidecar worker-slot waiting during hybrid load but increases native rerank work, regressing hybrid p99 to **60.089434 s**. It is evidence against simply adding replicas. Its RSS snapshots were approximately **2.66 GiB** during quality and **2.91 GiB** during load; these are point observations, not peaks. Local CPU contention, memory traffic and scheduler effects remain limits; no single cause is established by these runs. The small four-worker load improvement is a finite local observation, not a sustained-capacity guarantee or an isolated-host causal estimate. CPU reranking remains the main bottleneck under 50 clients.

## Verification and reproduction

**31 Python tests**, all Go race tests and Go vet passed. Real pinned-model ONNX/PyTorch comparisons cover batches one/two, sequences 8/129/512, masked padding and float32 score tolerance `atol=rtol=1e-4`. The runtime test checks CPU-only execution and two-thread configuration, and rejects mismatched model provenance. Existing cancellation tests ensure worker slots remain occupied until native inference actually completes. [Python log](python-tests.log), [Go race log](go-tests.log), [Go vet](go-vet.log).

Independent verifiers recompute four-stage nDCG from all raw rankings, per-query quality changes, every load CSV count/status/QPS/nearest-rank percentile, vector/corpus/query/judgment fingerprints, model/graph hashes and bounded runtime parameters. Eight-worker rebalancing is permitted only explicitly while preserving the nominal CPU budget and eight-worker cap.

[Four-worker report and raw evidence](../../ingest/scale-v2-20261031/benchmark-pq128-onnx-workers4-threads2/RESULTS.md) · [Four-worker independent checks](../../ingest/scale-v2-20261031/benchmark-pq128-onnx-workers4-threads2/independent-comparison.json) · [Eight-worker independent checks](../../ingest/scale-v2-20261031/benchmark-pq128-onnx-workers8-threads1/independent-comparison.json).

```bash
# In an environment with py/requirements.txt and pinned local model snapshots:
python -m pip install -r py/requirements-onnx.txt
export PYTHONPATH="$PWD/py"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2 NUMBA_NUM_THREADS=4
python py/export_reranker.py --out models/reranker-onnx-fp32-rerun
python scripts/benchmark-expanded.py \
  --data data/sec-scale-v2-evaluation-20261031 \
  --vectors indexes/vector-sec-scale-v2-pq128-20261031 \
  --sparse indexes/sparse-sec-scale-v2-20261031 \
  --inference-workers 4 --threads 2 --rerank-batch-size 2 \
  --reranker-onnx models/reranker-onnx-fp32-rerun \
  --out bench_kit/results/optimization/reranker-rerun
```

All output directories must be fresh. [Benchmark log](benchmark.log), [eight-worker benchmark log](benchmark-workers8.log), [export log](export-optimized.log). The graph itself remains a local generated model artifact under `models/`; its manifest and source fingerprints are saved with the evidence.
