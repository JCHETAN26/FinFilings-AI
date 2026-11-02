#!/usr/bin/env python3
"""One-command local FinFilings audit workbench: sidecar + Go bridge + loopback web UI.

Enrolls provenance from the existing ingestion reports with the same checks as scripts/evaluate-expanded-agent.py,
builds the bridge into the ignored work directory data/audit-ui/, starts the 4x2 sidecar and the bridge on loopback
ports, waits for both corpus fingerprints, then serves the UI. All owned process groups are stopped on SIGINT, SIGTERM
and any startup failure. No SEC download, no model fetch, nothing listens beyond loopback, no unrelated process is touched.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = dict(corpus_dir='data/sec-scale-v3-evaluation-20261101', ingest_report='indexes/sec-scale-v3-20261101/report.json',
                original_provenance='data/sec/provenance.json', sparse_index='indexes/sparse-sec-scale-v3-20261101',
                vector_index='indexes/vector-sec-scale-v3-pq128-20261101', reranker_model='models/reranker-onnx-fp32-optimized-20261031')


class Terminated(SystemExit):
    """Raised only by this launcher's own SIGTERM handler (conventional exit status 143)."""


def classify_exit(exc: BaseException) -> tuple[str, str | None, int | None]:
    """(status, stop_reason, exit_code) for an exception leaving main(). Only an operator stop is 'stopped': Ctrl-C or this
    launcher's own SIGTERM handler. Everything else (startup/server errors, any other SystemExit) is 'failed'."""
    if isinstance(exc, KeyboardInterrupt):
        return 'stopped', 'interrupt', 130
    if isinstance(exc, Terminated):
        return 'stopped', 'SIGTERM', 143
    return 'failed', None, None


def startup_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a number')
    if not math.isfinite(value) or not 1 <= value <= 600:
        raise argparse.ArgumentTypeError('must be a finite number of seconds in 1..600')
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name, default in DEFAULTS.items():
        parser.add_argument('--' + name.replace('_', '-'), type=Path, default=Path(default), help=f'default: {default}')
    parser.add_argument('--host', default='127.0.0.1', help='loopback only (127.0.0.1, localhost or ::1)')
    parser.add_argument('--port', type=int, default=8766, help='UI port (default 8766)')
    parser.add_argument('--tunnel-local-port', type=int, action='append', default=[],
                        help='local port of a trusted SSH tunnel when it differs from --port (adds it to the Host allowlist)')
    parser.add_argument('--model', default='haiku', help='Claude CLI model alias (default haiku)')
    parser.add_argument('--agent-mode', choices=('fast', 'legacy'), default='fast', help='fast (default): critic-selected quotes + host formatting; legacy: separate synthesis call')
    parser.add_argument('--job-timeout', type=float, default=600, help='seconds per audit, max 600')
    parser.add_argument('--model-timeout', type=float, default=180, help='seconds per model call, max 180')
    parser.add_argument('--startup-timeout', type=startup_seconds, default=180, help='seconds to wait for the retrieval services, 1..600 (default 180)')
    parser.add_argument('--browser', action=argparse.BooleanOptionalAction, default=False, help='open a browser tab once ready (default: --no-browser)')
    parser.add_argument('--check-only', action='store_true', help='validate artifacts and provenance enrollment, then exit without starting anything')
    return parser


def prepare_work_dir() -> Path:
    """data/audit-ui/ (git-ignored) and a fresh per-run directory. Refuses symlinks, foreign owners and unsafe pre-existing files."""
    data = ROOT / 'data'
    if data.is_symlink() or not data.is_dir():
        raise SystemExit('refusing: data/ must be a real directory')
    base = data / 'audit-ui'
    if base.is_symlink():
        raise SystemExit('refusing: data/audit-ui is a symlink')
    if base.exists():
        info = base.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise SystemExit('refusing: data/audit-ui is not a directory owned by you or is group/world-writable')
        for entry in base.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                raise SystemExit(f'refusing: unexpected pre-existing file in data/audit-ui ({entry.name}); remove it yourself')
    else:
        base.mkdir(mode=0o700)
    run = base / f'run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{os.getpid()}'
    run.mkdir(mode=0o700)  # exist_ok=False: never reuse a directory
    return run


def private_cache(name: str) -> Path:
    path = Path('/tmp') / f'{name}-{os.getuid()}'
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise SystemExit(f'refusing unsafe cache directory {path}')
    return path


def free_port(avoid=()) -> int:
    while True:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        if port not in avoid:
            return port


def service_health(base: str) -> dict:
    with urllib.request.urlopen(base + '/health', timeout=3) as response:
        return json.load(response)


def stop(process: subprocess.Popen) -> None:
    """Stop a process group WE started (pid == pgid via start_new_session); nothing else is ever signalled."""
    if process.poll() is not None:
        return
    try:
        owned = os.getpgid(process.pid) == process.pid
    except ProcessLookupError:
        return
    send = (lambda sig: os.killpg(process.pid, sig)) if owned else process.send_signal
    try:
        send(signal.SIGTERM)
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        pass


def require_artifacts(args) -> None:
    missing = []
    for path in (args.corpus_dir / 'chunks.jsonl', args.corpus_dir / 'provenance.json', args.ingest_report, args.original_provenance,
                 args.sparse_index, args.vector_index, args.reranker_model / 'model.onnx', Path('data/sec/chunks.jsonl')):
        if not path.exists():
            missing.append(str(path))
    if missing:
        raise SystemExit('missing required local artifacts (nothing is downloaded):\n  ' + '\n  '.join(missing))


