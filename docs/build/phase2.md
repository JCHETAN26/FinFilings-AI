# Phase 2 — implementation and measured results

Completed locally on 2026-10-30. Phase 1 storage/search APIs are preserved. No Phase 3 agent or UI work has begun. All code remains local and uncommitted.

## Implemented path

1. `py/embed.py` streams SEC passage JSONL through pinned `BAAI/bge-small-en-v1.5` (384-dimensional normalized CLS vectors). Documents have no query prefix; queries use `Represent this sentence for searching relevant passages: `. Vector generation is batched; float32 references live in a disk-backed NumPy mmap. Passage IDs, text and metadata live in SQLite, not a corpus-sized Python list.
2. Real FAISS `IndexPQ(384, 48, 8, METRIC_INNER_PRODUCT)` trains on 19,511 genuine vectors, exceeding the 9,984 recommended training count for 256 centroids. Seed 20261030, 25 iterations. Each vector has a 48-byte code. The actual codebook is saved both inside `index.pq8.faiss` and as `centroids.npy`; integration checks show exact equality. Raw float32 references are retained on disk for evaluation, not searched by the serving index.
3. `pkg/hybrid` admits at most eight requests and launches exactly two retrieval goroutines per admitted request: real Go BM25 plus an HTTP call to real PQ8. Both branches retrieve 100 IDs. RRF uses one-based ranks and `1/(60+rank)`, combines overlap once per branch, deterministically breaks ties by ID, and returns up to 50 candidates. Either branch failing cancels the peer and returns an error. Corpus fingerprints must agree.
4. The Python sidecar uses the actual `cross-encoder/ms-marco-MiniLM-L6-v2` checkpoint (canonical spelling), batches query/passage inference in groups of eight, and returns Top-10 raw-logit ranks with actual text and metadata retrieved from SQLite. Go rejects unknown/duplicate IDs, missing passages, inconsistent metadata, non-finite scores and malformed/oversized responses.
5. Model files have pinned revisions and SHA-256 checks. Inference is CPU-resident, as shown in the final health log. One dedicated inference executor worker and a slot released on actual future completion preserve the native-work bound when HTTP calls cancel. The sidecar caps connections/tasks at 16, request bodies at 32 KiB and candidate IDs at 50. Go caps live HTTP connections at 16 and engine requests at eight; all relevant buffers and queues have limits.

## Quality benchmark

**Real corpus:** 19,511 passages: the prior 702 SEC passages plus deduplicated report facts from the FinQA development contexts and the first 500 training contexts. Text comes from the published report contexts, and every table row uses the same official renderer; no QA answers, programs, gold strings or relevance labels are injected into passage text.

**Qrels:** the first 100 eligible FinQA development questions, fixed in source order before inference or parameter scoring. Their published supporting-fact IDs are binary judgments. All gold IDs exist in the corpus. Tickers without a current SEC CIK mapping are excluded and counted in provenance; the benchmark is a selected development subset, not a complete official FinQA leaderboard run. Corpus construction uses development contexts as an unsupervised search collection; no encoder/reranker is fine-tuned on its labels.

| Retrieval stage | nDCG@10 |
|---|---:|
| Go BM25 | 0.491910 |
| FAISS PQ8 only | 0.445773 |
| Hybrid RRF, k=60 | 0.519558 |
| Hybrid + MiniLM cross-encoder | 0.575744 |

Hybrid minus sparse: **+0.027648**; reranked minus sparse: **+0.083835** absolute nDCG. Dense-only is worse than BM25 on this subset; fusion and reranking improve the measured mean. No statistical-significance or generalization claim is made.

`ranx` receives descending ordinal scores solely to preserve returned ranking order, including deterministic ties. Raw actual model/BM25/RRF scores remain in the per-query logs. A separate scalar implementation recomputed all 100 binary-judgment nDCGs from those logs and matched ranx within `1e-12`.

