# Local audit workbench (UI)

A browser front end around the existing, unchanged `Agent(ClaudeJSON, HybridClient, ProvenanceRegistry)`. It is a **local, single-user tool**: no accounts, no tenants, no authentication, no cloud deployment, and no production claim. The planner, critic, retrieval, ranking and extractive-synthesis code in `py/agent/` is not modified.

**Status:** implemented and verified locally on November 2, 2026: 167 application tests, real Chrome 139 checks on three frozen scenarios against the real services, a 20-way admission/cancellation burst, security probes and a real active-audit shutdown. These are source/structural checks, not answer accuracy, human gold or load latency. Full evidence and the preserved harness and launcher corrections: [bench_kit/results/audit-ui-20261102/RESULTS.md](../../bench_kit/results/audit-ui-20261102/RESULTS.md). Not deployed; no authentication or tenancy.

## Contents of the change

| Path | Role |
|---|---|
| `py/audit_web/limits.py` | Every bound in one file (mirrored below) |
| `py/audit_web/manager.py` | Single-slot job manager (`AuditManager`) |
| `py/audit_web/projection.py` | Agent result → browser document; citation/evidence binding; SEC-link trust; size bound |
| `py/audit_web/errors.py` | Sanitized error classification |
| `py/audit_web/app.py` | FastAPI app, `Guard` ASGI middleware, static assets, catalog |
| `py/audit_web/samples.py` | Sample prompts and catalog-derived notes |
| `py/audit_web/runtime.py` | Production wiring (real agent) and bridge health probe |
| `py/audit_web/server.py` | Loopback bind policy, uvicorn ingress bounds |
| `py/audit_web/enrollment.py` | Provenance enrollment (same checks as `scripts/evaluate-expanded-agent.py`) |
| `py/audit_web/__main__.py` | Standalone server against already-running services |
| `scripts/serve-audit-ui.py` | One-command full-stack launcher |
| `ui/index.html`, `ui/app.css`, `ui/app.js`, `ui/favicon.svg` | Static front end: system fonts, no CDN, no inline script/style |
| `py/test_audit_web_*.py`, `py/audit_web_testkit.py` | Tests, fixtures and test doubles (clearly labeled) |

No new runtime dependency: FastAPI, uvicorn, httpx and pydantic are already in `py/requirements.txt`.

## Requirements and offline artifacts

