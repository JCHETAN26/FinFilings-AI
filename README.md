# FinFilings AI

Go module: `github.com/JCHETAN26/FinFillings-AI`.

**Status:** Phases 1 and 2 are approved. Phase 3 implements a bounded asynchronous audit agent with structured planning/critique and verified extractive citations. Phase 4 load testing and the consolidated metrics harness are implemented; see [all verified benchmarks](bench_kit/RESULTS.md). Tenant isolation and UI are not implemented.

- `pkg/model`: validated SECChunk records.
- `pkg/index`: disk-backed immutable segments, delta/PFOR compressed postings, mmap, BM25, bounded analysis/search admission, and instance-owned pools. SSE2 pack/unpack kernels cover widths 8/16 on amd64; other widths/platforms use scalar code.
- `py/embed.py`: real pinned BGE embeddings, disk-backed float32 reference vectors, trained FAISS PQ8, saved centroids and SQLite passage storage.
- `pkg/hybrid`: bounded concurrent BM25/PQ8 retrieval, RRF with k=60 and Top-50 fusion, and real cross-encoder Top-10 reranking with passage text/metadata. Failed branches are errors, not silent fallback.
- `cmd/finfilings`: real sparse index builder and bounded loopback HTTP bridge.
- `py/agent`: typed Plan → Retrieve_Evaluate → Synthesize controller, filing filters, at most two critic retries per subquery, and exact SEC quote/chunk/accession validation.
- `bench_kit`: real SEC concurrent sparse benchmark and FinQA-label hybrid evaluation with raw logs, exact vector payload accounting, and isolated RAM probes.

## Verification

```bash
go test ./...
go test -race ./...
go test -tags=purego ./...
go vet ./...
```

Networking tests need loopback sockets. The managed Codex sandbox requires network-enabled execution for those tests. `scripts/verify-phase1.sh` also captures sparse memory diagnostics and SIMD disassembly. Python dependencies and verification commands are documented in the Phase 2 report.

## Measured evidence

- [Phase 1 real SEC load](bench_kit/RESULTS.md): 702 actual SEC passages, 50 workers, 10 million searches; small warm-cache workload.
- [Phase 2 results and reproduction](docs/build/phase2.md): 19,511 actual financial passages; 100 fixed published FinQA supporting-fact queries. nDCG@10: BM25 0.4919, PQ8 0.4458, hybrid 0.5196, reranked 0.5757. Exact vector native payload reduced 95.56%; isolated RSS growth reduced 92.39%. These are vector-component measurements, not whole-service RAM savings.
- [Phase 3 agent and verification](docs/build/phase3.md): ten real-model audit prompts passed source/citation checks: 54 verified citations, six fully covered positive audits, two partial audits and two correct abstentions. [Per-prompt results](bench_kit/results/phase3/RESULTS.md) include retries and evidence gaps.
- [Phase 1 storage design](docs/build/phase1.md), [vector/inference components](py/README.md), [benchmark harness](bench_kit/README.md).

Artifacts and datasets remain on disk under ignored `data/`, `indexes/`, and `/tmp` model/environment caches. Reviewable results, hashes and provenance are saved under `bench_kit/results/`. Test fixtures are explicitly labeled; production benchmarks do not substitute generated documents for real input. The evaluation measures retrieval of published support facts, not generated-answer accuracy or production availability.
