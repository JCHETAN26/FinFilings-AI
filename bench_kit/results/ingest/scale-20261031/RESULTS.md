# Larger real SEC corpus and hybrid benchmark

Verified on 2026-10-31. This is a frozen-FinQA-query stress test with additional live SEC passages, not exhaustive relevance evaluation of the new SEC corpus.

Ingested **138 real 10-K filings from 71 companies**, yielding **26,412 section-tagged passages in 9 quarter shards**. The fixed cohort contained 100 companies, up to two recent annual filings each. The deterministic parser rejected 53 filings rather than guessing section tags. Accepted raw filings totaled 548,341,779 bytes. [Coverage and gaps](ingestion-summary.json), [per-filing provenance](ingestion-report.json), [fixed cohort](cohort.json).

The benchmark retains the original 19,511 passages and all 100 published FinQA query/judgment records, and appends 26,412 SEC passages: **45,923 total candidates**. Query and qrel hashes were independently checked against Phase 2. Newly added passages are unlabeled; a lower score can reflect relevant but unjudged retrieval as well as errors.

| Ranking stage | Original 19,511 passages | Expanded corpus nDCG@10 |
|---|---:|---:|
| sparse | 0.491910 | 0.494313 |
| dense | 0.445773 | 0.379442 |
| hybrid | 0.519558 | 0.463680 |
| reranked | 0.575744 | 0.539313 |

PQ8 recall@10 versus exact float32 search: **0.535000** (100 fixed queries). This is quantization fidelity, not human relevance.

Exact native vector payload: **70,537,728 → 2,597,520 bytes**, a **96.3175% reduction**. Isolated median observed RSS growth: raw 70,873,088 bytes; PQ8 3,772,416 bytes. These exclude model and passage-store RAM.

Training used 20,000 deterministic real-vector samples; recommended minimum 9,984; adequate training count: True. Reused 19,511 checksum-verified BGE embeddings after exact ID/text/configuration checks; encoded 26,412 passages. PQ codebooks were retrained on the expanded corpus.

| 50-worker endpoint | Successes / requests | p50 ms | p95 ms | p99 ms | Successful QPS | Failure rate |
|---|---:|---:|---:|---:|---:|---:|
| hybrid | 200/200 | 92301.864238 | 98975.711448 | 100366.191495 | 0.535769 | 0.00% |
| dense | 200/200 | 890.718489 | 907.672367 | 911.780216 | 55.477333 | 0.00% |

Compared with the original benchmark, reranked nDCG@10 fell from 0.575744 to 0.539313; PQ8/exact ID recall fell from 0.685000 to 0.535000. On this run, RRF alone scored below BM25. The cross-encoder recovered quality, but did not reach the historical result.

Across the supervised quality, warmup and load requests, reranking accounted for 98.02% of instrumented native inference work (564.690 s rank vs 11.393 s dense). This points to CPU reranking and queueing as the full-pipeline latency bottleneck. Admission peaked at 50/64, with 0 application-budget rejections and 1 native inference worker. These counters span the full supervised run, not load alone.


Load percentiles above include all attempts, using nearest rank over integer nanoseconds. Separate successful-only distributions, status counts and raw observations are retained. This finite closed-loop run has five excluded warmups per endpoint, 200 measured requests per endpoint, fifty clients, eight hybrid admissions, and one native inference worker. It is not an open-loop capacity or soak test.

Historical optimized 19,511-passage hybrid run: p99 30.675532 seconds and 1.756374 QPS. The larger-corpus run is a separate local observation, not an isolated causal or statistical significance claim.

## Hybrid slowdown diagnosis and 4-worker rerun

**Cause.** The admission limit was not the bottleneck: it was 64 in this run and in the optimized 19,511-passage run. Rerank cost per call is. New SEC passages average 254 words, against 38 for the original passages. They make up 50.5% of the hybrid Top-50 candidates sent to the cross-encoder, so those candidates average 154 words. Native rerank time per call rose from 0.549 s to 1.851 s (3.37×). With one inference worker this caps throughput at about 1/1.851 ≈ 0.54 QPS, which matches the measured 0.536. [Diagnosis](rerank-diagnosis.json).

The cross-encoder also strongly prefers short passages: new SEC passages are 50.5% of its Top-50 input but only 22.6% of its Top-10 output.

**Rerun.** `py/service.py --inference-workers 4` gives each worker a private encoder, reranker and tokenizer (Hugging Face fast tokenizers are not safe to share across threads). Everything else matched the first run: corpus, artifacts, 100 queries, 4 intra-op threads, 64 admissions, 8 Go engine slots, 50 clients and 200 measured requests per endpoint.

| 50-worker endpoint | Inference workers | Successes / requests | p50 s | p95 s | p99 s | Successful QPS |
|---|---:|---:|---:|---:|---:|---:|
| hybrid | 1 | 200/200 | 92.302 | 98.976 | 100.366 | 0.536 |
| hybrid | 4 | 200/200 | 58.049 | 63.561 | 65.152 | 0.847 |
| dense | 1 | 200/200 | 0.891 | 0.908 | 0.912 | 55.477 |
| dense | 4 | 200/200 | 0.608 | 0.921 | 0.946 | 70.254 |

- **Hybrid:** 1.58× throughput, and p99 is 35.1% lower.
- **Quality:** unchanged. All four nDCG@10 stages and PQ8 recall@10 (0.535) are identical to the single-worker run.
- **Percentiles:** recomputed from the raw request CSVs (nearest rank) and identical to the load report. See [`benchmark-workers4/independent-percentile-check.json`](benchmark-workers4/independent-percentile-check.json).

**Why the gain is not 4×.** Rerank time per call rose again, from 1.851 s to 3.657 s. Four workers × 4 intra-op threads is 16 threads on 8 physical cores, so four workers can finish at most about 4/3.657 ≈ 1.09 reranks/s. Hybrid throughput stays below the original corpus's 1.756 QPS.

Untested next steps:
- match total threads to physical cores (4 workers × 2 threads);
- cap the cross-encoder's input length, which needs a quality re-check;
- rerank fewer than 50 candidates.

These are single local closed-loop observations on a non-isolated desktop. A Docker Kafka/Airflow stack was running in the background. They are not significance or capacity claims.

[4-worker quality report](benchmark-workers4/evaluation/report.json) · [4-worker load report](benchmark-workers4/load/report.json) · [4-worker service health](benchmark-workers4/health-after.json).

## Collection notes

Live SEC downloads used the operator-supplied identifying contact header at five requests/second. The ingestion output keeps nine true filing-quarter shards. For the directly comparable FinQA quality/load run, the original mixed benchmark collection plus new SEC candidates is streamed into a separate single sparse index; no filing quarters are fabricated for legacy FinQA snippets. The previously verified quarter-shard router remains available for the SEC-only collection.

[Quality report](benchmark/evaluation/report.json) · [Every ranked query](benchmark/evaluation/per-query.jsonl) · [Load report](benchmark/load/report.json) · [Independent checks](independent-checks.json) · [Service health after load](benchmark/health-after.json).

Reproduction tools: `bench_kit/prepare_expanded.py`, `py/embed.py --reuse-index`, `scripts/benchmark-expanded.py`, `bench_kit/report_expanded.py`. Model snapshots and Phase 1–4 evidence remain unchanged.
