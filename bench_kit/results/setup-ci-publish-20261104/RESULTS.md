# Setup/startup publication verification

This evidence was generated from an isolated checkout of the publication candidate. It includes portable setup, startup checks and the source-enrollment/storage dependencies those checks need. Unrelated local agent and provider changes were excluded.

- **42 offline setup/startup tests passed**, with no failures, errors or skips. The runner used another source-only temporary checkout with spaces in its path. Outbound connections and Claude generation were blocked; no generation calls occurred. [Summary](verification.json), [raw test output](checks.log).
- **Go tests passed** using `GOMAXPROCS=2 GOPROXY=off GOTOOLCHAIN=local GOFLAGS=-mod=readonly go test -count=1 ./...`. [Output](go-tests.log).
- **Real retrieval smoke check passed** from the publication candidate, against the previously restored corpus/model bundle: 152 filings enrolled, 49,147 passages, matching sparse/dense corpus fingerprints, ten reranked results. Inference stayed at four workers × two threads. No model provider was constructed. [Results](retrieval-smoke.json).
- **All owned services stopped** after the smoke check. [Cleanup](cleanup.json).
- `actionlint v1.7.7` and `git diff --check` passed. Implementation hashes are in [code-sha256.json](code-sha256.json).

The environment was the isolated Linux x86_64/Python 3.10 CPU environment from the end-to-end setup verification, with the unchanged hash-locked dependency file. PyTorch is 2.6.0+cpu and ONNX Runtime is 1.23.2. No package pins were changed for publication. Native-library tests use explicitly synthetic unit fixtures; they are not retrieval-quality or performance benchmarks.

Hosted CI is configured to install a fresh environment on every run, execute these offline checks, build the Go bridge and retain logs for seven days. The hosted job does not receive the large real corpus/model bundle. Its status should be read from the GitHub Actions run for the published commit. Live audit generation and the thirty audit prompts were not run in this verification.