PQ8 recall@10 against exact float32 inner-product retrieval: **0.685**. The exact baseline scans disk-backed raw vectors in blocks of 2,048, retaining only Top-10. It does not create an unquantized corpus-wide serving index. Identical-text documents at different locations can create interchangeable exact-score ties, so ID recall is not a semantic relevance score.

## Vector memory and latency

| Measurement | Float32 | PQ8 |
|---|---:|---:|
| Exact native vector payload | 29,968,896 B | 1,329,744 B |
| Serialized FAISS index | 29,968,941 B | 1,329,830 B |
| Observed median RSS growth, isolated probes | 30,306,304 B | 2,306,048 B |

PQ8 payload includes **936,528 bytes of codes + 393,216 bytes of centroids**, with zero materialized auxiliary tables in this run. Exact native payload reduction: **95.562920%**. Observed RSS-growth reduction: **92.390864%**. Three independent subprocesses per mode loaded and searched the real index. The raw subprocess builds a genuine `IndexFlatIP` from the mmap in batches, then unmaps the reference file before its RSS sample.

Native buffer lengths are exact logical payload sizes; FAISS does not expose all allocator headers or reserved capacity. RSS is a measured process/page statistic, not an exact allocator decomposition. Both are reported rather than calling one the other. These probes exclude model and passage-store RAM. The full model sidecar used **1,612,705,792 bytes (1.50 GiB) RSS** after evaluation; it did not achieve a 95.56% reduction in total application RAM.

Full pipeline, warm models, **serial CPU** queries: p50 **0.921 s**, p99 **2.674 s**, max **2.717 s**. This includes BGE query encoding, Go parallel sparse/dense retrieval, RRF, and Top-50 real cross-encoder inference. It is not the Phase 1 sparse-only 0.048 ms p99, a GPU benchmark, or a 50-worker hybrid load result. The host can expose CUDA outside the managed sandbox, but both actual inference devices were `cpu`.

## Reproduce

The project SSD does not support the symlinks needed by Python venv, so use a filesystem such as `/tmp`. A clean environment avoids the host's unrelated vision/NumPy packages; the recorded run reused its PyTorch installation with pinned compatible text dependencies. Model math remained real PyTorch throughout; no fake torchvision operators were registered.

```bash
python3 -m venv /tmp/finfilings-phase2-clean
export PHASE2_PYTHON=/tmp/finfilings-phase2-clean/bin/python
"$PHASE2_PYTHON" -m pip install torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu
"$PHASE2_PYTHON" -m pip install -r py/requirements.txt
export HF_HOME=/tmp/finfilings-hf-cache
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=4 NUMBA_NUM_THREADS=4
export MPLCONFIGDIR=/tmp/finfilings-matplotlib
export GOCACHE=/tmp/finfilings-go-cache
export CCACHE_DIR=/tmp/finfilings-ccache CCACHE_TEMPDIR=/tmp/finfilings-ccache-tmp
```

Set `SEC_USER_AGENT` to a project/research identifier and contact email before SEC downloads. From the repository root, on **fresh output paths**:

```bash
python3 bench_kit/prepare_sec.py
python3 bench_kit/fetch_finqa.py --user-agent "$SEC_USER_AGENT"
"$PHASE2_PYTHON" bench_kit/prepare_finqa.py --train-contexts 500 --query-limit 100
HF_HUB_DISABLE_XET=1 "$PHASE2_PYTHON" py/fetch_models.py
"$PHASE2_PYTHON" py/embed.py

go build -o /tmp/finfilings-phase2 ./cmd/finfilings
/tmp/finfilings-phase2 -mode build
```

Raw documents and prepared/indexed artifacts already exist for this run. To reuse them, skip their build steps. Builders reject existing output directories instead of overwriting live indexes. FinQA raw files and model revisions are pinned; the current SEC ticker mapping is cached and its exact snapshot hash is in provenance. A new ticker snapshot can change eligibility and must be reported as a different corpus.

Start these in separate terminals (loopback only):

```bash
"$PHASE2_PYTHON" py/service.py
/tmp/finfilings-phase2 -mode serve
```

