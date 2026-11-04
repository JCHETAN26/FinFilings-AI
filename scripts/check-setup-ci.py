#!/usr/bin/env python3
"""Run artifact/startup checks from a clean source copy, denying network and model generation."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TESTS = ('test_artifact_manager', 'test_managed_startup', 'test_setup', 'test_startup', 'test_audit_web_ui_static',
         'test_audit_web_app.StaticAssets', 'test_audit_web_app.Lifespan',
         'test_retrieval_storage', 'test_phase2.Contracts')


def worker(report):
    import faulthandler
    faulthandler.dump_traceback_later(30)
    # Install denial hooks before importing any application/test module.
    denied = []
    ipv6_probes = []
    def audit(event, args):
        # urllib3 checks IPv6 support by binding an ephemeral loopback socket on import.
        if event == 'socket.bind' and args[1] == ('::1', 0):
            ipv6_probes.append('urllib3 IPv6 availability probe')
            return
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.bind'):
            denied.append(event)
            raise RuntimeError('Network forbidden in offline setup CI')
        if event == 'subprocess.Popen' and Path(os.fsdecode(args[0])).name not in ('sleep', 'node'):
            denied.append('subprocess:' + Path(os.fsdecode(args[0])).name)
            raise RuntimeError('Unexpected subprocess forbidden in offline setup CI')
    sys.addaudithook(audit)
    sys.path[:0] = [str(ROOT / name) for name in ('setup', 'py', 'bench_kit')]
    from agent.provider import ClaudeJSON
    async def forbidden(*args, **kwargs):
        denied.append('generation')
        raise RuntimeError('Generation forbidden in offline setup CI')
    ClaudeJSON.generate = forbidden
    suite = unittest.defaultTestLoader.loadTestsFromNames(TESTS)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    faulthandler.cancel_dump_traceback_later()
    data = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                skipped=[reason for _, reason in result.skipped], denied_operations=denied,
                generation_calls=0, ipv6_availability_probes=len(ipv6_probes), artifact_mode='unit fixtures; no SEC/model bundle',
                clean_checkout=True, success=result.wasSuccessful() and not denied)
    report.write_text(json.dumps(data, indent=2) + '\n')
    return 0 if data['success'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    report = args.report.resolve(); report.parent.mkdir(parents=True, exist_ok=True)
    if args.worker:
        return worker(report)
    if sys.prefix == sys.base_prefix:
        raise SystemExit('Run with the bootstrapped virtual environment Python')
    with tempfile.TemporaryDirectory(prefix='finfilings CI checkout ') as tmp:
        root = Path(tmp)
        for name in ('scripts', 'setup', 'py', 'bench_kit', 'ui', 'releases'):
            shutil.copytree(ROOT / name, root / name,
                            ignore=shutil.ignore_patterns('results', '__pycache__', 'node_modules'))
        env = dict(os.environ, PYTHONPATH='', PYTHONNOUSERSITE='1', HF_HUB_OFFLINE='1',
                   TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1', OPENBLAS_NUM_THREADS='1',
                   OMP_NUM_THREADS='2', MKL_NUM_THREADS='2', NUMBA_NUM_THREADS='2',
                   TOKENIZERS_PARALLELISM='false', MPLCONFIGDIR=str(root / '.mpl-cache'))
        return subprocess.call([sys.executable, str(root / 'scripts/check-setup-ci.py'),
                                '--worker', '--report', str(report)], cwd=root, env=env, timeout=300)


if __name__ == '__main__':
    sys.exit(main())
