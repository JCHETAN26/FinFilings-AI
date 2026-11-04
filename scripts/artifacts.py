#!/usr/bin/env python3
"""Install and select hash-pinned immutable FinFilings releases. No model-provider calls."""
import argparse
import json
import logging
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))
from artifact_manager import Store, load_release, prepare, read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, default=ROOT / 'artifacts')
    sub = parser.add_subparsers(dest='operation', required=True)
    p = sub.add_parser('prepare', help='verify a real bundle and generate release metadata locally')
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--recipe', type=Path, required=True)
    p.add_argument('--url', required=True, help='planned public HTTPS URL of the bundle')
    p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('install', help='download/restore one release; never overwrite an installed version')
    p.add_argument('--manifest', required=True)
    p.add_argument('--sha256', required=True, help='independently trusted SHA-256 of release.json')
    p.add_argument('--bundle-file', type=Path, help='use an existing verified bundle instead of downloading')
    p.add_argument('--activate', action='store_true')
    p.add_argument('--allow-loopback-http', action='store_true', help='HTTP loopback fixtures only; HTTPS is the default')
    p = sub.add_parser('activate'); p.add_argument('release_id')
    sub.add_parser('rollback')
    p = sub.add_parser('verify'); p.add_argument('release_id', nargs='?')
    sub.add_parser('status')
    args = parser.parse_args(argv)
    store = Store(args.store)
    if args.operation == 'prepare':
        result = prepare(args.bundle, read_json(args.recipe), args.url, args.out)
    elif args.operation == 'install':
        release, raw = load_release(args.manifest, args.sha256, args.allow_loopback_http)
        result = store.install(release, raw, args.bundle_file, args.allow_loopback_http)
        if args.activate:
            result['state'] = store.activate(release['release_id'])
    elif args.operation in ('activate', 'rollback'):
        result = store.activate(getattr(args, 'release_id', None), rollback=args.operation == 'rollback')
    elif args.operation == 'verify':
        with store.locked():
            version = args.release_id or store.state()['active']
            if version is None:
                raise ValueError('no active artifact release')
            release, payload = store.verify(version)
            result = dict(release_id=version, verified=True, corpus=release['corpus'], payload=str(payload))
    else:
        with store.locked():
            result = store.state()
            directory = store.root / 'releases'
            result['installed'] = sorted(p.name for p in directory.iterdir() if p.is_dir() and not p.name.startswith('.')) if directory.exists() else []
            result['payload_verification'] = 'not_run; use verify'
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc)}), file=sys.stderr)
        sys.exit(1)