Then verify and evaluate, giving the evaluation a fresh result directory:

```bash
go test ./...
go test -race ./...
go test -tags=purego ./...
go vet ./...
"$PHASE2_PYTHON" -m unittest discover -s py -p 'test_*.py'
"$PHASE2_PYTHON" bench_kit/verify_phase2.py
"$PHASE2_PYTHON" bench_kit/eval_hybrid.py --out bench_kit/results/phase2/repeated-evaluation
```

Network-enabled execution is required for loopback socket tests inside the managed Codex sandbox. Running the services from ordinary SSH terminals does not require Codex sandbox configuration. Stop the test services when finished; the workers used for this build were shut down after verification.

## API

- Python `POST /dense`: `{"query":"interest rate risk","k":100}` returns actual PQ8 IDs/scores and corpus fingerprint. Optional `include_vector:true` supports exact-baseline auditing.
- Python `POST /rerank`: query, up to 50 unique IDs, and `k<=10`; returns scored passages with text and metadata. Unknown IDs fail.
- Go `POST /search`: `{"query":"interest rate risk","rerank":true}` returns sparse, dense, hybrid, and reranked lists. No corpus text is copied across the bridge for inference input; the Python side resolves IDs from its disk store. Top-10 text is copied into the response by design.
- Both expose `/health`; the final Python health includes exact model revisions and actual devices. These are local development services, with no implemented authentication or tenant boundary.

## Evidence and limitations

All artifacts below are relative to the repository root:

- [Raw evaluation log](../../bench_kit/results/phase2/evaluation.log), [JSON report](../../bench_kit/results/phase2/evaluation/report.json), [per-query real rankings](../../bench_kit/results/phase2/evaluation/per-query.jsonl).
- [Independent metric check](../../bench_kit/results/phase2/metric-check.json), [real-model integration](../../bench_kit/results/phase2/integration-final.log).
- [Go tests](../../bench_kit/results/phase2/go-tests.log), [race](../../bench_kit/results/phase2/go-race.log), [scalar fallback](../../bench_kit/results/phase2/go-purego.log), [vet](../../bench_kit/results/phase2/go-vet.log), [Python tests](../../bench_kit/results/phase2/python-tests.log).
- [Corpus provenance](../../bench_kit/results/phase2/corpus-provenance.json), [model hashes/revisions](../../bench_kit/results/phase2/model-revisions.json), [vector manifest](../../bench_kit/results/phase2/vector-manifest.json), [environment](../../bench_kit/results/phase2/environment.json), [full-service RAM snapshot](../../bench_kit/results/phase2/live-sidecar-memory.json), and [source hashes](../../bench_kit/results/phase2/source-sha256.json).
- Source datasets: [FinQA](https://github.com/czyssrs/FinQA/tree/0f16e2867befa6840783e58be38c9efb9229d742), [SEC ticker metadata](https://www.sec.gov/files/company_tickers.json); model identities: [BGE small](https://huggingface.co/BAAI/bge-small-en-v1.5), [MiniLM cross-encoder](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2). The FinQA row renderer follows its published reference implementation.

Supporting-fact qrels are incomplete relevance judgments. This fixed selected dev subset does not establish answer accuracy, independent held-out generalization, or performance on all financial queries. FinQA filename years are report years, not verified filing dates, and CIKs are joined through a current ticker snapshot; those historical metadata identities have not been independently audited and must not be treated as production filing provenance. Existing SEC passages retain their original acquisition provenance. Text beyond 512 model tokens is truncated for inference; stored text is retained in full. Updates/deletes/compaction, multi-tenancy, HTTP authentication, GPU serving, and distributed availability remain outside this phase.

## Subsequent serving optimization

The measurements above retain the original Phase 2 configuration. The current sidecar uses a 64-request application budget and HTTP concurrency guard of 128, with one native inference worker. Length-bucketed cross-encoder batching reduces padding work without pruning candidates or replacing model weights. See the [verified before/after load comparison](../../bench_kit/RESULTS.md) for current serving behavior.
