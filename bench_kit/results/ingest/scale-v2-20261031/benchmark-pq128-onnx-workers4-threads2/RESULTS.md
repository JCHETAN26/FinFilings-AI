# FP32 cross-encoder: four workers × two threads

Same 49,452 passages, 100 financial queries, PQ128 index, RRF Top-50 candidates, float32 checkpoint and 512-token pair truncation. Fused ONNX CPU execution and batches of two replace PyTorch batches of eight. Four native inference slots, two intra-op threads each, sidecar admission 64 and Go admission eight are retained. ONNX session inter-op parallelism is one, execution is sequential and idle spinning is disabled.

## Verified quality

Reranked nDCG@10: **0.553718 → 0.553718**. Every query retains its prior nDCG. All sparse, dense and RRF scores/rankings are identical. Two Top-10 orders swap positions 2/3 between identical-text passages with different source IDs; no passages enter or leave those Top-10 sets. Maximum shared Top-10 score difference: 0.000009060. [Exact changed IDs, scores and text hashes](../../../optimization/cross-encoder-20261031/changed-orders.json).

## 50-client load

200 measured requests per endpoint, five excluded warmups. Empirical nearest-rank percentiles include queueing and full HTTP responses.

| Endpoint / backend | p50 s | p95 s | p99 s | QPS | Failures |
|---|---:|---:|---:|---:|---:|
| hybrid / PyTorch/8 | 48.538136 | 52.334358 | 54.353373 | 1.011818 | 0/200 |
| hybrid / ONNX/2 | 46.819551 | 51.084855 | 52.733752 | 1.053630 | 0/200 |
| dense / PyTorch/8 | 0.692504 | 0.748435 | 0.772436 | 69.176475 | 0/200 |
| dense / ONNX/2 | 0.777015 | 0.885906 | 0.896412 | 63.459618 | 0/200 |

Hybrid throughput increased 4.13%, and p99 decreased 2.98%. Dense latency increased. Serial full-query p50 fell 2.432633 → 1.823550 seconds. The concurrent gain is smaller than the serial gain; a finite run does not establish sustained capacity.

[Independent checks and native work/queue timings](independent-comparison.json) · [Quality report](evaluation/report.json) · [100 query rankings](evaluation/per-query.jsonl) · [Load report](load/report.json) · [Hybrid observations](load/hybrid-requests.csv) · [Dense observations](load/dense-requests.csv) · [Health](health-after.json) · [Evaluation log](evaluation.log) · [Load log](load.log).

All 400 requests succeeded. Services stopped cleanly after the run. The original frozen FinQA judgments incompletely label added SEC passages; these are retrieval metrics on this collection, not general SEC audit accuracy.
