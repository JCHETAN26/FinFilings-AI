#!/usr/bin/env python3
"""Start the packaged parser-v3 profile using the active isolated Python environment."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    if sys.prefix == sys.base_prefix:
        raise SystemExit('Activate the setup virtual environment first.')
    profile = json.loads((ROOT / 'setup-profile.json').read_text())
    allowed = {'corpus_dir', 'ingest_report', 'original_provenance', 'sparse_index',
               'vector_index', 'reranker_model', 'model_lock', 'source_enrollment'}
    if set(profile) != allowed:
        raise SystemExit('Invalid setup profile')
    args = [sys.executable, str(ROOT / 'scripts/serve-audit-ui.py'), '--isolated-runtime']
    for key, value in profile.items():
        args.extend(['--' + key.replace('_', '-'), value])
    env = dict(os.environ, PYTHONNOUSERSITE='1', PYTHONPATH='')
    return subprocess.call(args + sys.argv[1:], cwd=ROOT, env=env)

if __name__ == '__main__':
    sys.exit(main())
