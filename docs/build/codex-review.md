# Phase 1 build-manager verification

Verified locally on 2026-10-30 (America/Phoenix). Claude Code implemented the Go foundation, then addressed a second review covering analyzer budgets, concurrent search admission, exclusive writer ownership, real SIMD paths, and delta continuation overflow. Prompts are preserved alongside this report.

Independent checks passed with `bash scripts/verify-phase1.sh`: uncached unit tests, race tests, the `purego` fallback build, `go vet`, an opt-in memory diagnostic, and disassembly capture. The script places Go and ccache working files under `/tmp` to run inside the managed filesystem sandbox. Raw results and source hashes are in `codex-verification/`.

The original Claude benchmark output was extracted programmatically from its actual tool result into `codex-verification/claude-benchmarks.txt`; it contains all 160 benchmark lines from five runs and no truncation. No benchmark numbers were fabricated. Benchmark setup uses deterministic generated test fixtures and is not a real SEC corpus or production load test.

The independent memory diagnostic maps a 191,995-byte segment for 1,000 fixture chunks. The log records Go HeapAlloc and process VmRSS before open, after open, after 100 sequential searches, and after close. These are whole-test-process snapshots, including runtime and fixture setup, not a corpus capacity estimate. Heap metrics exclude mapped pages. RSS snapshots can vary with paging and runtime activity. Raw measurements, context, and reproduction commands are in `codex-verification/fixture-memory.txt` and `pkg/index/memory_profile_test.go`.

Scope and limits:

- Actual SSE2 kernels handle 8- and 16-bit packing/unpacking on amd64; all other widths, tails, and platforms have scalar fallback. Small-gap fixture postings can select scalar widths; kernel microbenchmarks do not establish end-to-end SIMD speedup.
- Ingestion is disk-backed with bounded segment and per-document analysis budgets. Accounting includes estimates of Go container overhead and is not an OS RSS ceiling. Search admission limits simultaneous scratch ownership; `sync.Pool` retention is managed by Go.
- The sparse index returns copied chunk metadata. Passage text requires a separate disk-backed store before RAG integration.
- This is a build-once index. Updates/deletes, compaction, duplicate-ID enforcement, tenant isolation, SEC acquisition, real relevance labels, and production load evaluation remain outside this Phase 1 implementation.
- Phases 2–4 have not started. Passing Phase 1 tests does not establish vector recall, RAG quality, multi-tenancy, or production latency.

Changes remain local and uncommitted; no code has been pushed to GitHub.

Subsequent work: the user authorized a Phase 1 real-data benchmark before Phase 2. See [bench_kit/RESULTS.md](../../bench_kit/RESULTS.md) for the public-SEC download/preparation path, scalar adapter cleanup, fresh verification, and actual 50-worker results. This supersedes the earlier absence of SEC acquisition/real-corpus baseline evidence; production relevance and tenant isolation remain untested.
