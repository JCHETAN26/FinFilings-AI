# Reproducible local setup

This workflow installs an isolated CPU runtime, restores a checksummed parser-v3 artifact snapshot, validates provenance, and starts the workbench. Setup and retrieval verification make no Claude calls. Asking an audit question still requires an authenticated Claude Code CLI; that optional step is separate from installation.

## Supported target

- Linux x86_64, glibc 2.28 or newer, Python **3.10** with `venv`, Go **1.24 or newer** on `PATH`.
- CPU execution; no CUDA installation is needed. The tested dependency lock uses PyTorch **2.6.0+cpu**, BGE-small's pinned weights, and the existing FP32 ONNX reranker.
- Allow roughly 5 GB free for the environment, bundle and restored artifacts; package caches and filesystem allocation overhead need additional space. This was tested on a Ryzen 7 5800H with about 20 GiB RAM. Minimum RAM and other platforms have not been validated.
- On Ubuntu 22.04, Python prerequisites can be installed with `sudo apt-get install python3.10-venv libgomp1`. Install a supported Go distribution separately.

The lock is specific to Linux x86_64/CPython 3.10. Other Python versions, macOS and Windows need separate locks and verification. The repository's older `models/runtime` overlay and globally installed Python packages are not used by this workflow.

## 1. Prepare the artifact bundle on the existing machine

From a checkout containing these setup changes:

```bash
python3 scripts/setup-artifacts.py export /path/to/finfilings-parser-v3.tar
sha256sum /path/to/finfilings-parser-v3.tar
```

Transfer the bundle and its SHA-256 to the fresh machine. It contains the current 49,147-passage corpus, 152 retained SEC source filings and their ingestion report, original source provenance, mmap sparse index, PQ128/PQ8 vector index, pinned BGE and cross-encoder snapshots, and optimized FP32 ONNX reranker. Model paths are rewritten relative to the exported lock, so the original machine's absolute paths are unnecessary. All payload files have SHA-256 and size entries in `bundle-manifest.json`.

The bundle contains no `.env`, provider credentials, generation results, Python environment, or `models/runtime`. It is a local artifact transfer, **not an artifact download hosted by this repository**. Keep the bundle with the matching code snapshot. Only restore bundles from a trusted source; internal hashes detect corruption, while the independently transferred outer SHA-256 identifies the bundle.

This restores the exact existing snapshot. It does not redownload EDGAR, retrain PQ, or claim that mutable SEC responses will reproduce historical bytes. Building a new corpus remains a separate ingestion/migration workflow; a changed corpus needs new provenance, indexes and audit-suite pins.

## 2. Install dependencies on the fresh machine

From the repository root:

```bash
python3.10 scripts/bootstrap.py --venv .venv
source .venv/bin/activate
```

The installer refuses to change an existing environment. It installs every direct and transitive runtime/evaluation dependency from an exact wheel URL with a required SHA-256, then runs `pip check`. It never installs into the user/global Python environment. Internet access is needed for the initial wheel downloads; serving uses local model files only.

For an exFAT checkout, put the environment on a filesystem supporting symbolic links, for example:

```bash
python3.10 scripts/bootstrap.py --venv /tmp/finfilings-venv
source /tmp/finfilings-venv/bin/activate
```

`/tmp` is temporary; use a persistent Linux filesystem for a durable environment. Interrupted installations remain in their own directory; choose another fresh path when retrying.

## 3. Restore and validate artifacts

```bash
sha256sum /path/to/finfilings-parser-v3.tar
python scripts/setup-artifacts.py restore /path/to/finfilings-parser-v3.tar
python scripts/start.py --check-only
python scripts/start.py --verify-retrieval
```

Compare the first hash with the source machine's value. Restore validates every archive entry before writing, rejects traversal, links, duplicate entries and overwrites, and writes only regular files. Restore into a fresh checkout; an interrupted filesystem write may leave files that require a new destination. Enrollment independently checks the corpus/report hashes and retained raw filing identities. The retrieval smoke check builds the Go bridge, starts the actual bounded services, executes a real reranked hybrid query, reports zero generation calls, and stops all processes it owns. It does not construct a generation provider.

## 4. Start the UI

```bash
python scripts/start.py --port 8766
```

Open `http://127.0.0.1:8766/`. Stop with Ctrl-C. Startup uses four inference workers with two threads each, bounded sidecar admission, bridge admission eight, and one active UI audit. Models load offline. No unrelated service is stopped. Each run writes logs and cleanup status under ignored `data/audit-ui/`.

The page can start without Claude authentication. Before submitting an audit, install/authenticate the Claude Code CLI separately. No key belongs in an artifact bundle or source file. Without a working provider, retrieval verification still works; a live generated audit has not been validated by this setup run.

## Verification evidence

See [the local setup results](../../bench_kit/results/setup-ci-publish-20261104/RESULTS.md). Verification used a new environment with user-site packages disabled and a separate checkout path containing spaces. The original corpus/model paths were not used for inference. This is a startup/retrieval check, not a replacement for the full retrieval-quality or 30-audit benchmarks.

## Continuous integration

[Setup and startup CI](../../.github/workflows/setup.yml) runs on pushes, pull requests and manual dispatches. A single Ubuntu 24.04/Python 3.10 job creates a **new** environment with `scripts/bootstrap.py` on every run; it never restores a preinstalled virtual environment. Each package wheel remains hash-locked. The job also builds the real Go startup bridge with dependency downloads disabled. The existing Go test/race/vet workflow remains separate.

The offline check runner copies source into a fresh temporary checkout whose path includes spaces. It copies no `data/`, `indexes/`, `models/`, result directories or user environment. Its explicit test allowlist checks artifact corruption/restore/re-export, relative model paths, startup interpreter/environment isolation, preflight exit before provider construction, loopback bind policy, process cleanup, UI assets and lifespan shutdown, storage failure recovery, and native CPU FAISS/ONNX operation. Small synthetic fixtures are unit-test inputs, not evidence of SEC retrieval quality.

Claude generation is replaced with fail-fast guards. Python audit hooks reject outbound connections, DNS lookups and unexpected subprocesses; attempted operations fail the overall run even if a dependency catches the exception. The only allowed bind is urllib3's ephemeral IPv6-loopback availability probe. No SEC downloads, pretrained model downloads, Claude calls or credentials are needed. Numerical libraries are capped at two threads (OpenBLAS at one), Go at two logical CPUs, and the job has a 20-minute limit. Superseded runs for a branch are cancelled. Logs and a machine-readable summary are retained for seven days, including on failure.

Run the same checks locally from an activated setup environment:

```bash
python scripts/check-setup-ci.py --report .cache/setup-ci-verification.json
GOMAXPROCS=2 GOPROXY=off GOTOOLCHAIN=local GOFLAGS=-mod=readonly go build -o .cache/finfilings-ci-bridge ./cmd/finfilings
```

The small CI fixtures do **not** replace `python scripts/start.py --verify-retrieval` with the separately transferred real corpus/model bundle, nor the full retrieval or audit evaluations. Hosted CI has no bundled corpus or model weights. See [local CI verification](../../bench_kit/results/setup-ci-publish-20261104/RESULTS.md) for the commands and results checked before publication; a hosted GitHub run requires these changes to be committed and pushed.