def configure_logging(run: Path) -> None:
    handler = RotatingFileHandler(run / 'audit-ui.log', maxBytes=5 << 20, backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger('audit_web').addHandler(logging.StreamHandler(sys.stderr))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    os.chdir(ROOT)
    os.umask(0o077)
    runtime_dir = ROOT / 'models/runtime'
    sys.path[:0] = [str(runtime_dir), str(ROOT / 'py')]
    from audit_web import limits
    from audit_web.enrollment import enroll
    from audit_web.runtime import build_runtime
    from audit_web.server import serve, validate_bind
    if not 1 <= args.job_timeout <= limits.JOB_TIMEOUT_MAX_SECONDS or not 1 <= args.model_timeout <= limits.MODEL_TIMEOUT_MAX_SECONDS:
        raise SystemExit('--job-timeout must be 1..600 and --model-timeout 1..180 seconds')
    try:
        host = validate_bind(args.host, args.port, tuple(args.tunnel_local_port))
    except ValueError as exc:
        raise SystemExit(str(exc))
    require_artifacts(args)
    print('Verifying checksums and provenance enrollment (hashes the corpus; this can take a moment)...', flush=True)
    enrolled = enroll(root=ROOT, corpus_dir=args.corpus_dir, ingest_report=args.ingest_report, original_provenance=args.original_provenance)
    print(f"Enrolled {enrolled['filings']} filings; corpus sha256 {enrolled['corpus_sha256'][:16]}...", flush=True)
    if args.check_only:
        return 0
    run = prepare_work_dir()
    configure_logging(run)
    manifest = run / 'agent-provenance.json'
    fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(enrolled['manifest'], stream, indent=2)
    cache = private_cache('finfilings-go-cache')
    env = dict(os.environ, HF_HOME=str(ROOT / 'models/hf-cache'), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1',
               PYTHONPATH=os.pathsep.join([str(runtime_dir), str(ROOT / 'py')]), OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='2', NUMBA_NUM_THREADS='4',
               GOCACHE=str(cache), GOPROXY='off', GOFLAGS='-mod=readonly', GOTOOLCHAIN='local')
    bridge_binary = run / 'bridge'
    processes, logs = [], []
    runtime = None
    def terminate(*_):
        raise Terminated(143)
    signal.signal(signal.SIGTERM, terminate)  # make `finally` cleanup run on termination too
    status = dict(started_utc=datetime.now(timezone.utc).isoformat(), host=host, port=args.port, status='starting')
    try:
        print('Building the Go bridge into the ignored work directory...', flush=True)
        subprocess.run(['go', 'build', '-o', str(bridge_binary), './cmd/finfilings'], env=env, check=True)
        sidecar_port = free_port({args.port})
        bridge_port = free_port({args.port, sidecar_port})
        sidecar, backend = f'http://127.0.0.1:{sidecar_port}', f'http://127.0.0.1:{bridge_port}'
        commands = [
            ('sidecar', [sys.executable, '-u', 'py/service.py', '--index', str(args.vector_index), '--port', str(sidecar_port),
                         '--inference-workers', '4', '--threads', '2', '--reranker-onnx', str(args.reranker_model), '--rerank-batch-size', '1',
                         '--dense-batch-size', '8', '--dense-batch-trigger', '8', '--dense-batch-wait-ms', '2']),
            ('bridge', [str(bridge_binary), '-mode', 'serve', '-index', str(args.sparse_index), '-sidecar', sidecar, '-listen', f'127.0.0.1:{bridge_port}'])]
        for name, command in commands:
            log = (run / (name + '.log')).open('w')
            logs.append(log)
            processes.append(subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
        print('Starting retrieval services (sidecar 4 workers x 2 threads, bridge admission 8)...', flush=True)
        deadline = time.monotonic() + args.startup_timeout
        while True:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError(f'a retrieval service exited during startup; see {run}/sidecar.log and bridge.log')
            try:
                health = [service_health(sidecar), service_health(backend)]
                if any(h.get('corpus_sha256') != enrolled['corpus_sha256'] for h in health):
                    raise RuntimeError('retrieval service corpus fingerprint does not match the enrolled corpus')
                break
            except (OSError, ValueError):
                if time.monotonic() > deadline:
                    raise TimeoutError('retrieval services did not become ready in time')
                time.sleep(0.25)
        runtime = build_runtime(corpus=enrolled['corpus'], provenance=manifest, backend_url=backend, expected_corpus_sha256=enrolled['corpus_sha256'],
                                model=args.model, agent_mode=args.agent_mode, job_timeout=args.job_timeout, model_timeout=args.model_timeout)
        status['status'] = 'serving'
        asyncio.run(serve(runtime, host, args.port, tunnel_ports=tuple(args.tunnel_local_port), open_browser=args.browser))
        status['status'] = 'stopped'
        return 0
    except BaseException as exc:
        outcome, reason, exit_code = classify_exit(exc)
        if outcome == 'stopped':
            status.update(status='stopped', stop_reason=reason)  # expected operator stop; owned groups are verified in `finally`
            return exit_code
        status.update(status='failed', error=type(exc).__name__)
        if not isinstance(exc, SystemExit):
            logging.getLogger('audit_web').exception('launcher failed')
            print(f'Startup failed: {type(exc).__name__}. See logs in {run}', file=sys.stderr)
            return 1
        raise
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # cleanup must finish even if a second signal arrives
        if runtime is not None:
            asyncio.run(runtime.close())
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()
        bridge_binary.unlink(missing_ok=True)
        status.update(finished_utc=datetime.now(timezone.utc).isoformat(), service_exit_codes=[p.returncode for p in processes],
                      owned_processes_stopped=all(p.poll() is not None for p in processes))
        (run / 'run-status.json').write_text(json.dumps(status, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
