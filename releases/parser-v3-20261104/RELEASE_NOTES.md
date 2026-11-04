# Parser-v3 release — parser-v3-20261104

**Status: real payload verified locally; unpublished.**

This version packages the verified 49,147-passage corpus and 152 SEC filings, using `deterministic-canonical-heading-v3-go`. The corpus also includes FinQA contexts. It includes the sparse mmap index, PQ128/PQ8 dense index, pinned local BGE/MiniLM snapshots, and optimized FP32 ONNX reranker.

The real archive passed complete per-file SHA-256/inventory checks, provenance/raw-source checks, corpus/index fingerprint and count checks, and exact model revision/weight checks. It was installed and activated in the immutable local store. Managed preflight and a real BM25/PQ8/RRF/ONNX reranked search passed with four workers × two threads and no provider construction. All ten returned IDs, in order, match the previously verified same-query smoke test. The installed payload was rehashed after startup and all owned services stopped.

- Corpus SHA-256: `7c9257673214d12b4d99355af53a9d8f7ebefe425f4d14de66528a6f7cf042ed`
- Bundle SHA-256: `9eaa202d9ece8d606914e26201f79bf9d7e8c13cf528c286075e89e7830b0139`
- Bundle size: 1,260,400,640 bytes
- Release manifest SHA-256: `a6514162bf8293800cf0e45ac12a22dd37819f7e3afc86163573e6e49f13000d`

[release.json](release.json) and [SHA256SUMS](SHA256SUMS) are the generated metadata; [recipe.json](recipe.json) records the pinned inputs. The source artifact baseline is `31d0e776e519a13a4acb5aeee0276004acc9837b`; the manager/startup working-tree hashes and actual verification are recorded in [the evidence report](../../bench_kit/results/parser-v3-release-20261104/RESULTS.md).

The local release asset folder is `dist/parser-v3-20261104/`. The URL in the manifest is the planned GitHub Release location and is not yet published. No Git tag, commit, push or upload was performed.

This verification does not constitute a new full retrieval-quality benchmark or generated-answer evaluation. The thirty live audit prompts were not run, and no Claude calls were made. Installation, integrity checking, activation and rollback require no model provider. Publishing remains a separate action.
