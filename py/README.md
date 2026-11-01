# Phase 2 vector and inference components

- `fetch_models.py`: downloads exact pinned safetensors snapshots; writes local paths, revisions and weight SHA-256 hashes. No remote custom model code or randomly initialized fallback.
- `embed.py`: streams SECChunk JSONL in batches of at most 128 (default 32), encodes real BGE CLS embeddings, normalizes them, writes float32 `.npy` mmap, and creates a SQLite ID/text/metadata store. Uses the query instruction only for queries. Transformer inputs are truncated at 512 tokens.
- `embed.py` then trains real `faiss.IndexPQ(d=384, M=128, nbits=8, METRIC_INNER_PRODUCT)` by default (`--subvectors 48` reproduces the historical compact configuration), with deterministic seed/25 iterations and a bounded training sample (default maximum 20,000). It adds vectors in batches, saves the compressed index and actual centroids, and publishes a checksummed manifest only on success. Existing output directories are refused.
- `service.py`: loopback-only FastAPI sidecar, compressed FAISS index in RAM, BGE query encoder, MiniLM reranker, and SQLite-backed passage lookup. Raw float32 corpus vectors are not loaded into a serving index. All corpus and model files are verified at startup. The default is one inference worker; `--inference-workers` permits 1–8 private encoder/reranker replicas. Completion-owned slots keep cancelled HTTP requests from releasing workers before native inference finishes. A 64-request application admission budget and Uvicorn guard of 128 bound pending work; excess application work receives 429 with Retry-After. The expanded-corpus benchmark uses four workers with two CPU threads each. Request bodies are limited to 32 KiB, dense depth to 1,000, and reranking to 50 unique IDs.
- `rerank.py`: runs `cross-encoder/ms-marco-MiniLM-L6-v2` in batches of 8 (max 32), scores real query/passage pairs, sorts raw logits, and returns up to 10 complete passages plus metadata. The canonical model name has `L6-v2`, not `L-6-v2`.

Models run on CPU in this implementation (PyTorch model/tokenizer tensors remain CPU-resident). The final integration health response records both devices as `cpu`. Host CUDA visibility is recorded separately and does not imply GPU inference. The measured full service used about 1.50 GiB RSS after evaluation; PQ8's vector-only reduction does not apply to this whole model service.

Setup, APIs, measurements, sources, and reproduction commands: [Phase 2 build report](../docs/build/phase2.md).

## Phase 3 agent

`agent/` provides Pydantic structured planning, bounded asynchronous hybrid retrieval, a real Claude JSON-schema critic with at most two retries, and extractive synthesis validated against the original SEC passages and verified accessions. Filing filters constrain both sparse and PQ8 ranking before fusion. See [Phase 3 design, limits and reproduction](../docs/build/phase3.md). The live ten-prompt runner is `scripts/verify-phase3.py`; it starts and owns both retrieval services for the entire evaluation.

Reranking now tokenizes the bounded Top-50 set once and groups pairs by token length before padding each eight-pair batch. The same float32 checkpoint, 512-token truncation, full candidate count and deterministic final score/ID sort are retained. A 100-query real-model replay preserved nDCG@10 at 0.575744; tiny floating-point differences changed two Top-10 orders. The [consolidated report](../bench_kit/RESULTS.md) records before/after 50-client performance and the original failed dense load.

## Rebuild quantization without re-embedding

`requantize.py` verifies source checksums, reads saved normalized float32 vectors through mmap, retrains PQ8 with a bounded sample, and creates a fresh immutable artifact directory. It copies the passage store and vectors byte for byte, preserves the seed and training sample, and never invokes a model. Example:

```bash
python py/requantize.py \
  --source indexes/vector-sec-scale-v2-20261031 \
  --out indexes/vector-sec-scale-v2-pq128-20261031 \
  --subvectors 128 --threads 2
```

`bench_kit/calibrate_pq.py` compares actual compressed indexes on 128 deterministic document queries excluded from training, excluding self matches. Selection maximizes recall subject to at least 90% native vector payload reduction; it never reads financial queries or relevance judgments. The separate `bench_kit/eval_hybrid.py` run measures real financial-query recall and nDCG, including exact float32 dense nDCG as a diagnostic. Serving continues to use only the compressed FAISS index and the disk passage store.

