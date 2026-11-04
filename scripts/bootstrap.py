#!/usr/bin/env python3
"""Install the tested CPU runtime into a new isolated environment (Linux x86_64/Python 3.10)."""
import argparse
import os
from pathlib import Path
import platform
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venv', type=Path, default=ROOT / '.venv')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 10) or platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise SystemExit('This lock is tested only on Linux x86_64 with Python 3.10. Other platforms need a separately verified lock.')
    destination = args.venv.resolve()
    if destination.exists():
        raise SystemExit('Choose a fresh venv directory; existing environments are never modified.')
    env = dict(os.environ, PYTHONNOUSERSITE='1', PYTHONPATH='', PIP_CONFIG_FILE=os.devnull,
               PIP_CACHE_DIR=str(ROOT / '.cache/setup-pip'), PIP_DISABLE_PIP_VERSION_CHECK='1')
    venv.EnvBuilder(with_pip=True, symlinks=False).create(destination)
    python = str(destination / 'bin/python')
    subprocess.run([python, '-m', 'pip', 'install', '--require-hashes', '--no-deps',
                    '-r', str(ROOT / 'setup/runtime-linux-x86_64-py310.lock')], env=env, check=True)
    subprocess.run([python, '-m', 'pip', 'check'], env=env, check=True)
    print(f'Activate: source "{destination}/bin/activate"')
    print('Restore your artifact bundle, then run: python scripts/start.py --check-only')

if __name__ == '__main__':
    main()
