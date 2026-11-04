# Real parser-v3 release verification — 2026-11-04

**Passed: real package preparation, immutable installation/activation, managed preflight, native hybrid retrieval and post-startup payload verification. Unpublished; no commits or pushes; no model-provider calls.**

## Safe application

The SSD was accessible on the host at `/dev/sda1`, mounted at `/media/grv22/Extreme SSD2`. The sandbox retained an older mount view; operations on the reattached drive used host access. The saved delivery's 28 files and patch passed their recorded SHA-256 checks.

The patch applied after manually merging the sole overlap in `scripts/serve-audit-ui.py`. All provider-related source lines were preserved exactly. A before/after snapshot verified **404 unrelated existing local files unchanged**. The complete original dirty tree, affected originals and hashes were backed up under ignored `.cache/artifact-manager-preapply-20261104/`. Nothing was staged. See [merge-verification.json](merge-verification.json).

## Real payload and startup

| Check | Actual result | Evidence |
|---|---|---|
| Real tar validation and release preparation | Passed every file size/hash, archive inventory/path safety, corpus/report/raw-source identities, sparse/vector fingerprints, vector count/config and pinned model checks | [prepare.log](prepare.log), [readiness](release-readiness.json) |
| Immutable local install and activation | `parser-v3-20261104` installed and selected; original source artifacts unchanged | [install.log](install.log), [store-status.json](store-status.json) |
| Managed preflight | **152 filings**, corpus fingerprint matches the pinned recipe | [preflight.log](preflight.log) |
| Native hybrid search | **49,147 passages**; BM25 + PQ8 + RRF + FP32 ONNX reranking returned **10** passages | [raw output](retrieval.log), [summary](retrieval-summary.json) |
| Smoke ranking comparison | All ten IDs match the prior verified same-query result, in identical order | [retrieval-summary.json](retrieval-summary.json) |
| Bounds | **4 inference workers × 2 threads**, bridge admission 8, existing bounded dense batching | [health](retrieval-summary.json) |
| Provider/transport guard | No provider/runtime constructed; remote/model traffic and unexpected child commands forbidden; **zero prohibited operations** | [driver](verification-driver.py), [retrieval.log](retrieval.log) |
| Post-startup immutability | Full installed payload rehashed successfully | [payload-verification.json](payload-verification.json) |
| Cleanup | Every owned service stopped; mutable run files are outside the payload | [cleanup.json](cleanup.json) |

The guard executes the real packaged startup selector and launcher in the guarded Python process, while allowing the actual Go build/bridge and Python retrieval sidecar child processes. This retains process, artifact, enrollment and native retrieval behavior while failing any generation wiring. No alternative model provider was exercised.

## Regression checks

- **68 offline tests passed**, no failures/errors/skips or prohibited operations: [summary](offline-verification.json), [log](offline-tests.log).
- **9 loopback transport tests passed**, single-worker fixtures and all fixture ports closed: [summary](download-verification.json), [log](download-tests.log).
- Live checkout `GOMAXPROCS=2 GOPROXY=off GOTOOLCHAIN=local go test -count=1 ./...` passed: [log](go-tests.log).
- actionlint v1.7.7 validated both workflows, exit 0: [log](workflow-lint.log).

## Release identities and scope

The ready metadata is copied into `releases/parser-v3-20261104/`; the complete staged release asset directory is ignored `dist/parser-v3-20261104/`. A publisher-facing `sha256sum -c SHA256SUMS` passed for both the manifest and the canonical bundle filename: [asset-checksums.log](asset-checksums.log). [release-readiness.json](release-readiness.json) records exact corpus, manifest and tar hashes. No remote asset has been uploaded, and the manifest's public URL remains a planned publishing location.

This validates package identity and a functional real-data retrieval smoke test, not a new nDCG/load benchmark or a thirty-prompt audit evaluation. No generated financial claims were produced; citation, retry and evidence-gap quality were not re-evaluated. Historical corpus and model identity pins remain unchanged. The base Git commit remains `31d0e776e519a13a4acb5aeee0276004acc9837b`; verification used the uncommitted working-tree implementation recorded by `code-sha256.json`.

## Publication candidate recheck

The isolated publication candidate excludes unrelated local provider/agent changes. It passed all **68 offline + 9 loopback tests**, Go tests and workflow lint again. A new real managed retrieval against the installed payload returned the same ten IDs in the same order, with four workers × two threads, zero provider construction and zero prohibited operations. See [publication-verification.json](publication-verification.json), [offline output](publication-offline.log), [download output](publication-downloads.log), [Go output](publication-go.log) and [actual native retrieval](publication-retrieval.log).

The included verification driver supports `FINFILINGS_VERIFY_STORE` so the clean source checkout can use a separately installed payload. It uses the published Claude-only wiring and denies provider construction. The original `code-sha256.json` records the earlier applied working-tree test; `publication-code-sha256.json` records the exact candidate rechecked for publication. The commit contains code and metadata only; the large tar remains a separate unpublished asset.
