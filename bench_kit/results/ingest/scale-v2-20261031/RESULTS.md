# Larger real SEC corpus and hybrid benchmark (parser v2)

Verified on 2026-10-31. This is a frozen-FinQA-query stress test with additional live SEC passages, not exhaustive relevance evaluation of the new SEC corpus.

Ingested **148 real 10-K filings from 77 companies**, yielding **29,941 section-tagged passages in 9 quarter shards**. The fixed cohort contained 100 companies, up to two recent annual filings each. The deterministic parser rejected 43 filings rather than guessing section tags. Accepted raw filings totaled 627,027,402 bytes. [Coverage and gaps](ingestion-summary.json), [per-filing provenance](ingestion-report.json), [fixed cohort](cohort.json).

The benchmark retains the original 19,511 passages and all 100 published FinQA query/judgment records, and appends 29,941 SEC passages: **49,452 total candidates**. Query and qrel hashes were independently checked against Phase 2. Newly added passages are unlabeled; a lower score can reflect relevant but unjudged retrieval as well as errors.

| Ranking stage | Original 19,511 passages | Expanded corpus nDCG@10 |
|---|---:|---:|
| sparse | 0.491910 | 0.496070 |
| dense | 0.445773 | 0.351491 |
| hybrid | 0.519558 | 0.466944 |
| reranked | 0.575744 | 0.537220 |

PQ8 recall@10 versus exact float32 search: **0.531000** (100 fixed queries). This is quantization fidelity, not human relevance.

Exact native vector payload: **75,958,272 → 2,766,912 bytes**, a **96.3573% reduction**. Isolated median observed RSS growth: raw 76,288,000 bytes; PQ8 3,784,704 bytes. These exclude model and passage-store RAM.

Training used 20,000 deterministic real-vector samples; recommended minimum 9,984; adequate training count: True. Reused 45,471 checksum-verified BGE embeddings after exact ID/text/configuration checks; encoded 3,981 passages. PQ codebooks were retrained on the expanded corpus.

| 50-worker endpoint | Successes / requests | p50 ms | p95 ms | p99 ms | Successful QPS | Failure rate |
|---|---:|---:|---:|---:|---:|---:|
| hybrid | 200/200 | 94658.584644 | 102151.133860 | 102891.272503 | 0.522096 | 0.00% |
| dense | 200/200 | 895.731805 | 921.024493 | 925.284735 | 55.330507 | 0.00% |

Compared with the original benchmark, reranked nDCG@10 fell from 0.575744 to 0.537220; PQ8/exact ID recall fell from 0.685000 to 0.531000. On this run, RRF alone scored below BM25. The cross-encoder recovered quality, but did not reach the historical result.

Across the supervised quality, warmup and load requests, reranking accounted for 98.06% of instrumented native inference work (579.690 s rank vs 11.473 s dense). This points to CPU reranking and queueing as the full-pipeline latency bottleneck. Admission peaked at 50/64, with 0 application-budget rejections and 1 native inference worker. These counters span the full supervised run, not load alone.


Load percentiles above include all attempts, using nearest rank over integer nanoseconds. Separate successful-only distributions, status counts and raw observations are retained. This finite closed-loop run has five excluded warmups per endpoint, 200 measured requests per endpoint, fifty clients, eight hybrid admissions, and one native inference worker. It is not an open-loop capacity or soak test.

Historical optimized 19,511-passage hybrid run: p99 30.675532 seconds and 1.756374 QPS. The larger-corpus run is a separate local observation, not an isolated causal or statistical significance claim.

Live SEC downloads used the operator-supplied identifying contact header at five requests/second. The ingestion output keeps nine true filing-quarter shards. For the directly comparable FinQA quality/load run, the original mixed benchmark collection plus new SEC candidates is streamed into a separate single sparse index; no filing quarters are fabricated for legacy FinQA snippets. The previously verified quarter-shard router remains available for the SEC-only collection.

[Quality report](benchmark/evaluation/report.json) · [Every ranked query](benchmark/evaluation/per-query.jsonl) · [Load report](benchmark/load/report.json) · [Independent checks](independent-checks.json) · [Service health after load](benchmark/health-after.json).

Reproduction tools: `bench_kit/prepare_expanded.py`, `py/embed.py --reuse-index`, `scripts/benchmark-expanded.py`, `bench_kit/report_expanded.py`. Model snapshots and Phase 1–4 evidence remain unchanged.

## Comparison with the earlier corpora

This run differs from [`../scale-20261031/`](../scale-20261031/RESULTS.md) only in the SEC passages. They come from the parser-v2 re-ingestion: 148 filings instead of 138, 29,941 passages instead of 26,412, with BAC, MS, AXP, PNC, COF and CVS gained and AFL boundaries corrected. The same 100 queries and judgments were used (hashes checked). All settings match: 1 inference worker, 4 threads, 64 admissions, 8 Go engine slots, 50 clients and 200 measured requests per endpoint.

| | Original (19,511) | v1 expanded (45,923) | v2 expanded (49,452) |
|---|---:|---:|---:|
| BM25 nDCG@10 | 0.4919 | 0.4943 | 0.4961 |
| PQ8 nDCG@10 | 0.4458 | 0.3794 | 0.3515 |
| RRF nDCG@10 | 0.5196 | 0.4637 | 0.4669 |
| Reranked nDCG@10 | 0.5757 | 0.5393 | 0.5372 |
| PQ8 recall@10 vs exact | 0.685 | 0.535 | 0.531 |
| Hybrid p99 / QPS (50 workers) | 30.68 s / 1.756 | 100.37 s / 0.536 | 102.89 s / 0.522 |
| Dense p99 / QPS (50 workers) | 0.844 s / 59.52 | 0.912 s / 55.48 | 0.925 s / 55.33 |
| Rerank time per call | 0.549 s | 1.851 s | 1.901 s |

- **Quality:** reranked quality is within 0.002 of the v1 corpus. BM25 and RRF are slightly higher and PQ8 dense retrieval is lower.
- **Labelling caveat:** the 3,529 additional passages are unlabeled, so any of them that are relevant count as misses.
- **Why hybrid is slower:** long SEC passages now make up 53.3% of the cross-encoder's Top-50 input (v1: 50.5%) and 24.0% of its Top-10 output (v1: 22.6%), so rerank cost per call rose slightly. This is the same bottleneck diagnosed in [`../scale-20261031/RESULTS.md`](../scale-20261031/RESULTS.md#hybrid-slowdown-diagnosis-and-4-worker-rerun).
- **Not re-run here:** the 4-inference-worker configuration.
- **Percentiles:** recomputed from the raw request CSVs; they match the load report ([`benchmark/independent-percentile-check.json`](benchmark/independent-percentile-check.json)).

These are single local closed-loop observations on a non-isolated desktop with background services. They are not significance or capacity claims.
