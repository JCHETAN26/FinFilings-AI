# Bounded CPU configuration on the parser-v2 corpus

Verified on 2026-10-31 with the same 49,452-passage vector artifacts, sparse index, pinned models, 100 FinQA queries/qrels, Top-50 RRF candidates and 512-token inputs. The baseline used one inference worker with four CPU threads per job; the experiment used four private model/tokenizer replicas with two threads per job. This compares complete runtime configurations, not the isolated effect of the thread count.

| Endpoint / configuration | p50 s | p95 s | p99 s | Successful QPS | Failures |
|---|---:|---:|---:|---:|---:|
| hybrid, 1 worker × 4 threads | 94.658585 | 102.151134 | 102.891273 | 0.522096 | 0/200 |
| hybrid, 4 workers × 2 threads | 51.559248 | 57.012374 | 58.173420 | 0.949196 | 0/200 |
| dense, 1 worker × 4 threads | 0.895732 | 0.921024 | 0.925285 | 55.330507 | 0/200 |
| dense, 4 workers × 2 threads | 0.724637 | 0.817213 | 1.205311 | 68.572437 | 0/200 |

Hybrid throughput increased **1.818×**, and p99 decreased **43.46%**. Both new loads completed 200/200 requests with no failures. Each load used 50 closed-loop clients and five excluded warmups; percentiles are nearest-rank integer-nanosecond observations. This is one non-isolated desktop run, not a capacity or significance claim.

## Quality and trade-offs

All returned ID orders were identical for all 100 queries at each of the four stages. nDCG@10 was unchanged: BM25 0.496070, PQ8 0.351491, RRF 0.466944, cross-encoder 0.537220. PQ8/exact float32 recall@10 remained 0.531. The checker independently recomputed nDCG and all percentiles from both runs, checking input hashes and identical vector manifests. These are published FinQA support judgments, not exhaustive labels for the newly ingested SEC passages.

Serial end-to-end p50 increased from 1.986834 to 2.673580 seconds. Dense p99 also increased from 0.925285 to 1.205311 seconds, despite higher dense throughput. This configuration favors hybrid throughput under concurrent load; it does not improve every latency measure.

Four model/tokenizer replicas consume additional resources. Point-in-time sidecar RSS was 1.841 GiB during quality evaluation and 2.153 GiB during load. These are not peaks or memory-leak proofs. The nominal intra-op CPU budget is eight threads (four jobs × two threads); OS/runtime thread counts are higher and are recorded in the snapshots.

Observed admission peak was 50/64, with 0 admission-budget rejections. Native inference concurrency remained bounded at 4. A new regression test confirms that client cancellation retains the replica slot until native work actually finishes.

## Use the measured configuration

```bash
PYTHONPATH=py python3 py/service.py \
  --index indexes/vector-sec-scale-v2-20261031 \
  --inference-workers 4 --threads 2
```

The default service configuration remains conservative; this is an explicit deployment option. Reproduce the complete experiment with `scripts/benchmark-expanded.py --inference-workers 4 --threads 2` and the v2 data/vector/sparse paths. Run `bench_kit/compare_runtime.py` to check the resulting rankings and request traces.

[Independent comparison](independent-comparison.json) · [Quality](evaluation/report.json) · [Load](load/report.json) · [Health](health-after.json) · [Runtime parameters](parameters.json) · [Quality memory observation](memory-observation.json) · [Load memory observation](load-memory-observation.json). Both supervised services were stopped after evaluation.
