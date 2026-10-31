# Phase 2 vector and inference components

- `fetch_models.py`: downloads exact pinned safetensors snapshots; writes local paths, revisions and weight SHA-256 hashes. No remote custom model code or randomly initialized fallback.
- `embed.py`: streams SECChunk JSONL in batches of at most 128 (default 32), encodes real BGE CLS embeddings, normalizes them, writes float32 `.npy` mmap, and creates a SQLite ID/text/metadata store. Uses the query instruction only for queries. Transformer inputs are truncated at 512 tokens.
- `embed.py` then trains real `faiss.IndexPQ(d=384, M=48, nbits=8, METRIC_INNER_PRODUCT)` with deterministic seed/25 iterations and a bounded training sample (default maximum 20,000). It adds vectors in batches, saves the compressed index and actual centroids, and publishes a checksummed manifest only on success. Existing output directories are refused.
- `service.py`: loopback-only FastAPI sidecar, compressed FAISS index in RAM, BGE query encoder, MiniLM reranker, and SQLite-backed passage lookup. Raw float32 corpus vectors are not loaded into a serving index. All corpus and model files are verified at startup. One model-inference executor worker and one completion-owned admission slot prevent cancellation from spawning overlapping native work; The optimized service has a 64-request application admission budget and a Uvicorn guard of 128; excess application work receives 429 with Retry-After. The native inference executor remains single-worker. Request bodies are limited to 32 KiB, dense depth to 1,000, and reranking to 50 unique IDs.
- `rerank.py`: runs `cross-encoder/ms-marco-MiniLM-L6-v2` in batches of 8 (max 32), scores real query/passage pairs, sorts raw logits, and returns up to 10 complete passages plus metadata. The canonical model name has `L6-v2`, not `L-6-v2`.

Models run on CPU in this implementation (PyTorch model/tokenizer tensors remain CPU-resident). The final integration health response records both devices as `cpu`. Host CUDA visibility is recorded separately and does not imply GPU inference. The measured full service used about 1.50 GiB RSS after evaluation; PQ8's vector-only reduction does not apply to this whole model service.

Setup, APIs, measurements, sources, and reproduction commands: [Phase 2 build report](../docs/build/phase2.md).

## Phase 3 agent

`agent/` provides Pydantic structured planning, bounded asynchronous hybrid retrieval, a real Claude JSON-schema critic with at most two retries, and extractive synthesis validated against the original SEC passages and verified accessions. Filing filters constrain both sparse and PQ8 ranking before fusion. See [Phase 3 design, limits and reproduction](../docs/build/phase3.md). The live ten-prompt runner is `scripts/verify-phase3.py`; it starts and owns both retrieval services for the entire evaluation.

Reranking now tokenizes the bounded Top-50 set once and groups pairs by token length before padding each eight-pair batch. The same float32 checkpoint, 512-token truncation, full candidate count and deterministic final score/ID sort are retained. A 100-query real-model replay preserved nDCG@10 at 0.575744; tiny floating-point differences changed two Top-10 orders. The [consolidated report](../bench_kit/RESULTS.md) records before/after 50-client performance and the original failed dense load.
