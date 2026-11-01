# PQ8 quality improvement — 2026-10-31

The fixed 49,452-passage collection now uses `IndexPQ(384, 128, 8, inner_product)`, increasing each vector code from 48 to 128 bytes. Codebooks retain 20,000 training rows, 25 iterations, seed 20261030, normalized BGE embeddings and the same pinned model revisions. Raw vectors and passage store are byte-identical to the previous run. No new embedding inference or relevance-label training occurred. The prior artifacts remain intact.

[Label-free configuration selection](../pq-quality-20261031/RESULTS.md) compared 48, 96 and 128 sub-vectors on non-training document queries. The selected configuration maximized recall subject to at least 90% vector payload savings. This selection did not read the 100 financial queries or judgments.

## Quality: the same 100 financial queries

| Metric | Previous PQ48 | New PQ128 |
|---|---:|---:|
| PQ recall@10 vs exact float32 | 0.531 | 0.838 |
| BM25 nDCG@10 | 0.496070 | 0.496070 |
| Dense nDCG@10 | 0.351491 | 0.482297 |
| RRF nDCG@10 | 0.466944 | 0.507805 |
| Reranked nDCG@10 | 0.537220 | 0.553718 |

Exact float32 dense nDCG@10 is **0.493503**, measured as a disk-backed diagnostic, not a serving shortcut. New PQ dense nDCG is 0.011205 below that reference. Recall increased by 30.7 percentage points. All sparse rankings and scores were unchanged.

Judgments remain the frozen published FinQA binary supporting-fact labels. The newly ingested SEC passages are not exhaustively labeled; these values are collection-expansion retrieval measurements, not comprehensive SEC audit accuracy or held-out model-generalization claims. Identical-text IDs may penalize neighbor recall. No Phase 3 agent evaluation was rerun.

## Memory trade-off

Raw float32 payload: **75,958,272 bytes**. PQ payload rises from **2,766,912 to 6,723,072 bytes** (3.773 MiB more). Savings remain **91.1490%**, compared with 96.3573% previously. New payload includes 6,329,856 code bytes and 393,216 centroid bytes; materialized auxiliary buffers are empty.

Three isolated FAISS probes per representation measured median RSS growth of **76,292,096 bytes raw** and **8,110,080 bytes PQ**. These are page-level observations, distinct from exact buffer lengths. Neither measure includes BGE/MiniLM model replicas, SQLite, allocator headers or full application RAM. Raw float32 vectors remain on disk for diagnostics and future rebuilding; serving searches only the compressed index.

## Load: unchanged bounded concurrency

Four private inference replicas, two intra-op CPU threads per job (nominal budget eight), 64 admitted sidecar requests, Go engine admission eight and connection cap sixteen. Fifty clients issue 200 measured requests per endpoint, with five excluded warmups; hybrid and dense scenarios run sequentially. RRF k=60, sparse/dense depths 100, fused Top-50, reranked Top-10 and 512-token limit are unchanged. This thread setting bounds per-job parallelism, not the process-wide OS thread count.

| Endpoint / configuration | p50 s | p95 s | p99 s | Successful QPS | Failures |
|---|---:|---:|---:|---:|---:|
| hybrid / PQ48 | 51.559248 | 57.012374 | 58.173420 | 0.949196 | 0/200 |
| hybrid / PQ128 | 48.538136 | 52.334358 | 54.353373 | 1.011818 | 0/200 |
| dense / PQ48 | 0.724637 | 0.817213 | 1.205311 | 68.572437 | 0/200 |
| dense / PQ128 | 0.692504 | 0.748435 | 0.772436 | 69.176475 | 0/200 |

All **400/400** measured requests succeeded, with zero 503s. Final health: four inference workers, two threads each, admission peak 50/64, zero rejected, zero remaining active. Serial full-query p50 was **2.432633 s**, p99 **3.990647 s**. The supervised services were stopped after the run. These are finite local runs, not a sustained capacity guarantee; host load and changed candidate content can affect latency.

## Verification and reproduction

[Independent verification](independent-comparison.json) recomputes all four nDCG values from saved rankings, new exact-dense nDCG and neighbor recall, and all 400 request CSV statuses, empirical nearest-rank percentiles and QPS for each run. Corpus, query and qrel hashes, raw vectors/store, model configuration, training sample and concurrency settings are checked. [Evaluation report](evaluation/report.json), [raw query rankings](evaluation/per-query.jsonl), [load report](load/report.json), [hybrid CSV](load/hybrid-requests.csv), [dense CSV](load/dense-requests.csv), [terminal logs](../pq-quality-20261031/benchmark.log), [evaluation log](evaluation.log), [load log](load.log), [health](health-after.json).

All 30 Python tests, all Go race tests and Go vet passed; see [verification logs](../pq-quality-20261031/RESULTS.md). A real FAISS rebuild round trip verifies immutable source bytes and manifest mismatch rejection. Filtered PQ128 search is checked against the actual full compressed ranking.

```bash
# With local dependencies/models and the saved corpus/index artifacts available:
export PYTHONPATH="$PWD/py"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2 NUMBA_NUM_THREADS=4
python scripts/benchmark-expanded.py \
  --data data/sec-scale-v2-evaluation-20261031 \
  --vectors indexes/vector-sec-scale-v2-pq128-20261031 \
  --sparse indexes/sparse-sec-scale-v2-20261031 \
  --inference-workers 4 --threads 2 \
  --out bench_kit/results/ingest/scale-v2-20261031/benchmark-pq128-rerun
```

New `embed.py` builds default to 128 sub-vectors; `--subvectors 48` retains the historical compact option. `requantize.py` creates fresh artifacts using existing verified embeddings. Its nested `requantization` record describes this rebuild; inherited embedding-generation provenance describes the original source embedding build, not new model inference.