Everything must already exist locally; the launcher downloads nothing (no SEC fetch, no model fetch; `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `GOPROXY=off`).

| Artifact (launcher default) | Purpose |
|---|---|
| `data/sec-scale-v3-evaluation-20261101/` (`chunks.jsonl`, `provenance.json`) | Evaluated corpus and its checksums |
| `indexes/sec-scale-v3-20261101/report.json` | Ingestion report (lineage-checked, source of enrolled filings) |
| `data/sec/provenance.json`, `data/sec/chunks.jsonl` | Original four-filing provenance and corpus |
| `indexes/sparse-sec-scale-v3-20261101` | Sparse index for the Go bridge |
| `indexes/vector-sec-scale-v3-pq128-20261101` | PQ128 vector index for the sidecar |
| `models/reranker-onnx-fp32-optimized-20261031` | ONNX cross-encoder |
| `models/hf-cache`, `data/phase2-models.json`, `models/runtime` | Pinned encoder/reranker snapshots and Python packages, as for the other scripts |
| Go toolchain | Builds the bridge into the ignored work directory (`go build`; no module download) |
| Claude Code CLI, signed in (`claude auth login`) | Structured generation, as for `py/agent/state_machine.py` |

The Claude CLI is the only component that talks to a remote service, exactly as in the existing agent.

## Launch

```bash
python3 scripts/serve-audit-ui.py                # UI on http://127.0.0.1:8766/
python3 scripts/serve-audit-ui.py --port 9000
python3 scripts/serve-audit-ui.py --check-only   # verify artifacts + provenance enrollment, start nothing
```

The launcher:

1. Validates the bind (loopback only) and that every artifact exists.
2. **Enrolls provenance** with the same checks as `scripts/evaluate-expanded-agent.py`: ingestion report and corpus hashes against `provenance.json`, original-corpus hash against the baseline manifest, ingestion-report lineage, and the identical trusted-source list. It reuses `bench_kit/agent_suite.build_trusted_sources` and `check_report_lineage` rather than re-implementing a weaker check (`py/audit_web/enrollment.py` is the only product file that touches benchmark helpers).
3. Creates a fresh run directory `data/audit-ui/run-<UTC>-<pid>/` (`data/` is git-ignored). It refuses a symlinked `data/` or `data/audit-ui/`, a directory owned by someone else, group/world-writable permissions (where the filesystem keeps modes; exFAT cannot) and any unexpected pre-existing file or symlink in `data/audit-ui/`. The enrolled manifest, logs (`sidecar.log`, `bridge.log`, rotating `audit-ui.log`), `run-status.json` (`status` is `stopped` with `stop_reason` `interrupt` for Ctrl-C or `SIGTERM` for the launcher's own SIGTERM, exit 130/143; any startup/server error or other exit is `failed`; `owned_processes_stopped` is recorded separately after the cleanup in `finally`) and the built `bridge` binary live there; the binary is deleted on exit. Nothing here is committed.
4. Builds the bridge, then starts the sidecar (4 inference workers × 2 threads, dense batch size 8, trigger 8, wait 2 ms, rerank batch size 1, ONNX reranker) and the Go bridge (admission 8) on free **loopback** ports, each in its own process group.
5. Waits (default 180 s) until both services report the enrolled corpus fingerprint, then builds the one shared disk-backed registry and starts the UI server. It prints `ready at http://127.0.0.1:8766/` only after uvicorn has actually started. `--browser` opens a tab; the default is `--no-browser`.
6. On Ctrl-C, SIGTERM or any startup failure: cancels and awaits the running audit (agent task and Claude process groups), closes the HTTP clients and registry, then stops only the process groups it started (`SIGTERM`, then `SIGKILL` after 15 s). It never signals processes by name or any process it did not create.

Options: `--host` (loopback only), `--port` (1024–65535, default 8766), `--tunnel-local-port`, `--model` (Claude alias, default `haiku`), `--job-timeout` (≤600 s), `--model-timeout` (≤180 s), `--startup-timeout` (finite, 1–600 s, default 180), `--corpus-dir`, `--ingest-report`, `--original-provenance`, `--sparse-index`, `--vector-index`, `--reranker-model`.

Startup hashes the corpus twice (enrollment and the registry's own check) and indexes passage offsets in a temporary SQLite file; expect some delay before the URL is printed.

### Standalone server

If the retrieval services are already running (for example under `scripts/evaluate-expanded-agent.py`-style supervision), the UI alone can be started:

```bash
# Set these to the enrolled manifest and the bridge URL of the services that are already running:
MANIFEST="data/audit-ui/run-EXAMPLE/agent-provenance.json"   # replace EXAMPLE with the real run directory name
BACKEND="http://127.0.0.1:8765"                              # replace with the running bridge's loopback URL
PYTHONPATH=models/runtime:py python3 -m audit_web \
  --corpus data/sec-scale-v3-evaluation-20261101/chunks.jsonl --provenance "$MANIFEST" --backend "$BACKEND"
```

Run it from the repository root.

`--provenance` must be an enrolled manifest of the form written by the launcher (`corpus_sha256`, `sources`, `source_manifests`). The server starts no services and verifies the bridge's corpus fingerprint against that manifest.

### Access from a Mac (SSH tunnel)

The server never listens beyond loopback, so reach it through a tunnel from the machine running a browser:

```bash
ssh -N -L 8766:127.0.0.1:8766 USER@LINUX_HOST     # then open http://127.0.0.1:8766/ on the Mac
```

Use the **same local port** as `--port`; the `Host` allowlist only accepts `127.0.0.1:<port>` and `localhost:<port>`. To use a different local port (for example `-L 9000:127.0.0.1:8766`), start the launcher with `--tunnel-local-port 9000` and browse `http://127.0.0.1:9000/`.

## Exact limits

| Limit | Value | Enforced in |
|---|---|---|
| Active audits | **1 globally**; no pending queue; extra POST → `429` + `Retry-After: 5` | `AuditManager.submit` (no `await`, so atomic on the event loop) |
| Finished jobs retained | **8 hard cap** (completed/failed/cancelled); each is available for **30 min** after finishing, enforced when jobs are accessed or pruned (not guaranteed physical erasure at 30 minutes) | `AuditManager._prune` |
| Prompt | **8192 UTF-8 bytes**, non-blank, no control characters | `POST /api/audits` |
| Request body | **16384 bytes**, checked from `Content-Length` and again while streaming (chunked bodies stop at the bound) | `Guard`, `read_limited_body` |
| Serialized result | **1 MiB** per job (explicit `result_too_large` failure, never a truncated result); history ≤ 8 MiB total. A normal real result is expected to be on the order of tens of KB; this is an expectation, not a measurement | `project_result`, `AuditManager` |
| Passages stored per result | each retrieved-and-verified passage once (≤ 3 subqueries × 3 attempts × ≤ 5 passages per search context), only those the agent selected into a hop's context | `project_result` |
| Job runtime | configurable, **≤ 600 s** (default 600) | `AuditManager` |
| One model call | **≤ 180 s** (default 180) | `ClaudeJSON` (existing) |
| Model concurrency | **2** | `ClaudeJSON` (existing default) |
| Agent | ≤ 3 subqueries, ≤ 2 retries, ≤ 5 passages per search context, 3 concurrent retrievals | `Agent` (unchanged) |
| Catalog | filings only (never passages), bounded to 10 000 entries | `ProvenanceRegistry`, `build_catalog` |
| HTTP | uvicorn `limit_concurrency` 32, backlog 64, header block ≤ 16 KiB, keep-alive 5 s | `server.uvicorn_config` |
| Shutdown cleanup warning interval | 45 s. This is a **warning deadline, not a bound**: shutdown logs a warning every interval and keeps waiting until the agent task and its model process groups have really finished (the slot, backend and registry stay held, new audits are refused). An outer supervisor's SIGKILL is the only hard stop | `AuditManager.shutdown` |
| Browser request timeout | 15 s per request (`AbortController`, timer cleared on completion); `POST` is never retried automatically | `ui/app.js` |
| Health probe | streams `/health` with an incremental 64 KiB limit and abandons larger bodies; at most 2 connections | `BridgeProbe` |

Browser result association: every result and the status card show the **audited question** (the job's own prompt), independent of the editable question box; opening history never touches the draft, and starting or following a different job clears the previous job's results at once. Each job switch invalidates in-flight status requests (aborted, and ignored if they still arrive), so a slow response can never repaint a job the user has left; status polling is one request at a time. If a submit fails ambiguously (timeout or network error) the UI refreshes Recent audits and asks the user to check them before submitting again.

Cancellation: `DELETE` marks the job `cancel_requested` (it stays `running`) and cancels the agent task. The slot is released only after the task and its Claude process groups (`ClaudeJSON` kills them in its own `finally`) have fully finished; until then a new POST gets `429`. Server shutdown does the same and then closes the HTTP backend and registry. A cancelled or failed job never carries a result.

## API

All JSON; every response carries `Cache-Control: no-store` and the security headers below. Job IDs are random UUID4s; unknown, malformed or expired IDs return `404`.

| Method and path | Result |
|---|---|
| `GET /api/health` | `200` ready / `503` unavailable. Verifies the bridge's corpus fingerprint; reports state codes (`ready`, `unreachable`, `unhealthy`, `fingerprint_mismatch`), whether an audit is active and the limits. No paths or secrets |
| `GET /api/catalog` | Enrolled filings (ticker, filing year, form, dashed accession, fiscal period end from the cover page, section labels, SEC URL) and the example prompts with notes computed from the actual catalog |
| `POST /api/audits` `{"prompt": "..."}` | `202` `{id, status, status_url}`; `400` bad JSON; `413` body too large; `415` content type; `422` bad/empty/over-long prompt; `429` busy; `503` backend not ready (fingerprint re-verified before admission) or shutting down |
| `GET /api/audits` | Active job then up to 8 finished ones, summaries only (no results) |
| `GET /api/audits/{id}` | Status, elapsed seconds, `cancel_requested`, sanitized `error`, and, only for `completed`, the `result` document |
| `DELETE /api/audits/{id}` | `202` cancellation requested (still `running`), `200` if already finished, `404` unknown |

Statuses are `pending`, `running`, `completed`, `failed`, `cancelled`; the UI shows exactly these and no other progress stages. `pending` lasts only until the job's supervisor starts.

The result document (serialized once; stored only as bytes) contains: `excerpts` (quote, UTF-16 highlight span, tag, chunk ID, accession, SEC URL, company/year/form/section), `gaps` (the agent's own flags), `plan` (goal, search wording, immutable filters), `hops` (score, accepted, rationale, proposed rewrite, terminal reason, carried/fresh context IDs), `evidence` (each passage once, full text) and `diagnostics` (states, retrieval calls, retries, model-call rows). The agent's Markdown is not forwarded.

### Failure handling

Exceptions are mapped to `{code, message, hint}` (`model_timeout`, `model_unavailable`, `retrieval_unavailable`, `retrieval_timeout`, `invalid_model_output`, `evidence_rejected`, `result_rejected`, `result_too_large`, `job_timeout`, `internal_error`). No exception text, path, traceback or credential reaches the browser; the full exception is written to the local log (`audit-ui.log`). A projection failure (a citation that does not bind to accepted evidence in the same result, a quote that is not verbatim, a non-`www.sec.gov/Archives/` link) fails the job instead of showing a partial result.

## Security model (loopback, single user)

- Bind: `127.0.0.1` or `::1` only (`localhost` → `127.0.0.1`); `0.0.0.0`, `::`, LAN addresses and arbitrary names are refused at launch.
- `Host` must equal `127.0.0.1:<port>` or `localhost:<port>` (plus any `--tunnel-local-port`); anything else is `403` (DNS rebinding). A present `Origin` must equal `http://<that host>`. Duplicate security-relevant headers are `400`.
- Mutations (`POST`, `DELETE`) need `X-Requested-With: finfilings-ui` (a custom header a cross-origin page cannot send without a CORS preflight, and no CORS is ever granted), `Sec-Fetch-Site` of `same-origin` when present, and for `POST` `Content-Type: application/json`.
- No CORS headers; `OPTIONS` is `405`.
- Static assets are four fixed in-memory routes (`/`, `/app.css`, `/app.js`, `/favicon.svg`); there is no file or path parameter API, so traversal is not reachable. `/docs`, `/redoc` and `/openapi.json` are disabled.
- CSP: `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'` — external JS/CSS only, no inline script or style, no `unsafe-eval`. Also `nosniff`, `no-referrer`, `X-Frame-Options: DENY`, COOP/CORP same-origin.
- The front end renders every query, passage, flag, rationale and model string with `textContent`/text nodes (no `innerHTML`, no Markdown). SEC links are created only if the URL is exactly `https://www.sec.gov/Archives/...` without credentials, port, query or fragment (checked in the server and again in the browser), with `rel="noopener noreferrer"`.
- Nothing is stored in `localStorage`/`sessionStorage`/cookies. Results exist in server memory (bounded above) and in the page. "Download result as JSON" builds a local `Blob` from the already-fetched result (≤ 1 MiB); no endpoint exists for it.
- No analytics, telemetry, third-party fonts or external requests.

Trust boundary: any local process or user who can reach `127.0.0.1:<port>` (including through an SSH tunnel you opened) can run audits and read results. The guards above protect against *browsers on other origins*, not against other local users. There is deliberately no authentication, multi-tenancy or isolation, and none is claimed.

## Using the UI

Enter a question (or press an example button, which only fills the box), press **Run audit**, and watch the real status and elapsed time; **Cancel audit** requests cancellation. Completed audits show verified excerpts (company, filing year, form, section, inline citation tag, passage ID, accession, sec.gov link); the tag opens the full passage with the quoted text highlighted. Evidence gaps are listed separately. "How the audit searched" shows each topic's goal, fixed filing scope, attempts with relevance scores and rationale, and which passages were carried over versus newly retrieved. Technical diagnostics are under "Run details". Empty, busy (`429`), unavailable-backend and failed states have explicit messages. A second browser tab sees the running audit and cannot start another.

Example prompts: two are the recorded regression prompts `audit03` and `audit01` (a test asserts they are identical to `bench_kit/eval_agent.py`); the third asks about NVDA filing year 2023. Each shows a note derived from the live catalog (for the launcher's default corpus, NVDA 2023 is not enrolled). No sample carries an expected answer.

## Tests

```bash
cd py && PYTHONPATH=../models/runtime:. python3 -m unittest test_audit_web_manager test_audit_web_projection test_audit_web_app test_audit_web_ui_static test_audit_web_ui_flow
```

`py/test_audit_web_ui_flow.py` runs `py/ui_flow/harness.js` under Node: a minimal fake DOM and scripted `fetch` drive the real `ui/app.js` through result/job association, stale-response races, the 15 s timeout with no POST retry, history refresh after an ambiguous submit, cancel availability and non-overlapping polling. It is not a browser; layout, focus and real rendering are not covered.

They use scripted agents/models/backends and a synthetic fixture corpus (`py/audit_web_testkit.py`, TEST DOUBLES ONLY); one end-to-end class runs the real `Agent` and `ProvenanceRegistry` over that fixture with a scripted model and backend. Covered: single-slot admission (25 simultaneous submits, 20 simultaneous HTTP POSTs → exactly one accepted), pending/running/completed/failed/cancelled consistency, no slot release before cancel cleanup, shutdown ordering, job timeout, history count/TTL/result-byte bounds, oversized result, sanitized errors for each failure class, body limits (Content-Length, chunked, raw ASGI streaming), prompt validation, Host/Origin/CSRF/content-type/duplicate-header attacks, no CORS, security headers on rejections, static routes and traversal, SEC URL trust, citation/evidence binding (including unknown and unaccepted evidence), UTF-16 highlight offsets, real no-evidence flags, hostile strings remaining inert data, static HTML/CSS/JS rules (no inline script/handler, no `innerHTML`/`eval`/storage, element IDs exist), bind policy, uvicorn bounds, launcher work-directory safety, process-group-only termination, and enrollment checksum refusal. Timings in these tests are not benchmarks. They do not test model quality (the agent is unchanged) and do not exercise a real browser.

## Known limitations and unresolved requirements

- Verified live (see the report above): real-browser rendering of the three primary scenarios at desktop and mobile sizes, live audits including an unavailable exact section and a stalled-rewrite stop, real cancellation, a 20-way submit burst, security probes and active-audit shutdown. Still not covered: browsers other than Chrome 139, assistive-technology testing, sustained or multi-client load (this is a single-user tool; no p50/p99 exists), and answer accuracy.
- One audit at a time, by design; the UI never queues.
- Job state and results live in server memory only and vanish on restart.
- Elapsed time shown between polls is interpolated in the browser; the server's value replaces it every second.
- `Sec-Fetch-Site` and `Origin` are enforced only when the browser sends them; the custom header and the Host allowlist are the primary guards. A non-browser local client can ignore all of them.
- On exFAT (this repository's drive) POSIX modes are not enforced, so the launcher's private-permission checks cannot protect `data/audit-ui/` there.
- Run-directory files (manifest, logs, `bridge`) are created with owner-only modes where the filesystem supports them, but on exFAT no confidentiality is claimed from `chmod`; logs may contain audit questions and error details, stay local and must not be published.
- Sidecar and bridge stdout logs under `data/audit-ui/run-*/` are not rotated, and old run directories are not pruned.
- The registry's catalog reports the cover-page fiscal period text as extracted (for example `September 30 , 2023`); it is displayed as recorded.
- Result quality is whatever the unchanged agent produces; see the existing validation reports. This UI adds no accuracy claim.
