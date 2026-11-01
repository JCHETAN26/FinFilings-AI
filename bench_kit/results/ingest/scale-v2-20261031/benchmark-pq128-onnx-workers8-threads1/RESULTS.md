# Eight single-thread workers: rejected hybrid configuration

The complete real quality and load run passed 400/400 requests and preserved every query’s nDCG, but hybrid p99 regressed from 54.353373 to **60.089434 seconds** and QPS fell from 1.011818 to **0.978849**. Serial p50 increased to **3.214308 seconds**. Keep four workers × two threads for the tested hybrid path.

[Full comparison, constraints and reproduction](../../../optimization/cross-encoder-20261031/RESULTS.md) · [Independent checks](independent-comparison.json) · [Evaluation](evaluation/report.json) · [Raw rankings](evaluation/per-query.jsonl) · [Load report](load/report.json) · [Hybrid CSV](load/hybrid-requests.csv) · [Dense CSV](load/dense-requests.csv) · [Health](health-after.json) · [Quality RSS snapshot](quality-memory-snapshot.json) · [Load RSS snapshot](load-memory-snapshot.json).

Native jobs are capped at eight, intra-op threads at one each, and pending sidecar admission at 64. Nominal CPU budget stays eight. RSS snapshots are not peak measurements. Both Top-10 order changes swap identical-text source IDs; all Top-10 sets remain identical. The finite local run does not isolate the cause of contention or establish sustained capacity. Services stopped after completion.
