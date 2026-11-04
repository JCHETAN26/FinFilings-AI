#!/usr/bin/env python3
"""Start the packaged parser-v3 profile using the active isolated Python environment."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    if sys.prefix == sys.base_prefix:
        raise SystemExit('Activate the setup virtual environment first.')
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--release-store', type=Path)
    options, remaining = parser.parse_known_args(sys.argv[1:])
    store_root = options.release_store or ROOT / 'artifacts'
    managed = options.release_store is not None or (store_root / 'state.json').exists()
    extra = []
    if managed:
        sys.path.insert(0, str(ROOT / 'py'))
        from artifact_manager import Store
        release, payload = Store(store_root).active()
        profile = {key: value if key == 'source_enrollment' else str(payload / value)
                   for key, value in release['profile'].items()}
        extra = ['--artifact-root', str(payload), '--work-dir', str(store_root.absolute() / 'runs')]
    else:
        profile = json.loads((ROOT / 'setup-profile.json').read_text())
    allowed = {'corpus_dir', 'ingest_report', 'original_provenance', 'sparse_index',
               'vector_index', 'reranker_model', 'model_lock', 'source_enrollment'}
    if set(profile) != allowed:
        raise SystemExit('Invalid setup profile')
    args = [sys.executable, str(ROOT / 'scripts/serve-audit-ui.py'), '--isolated-runtime']
    for key, value in profile.items():
        args.extend(['--' + key.replace('_', '-'), value])
    env = dict(os.environ, PYTHONNOUSERSITE='1', PYTHONPATH='')
    return subprocess.call(args + extra + remaining, cwd=ROOT, env=env)

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f'Cannot start FinFilings: {exc}') from None
