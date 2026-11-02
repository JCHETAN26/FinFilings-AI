"""Standalone API/UI server against already-running services:

    python -m audit_web --corpus data/.../chunks.jsonl --provenance <enrolled provenance.json> --backend http://127.0.0.1:PORT

The provenance file must already be an enrolled manifest (see scripts/serve-audit-ui.py, which builds one from the
ingestion reports). This entry point starts no retrieval services and fetches nothing.
"""
import argparse
import asyncio
import json
import logging
from pathlib import Path
from .runtime import build_runtime
from .server import serve
from . import limits


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--provenance', type=Path, required=True)
    parser.add_argument('--backend', required=True, help='Go bridge URL, e.g. http://127.0.0.1:8765')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--model', default='haiku')
    parser.add_argument('--agent-mode', choices=('fast', 'legacy'), default='fast')
    parser.add_argument('--job-timeout', type=float, default=limits.JOB_TIMEOUT_MAX_SECONDS)
    parser.add_argument('--model-timeout', type=float, default=limits.MODEL_TIMEOUT_MAX_SECONDS)
    parser.add_argument('--tunnel-local-port', type=int, action='append', default=[], help='extra local port a trusted SSH tunnel uses')
    parser.add_argument('--open-browser', action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    expected = json.loads(args.provenance.read_text())['corpus_sha256']
    runtime = build_runtime(corpus=args.corpus, provenance=args.provenance, backend_url=args.backend, expected_corpus_sha256=expected,
                            model=args.model, agent_mode=args.agent_mode, job_timeout=args.job_timeout, model_timeout=args.model_timeout)
    try:
        asyncio.run(serve(runtime, args.host, args.port, tunnel_ports=tuple(args.tunnel_local_port), open_browser=args.open_browser))
    finally:
        asyncio.run(runtime.close())


if __name__ == '__main__':
    main()