## Fused float32 cross-encoder serving

Install the optional export/runtime dependencies with `python -m pip install -r py/requirements-onnx.txt`. Export the pinned checkpoint to a fresh directory, then select it explicitly when starting the sidecar:

```bash
python py/export_reranker.py --out models/reranker-onnx-fp32
python py/service.py \
  --index indexes/vector-sec-scale-v2-pq128-20261031 \
  --reranker-onnx models/reranker-onnx-fp32 \
  --inference-workers 4 --threads 2 --rerank-batch-size 1 \
  --dense-batch-size 8 --dense-batch-trigger 8
```

The export uses float32 weights, dynamic batch/sequence dimensions and exact GELU. BERT graph fusion combines attention, bias/GELU and residual/layer-normalization operations. The graph manifest pins the source model revision and weight hash and checksums the exported file. Serving verifies those fields before opening a CPU-only ONNX Runtime session. Each admitted worker owns its tokenizer/session; session execution is sequential, intra-op parallelism follows `--threads`, inter-op threads are one, and idle spinning is disabled. Existing completion-owned worker slots and bounded HTTP admission remain in force.

All 50 candidates, the same 512-token pair truncation and deterministic score/ID sorting are retained. `--rerank-batch-size 1` is now the ONNX default and removes inter-pair padding. PyTorch retains its historical batch-eight default; explicit overrides remain available. Backend, graph hash and batch size appear in `/health`. The [measured CPU and queueing comparison](../bench_kit/results/optimization/pair-reuse-20261031/RESULTS.md) records quality, native work, queueing and 50-client load.

Against a fresh batch-two control, single-pair ONNX improves 50-client hybrid p99 from 42.495622 to 39.916006 seconds and QPS from 1.301409 to 1.378062. Serial p50 is essentially unchanged and dense p99 increased. All 100 queries retain identical Top-10 ordering. Eight single-thread workers previously regressed hybrid latency and are not recommended for this workload.

The Go `/search` response now includes `timings` for engine admission, sparse/dense retrieval, fusion, rerank round trip and engine total. Raw load CSVs retain these fields; missing engine timings on the standalone dense endpoint are blank. `health-after-evaluation.json` and final health allow sidecar work/wait/phase differences to be analyzed for the load portion. Use `bench_kit/analyze_queueing.py` to verify the decomposition. Native profiles remain separate from latency runs so operator tracing does not inflate reported benchmark latency.

## Pressure-triggered dense query batching

The recommended four-worker profile adds `--dense-batch-size 8 --dense-batch-trigger 8`. Queries at or below eight live dense calls keep the immediate path. Larger bursts can share one BGE call for up to eight requests, with a maximum two-millisecond collection window (`--dense-batch-wait-ms 2`). All batches use the existing four completion-owned worker slots, private tokenizers/models and per-request PQ search depths/filters. The same model, 512-token preprocessing, normalization and source-ID mapping are retained.

Batching is opt-in; `--dense-batch-size 1` is the default and disables grouping. `--dense-batch-trigger 0` enables collection at all pressures for comparison. The default trigger is twice the inference-worker count, capped by the admission budget. The Go engine’s eight-request pipeline stays on the immediate path with the recommended trigger eight. HTTP cancellation cancels the caller’s result but cannot release a native slot before its complete batch finishes. The async coordinator count, batch size, pending queue and shared native executor are all bounded.

`/health` records immediate/grouped requests, batch counts, queue peaks and native call counts. With grouping, one native call can serve multiple logical requests; `analyze_queueing.py` retains the separate counts. The verified 2,000-request dense probes reduce p99 **1.255924 → 0.552580 s** and increase throughput **73.295173 → 129.848810 QPS**. Standard hybrid throughput stays about 1.38 QPS; p99 is **39.590239 → 40.077777 s** in the matched pair, a small observed increase rather than a proven zero-regression result. [Full standard/probe results, concurrent ranking checks and reproduction](../bench_kit/results/optimization/dense-batching-20261031/RESULTS.md).
