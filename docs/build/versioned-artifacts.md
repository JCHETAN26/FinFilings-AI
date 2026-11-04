# Versioned artifact manager

The manager installs immutable corpus/model bundles, verifies their bytes and cross-artifact identities, and selects the version used by the workbench. It uses only Python's standard library and never constructs a model provider. Linux is the supported target (`flock` serializes store operations).

## Parser-v3 candidate

`releases/parser-v3-20261104/recipe.json` pins the previously verified 49,147-passage corpus, 152 SEC filings, parser identity, source commit, and exact BGE/MiniLM revisions and weight hashes. It is a **candidate recipe**, not a published download or a claim of a newly validated live audit run. Full release metadata is generated only after reading and verifying the real bundle.

The real bundle was verified, installed, activated and reverified after a guarded native retrieval smoke check. Generated `release.json` and `SHA256SUMS` are included with this version. The staged payload assets remain under ignored `dist/parser-v3-20261104/`; the public bundle URL is a planned publishing location. See [the release notes](../../releases/parser-v3-20261104/RELEASE_NOTES.md) and [real release verification](../../bench_kit/results/parser-v3-release-20261104/RESULTS.md).

## Prepare the official payload locally

When the real artifact snapshot is available, export the existing portable format if needed:

```bash
python scripts/setup-artifacts.py export /path/to/finfilings-parser-v3-20261104.tar
python scripts/artifacts.py prepare \
  --bundle /path/to/finfilings-parser-v3-20261104.tar \
  --recipe releases/parser-v3-20261104/recipe.json \
  --url https://github.com/JCHETAN26/FinFilings-AI/releases/download/parser-v3-20261104/finfilings-parser-v3-20261104.tar \
  --out dist/parser-v3-20261104
```

The URL is the **planned** publishing location, currently unpublished. Local preparation has completed; rerunning the command requires another fresh output directory. Preparation verifies the entire tar before creating `release.json` and `SHA256SUMS` atomically. It checks:

- Only regular, unique, contained archive paths; bounded archive/member/JSON sizes.
- Every file's size/SHA-256 and the exact file inventory.
- Corpus bytes and line count, ingestion report hash, 152 source identities for this recipe, retained raw bytes and positive Item 1A/7/8 section counts.
- Original SEC provenance and replacement filing metadata.
- Sparse corpus fingerprint, vector corpus/count/PQ128×8/384-dimension metadata and file hashes.
- Local model weight hashes and tokenizer/config presence; pinned embedding revision and FP32 reranker identity/hash.

It rejects a different corpus/model snapshot instead of quietly relabeling it with this version. Parser replay, native index correctness, retrieval quality and generated-answer quality remain separate evaluations. The candidate records that live Claude audits have not been run for this release.

Preparation does not upload anything. The later publishing action must upload the verified bundle and generated metadata together and make the manifest's SHA-256 available through an independently trusted channel. No existing version should be replaced with different bytes.

## Install, verify and activate

The consumer provides `RELEASE_MANIFEST` (a local path or HTTPS URL) and `TRUSTED_MANIFEST_SHA256` from the trusted publisher. Hashing an untrusted downloaded manifest and trusting its own hash is not authentication.

```bash
python scripts/artifacts.py install \
  --manifest "$RELEASE_MANIFEST" --sha256 "$TRUSTED_MANIFEST_SHA256"
python scripts/artifacts.py verify parser-v3-20261104
python scripts/artifacts.py activate parser-v3-20261104
python scripts/start.py --check-only
python scripts/start.py --verify-retrieval
python scripts/start.py --port 8766
```

Use an existing archive with `install --bundle-file /path/to/bundle.tar`; it still must match the pinned bundle hash. `install --activate` combines installation and explicit selection. Installation alone leaves the active version unchanged. A repeated identical installation is idempotent; the same release ID with different metadata is rejected.

Downloads are streamed in 1 MiB buffers over HTTPS, with one connection at a time, at most three attempts and a 30-minute transfer budget. Interrupted transfers keep `.part` bytes; retries validate exact `Content-Range`. Servers ignoring Range cause a clean restart. Full size and SHA-256 are checked before extraction. HTTPS-to-HTTP redirects, encoded responses and oversize downloads are refused. HTTP is allowed only for literal loopback when the explicit fixture flag is supplied. No credentials, shell command execution, SEC discovery or generation calls are involved.

## Activation and rollback

```bash
python scripts/artifacts.py status
python scripts/artifacts.py rollback
python scripts/start.py --check-only
```

The store defaults to ignored `artifacts/`:

```text
artifacts/
  .lock
  downloads/<bundle-sha>.part
  releases/<release-id>/release.json
  releases/<release-id>/receipt.json
  releases/<release-id>/payload/...
  state.json
  runs/run-.../...
```

A nonblocking OS lock permits one store operation at a time. Downloads and extraction never write into an installed version. A fresh staging directory is fully verified before rename, and `state.json` changes via an fsynced temporary file and atomic replacement. Failed installs and failed activations retain the prior selection. Handled extraction errors remove their staging files; a hard-killed process can leave an unselected `.install-*` directory. There is no automatic destructive garbage collection.

Activation rehashes the installed payload, retains the previous version, and updates only the selection. Rollback revalidates that previous version before switching. `status` is informational; it does not imply a new payload verification. Local store ownership is the trust boundary; this is integrity checking, not protection against an attacker who controls the entire store and its receipts.

Startup revalidates the selected payload and resolves artifact/model paths relative to it. Mutable logs, binaries and per-run provenance go into `runs/`, outside immutable payloads. Code checkout `data/` and `setup-profile.json` are unnecessary in managed mode. Existing direct-restore startup remains compatible. Use another store with `artifacts.py --store /path ...` and `start.py --release-store /path ...`.

Activation does not replace an already running service's corpus. Restart the UI after changing versions; the old payload remains available to processes already using it. Corrupted or incomplete releases fail before startup. Power-loss durability beyond the filesystem's atomic rename/fsync behavior and other operating systems have not been validated.

## Tests and CI

```bash
python scripts/check-setup-ci.py --report .cache/offline-verification.json
python scripts/check-artifact-downloads.py --report .cache/download-verification.json
```

The first command runs the explicit offline allowlist with network/generation denied. The second permits only each serial HTTP fixture's registered loopback port and rejects all subprocesses. Its test bytes are explicitly synthetic; no real source/model endpoint is involved. The local setup CI workflow includes both commands. No hosted workflow was dispatched for these uncommitted changes.
