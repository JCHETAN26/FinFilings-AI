# FinFilings AI — build rules

Repository: github.com/JCHETAN26/FinFillings-AI. Product name: FinFilings AI.

## Phase gate

Phases 1, 2 and 3 are user-approved. Phase 4 load testing and the consolidated metrics/report harness are explicitly authorized. Manifest-driven SEC ingestion, deterministic section tagging, immutable shards and real memory/resource verification are also user-authorized. UI: the earlier prohibition is superseded by explicit user authorization (2026-11-01) of a local, single-user audit workbench around the existing agent (`py/audit_web/`, `ui/`, `scripts/serve-audit-ui.py`, `docs/build/audit-ui.md`). The authorization is scoped to that workbench: it adds no tenancy, authentication, cloud deployment or production claims, binds loopback only, keeps the planner/critic/retrieval/ranking/math unchanged, and all constraints below still apply (no committed datasets, generated indexes or model caches, no invented metrics, unit-test timings are not benchmarks, no commit/push unless the user requests it). Preserve Phase 1 APIs and its recorded benchmark conditions.

1. Go foundation: validated SECChunk model, delta/PForDelta posting compression, real SIMD paths with documented coverage and scalar fallback, immutable mmap segments, BM25, bounded worker pools and instance-owned sync.Pool buffers.
2. Embeddings and FAISS PQ8, RRF (k=60), cross-encoder reranking.
3. Async Plan → Retrieve_Evaluate → Synthesize controller, bounded parallel subqueries, critic rewrites, validated inline citations.
4. bench_kit: ranx nDCG@10, k6 tail latency, PQ recall/RAM profiling on actual data.

## Required engineering constraints

- Never retain the entire passage collection in Go slices/maps or Python lists. Stream to disk-backed storage. Temporary segment buffers and per-document work must have documented explicit limits.
- Bound worker counts, queues, concurrent search scratch ownership, and top-k buffers. Reuse instance-owned buffers; avoid global mutable state.
- Mapped segment files are immutable. Respect mapping lifetimes; do not expose borrowed slices beyond Close. Validate corrupt/truncated files and propagate explicit errors.
- Do not call scalar bit packing SIMD. Document exactly which architectures and bit widths have vector kernels.
- After each Go phase: run go test ./..., go test -race ./..., go vet ./..., and relevant allocation-reporting benchmarks. Save actual output and environment details.
- Synthetic documents are allowed only as clearly labeled test fixtures. Never present generated-corpus correctness/compression/timing as real SEC retrieval quality, production load latency, or resume metrics.
- No placeholder implementations, invented metrics, or claims of multi-tenancy/isolation until enforced and tested.
- Record known limitations and unresolved requirements before phase sign-off. No automatic advancement merely because tests pass.
- Never commit secrets or generated indexes/model caches. Do not publish, commit, or push unless the user requests it.
