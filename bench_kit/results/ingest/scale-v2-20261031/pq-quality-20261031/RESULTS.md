# Label-free PQ8 calibration — 2026-10-31

Real 49,452-passage BGE embeddings; 384 dimensions, normalized inner product. Each candidate retains 8-bit quantization per sub-vector. Same 20,000 deterministic training rows, seed 20261030, 25 iterations. Requantization uses two CPU threads and a bounded training buffer, with batched addition from mmap. Neither embeddings nor passage content change.

128 deterministic document vectors outside the training sample serve as calibration queries. Self hits are excluded from both exact float32 and PQ rankings. No FinQA queries or relevance judgments are read during selection. Document-vector recall diagnoses compression fidelity and is not the financial-query benchmark. Identical text at distinct IDs can penalize ID recall.

Selection rule: maximize calibration recall subject to at least 90% native vector payload savings.

| Sub-vectors / bytes per code | Calibration recall@10 | Native payload bytes | Savings vs float32 |
|---|---:|---:|---:|
| 48 | 0.583594 | 2,766,912 | 96.3573% |
| 96 | 0.753906 | 5,140,608 | 93.2323% |
| 128 | 0.838281 | 6,723,072 | 91.1490% |

Selected: **128 sub-vectors**. Raw float32 payload: 75,958,272 bytes. Native payload means codes plus actual materialized FAISS buffers; it excludes models, passage store and allocator overhead.

The full financial-query and 50-worker run is documented in [the benchmark report](../benchmark-pq128-workers4-threads2/RESULTS.md). Raw row selections, exact neighbors and per-query calibration recalls are in [calibration.json](calibration.json). The rewritten bounded sampler was replayed and produced byte-identical calibration output.

Reproduction, from repository root with installed Python dependencies:

```bash
export PYTHONPATH="$PWD/py:$PWD/bench_kit"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2
python py/requantize.py --source indexes/vector-sec-scale-v2-20261031 \
  --out indexes/vector-sec-scale-v2-pq128-rerun --subvectors 128 --threads 2
python bench_kit/calibrate_pq.py \
  --indexes indexes/vector-sec-scale-v2-20261031 indexes/vector-sec-scale-v2-pq96-20261031 indexes/vector-sec-scale-v2-pq128-rerun \
  --out /tmp/calibration-rerun.json
```

The 96-code candidate can be rebuilt with the same command using `--subvectors 96` and a fresh output directory. Existing index outputs are refused. Source embeddings and passage-store copies are checksum verified. Build logs: [96-byte](requantize96.log), [128-byte](requantize128.log). Verification: [Python tests](python-tests.log), [Go race tests](go-tests.log), [Go vet](go-vet.log).
