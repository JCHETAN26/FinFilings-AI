#!/usr/bin/env python3
"""Export or restore a checksummed, relocatable parser-v3 runtime bundle. No inference."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = dict(corpus_dir='data/sec-all-v3-evaluation-20261104',
               ingest_report='indexes/sec-all-v3-20261104/report.json',
               original_provenance='data/sec/provenance.json',
               sparse_index='indexes/sparse-sec-all-v3-20261104',
               vector_index='indexes/vector-sec-all-v3-pq128-20261104',
               reranker_model='models/reranker-onnx-fp32-optimized-20261031',
               model_lock='data/setup-models.json', source_enrollment='complete_report')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def safe_name(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or '..' in p.parts or str(p) != name or '\\' in name:
        raise ValueError('unsafe bundle path')
    return p


def export_bundle(root, output):
    if output.exists():
        raise ValueError('bundle output already exists')
    paths = set()
    for name in (*[v for k, v in PROFILE.items() if k not in ('model_lock', 'source_enrollment')],
                 'data/sec/chunks.jsonl'):
        p = root / name
        if name == PROFILE['ingest_report']:
            p = p.parent
        paths.update(x for x in p.rglob('*') if x.is_file()) if p.is_dir() else paths.add(p)
    lock_path = root / PROFILE['model_lock']
    if not lock_path.is_file():
        lock_path = root / 'data/phase2-models.json'
    lock = json.loads(lock_path.read_text())
    for entry in lock.values():
        snapshot = Path(entry['path'])
        if not snapshot.is_absolute():
            snapshot = lock_path.parent / snapshot
        # Copy model contents under a stable portable name; HF cache symlinks are resolved only here.
        destination = 'models/setup-snapshots/' + entry['revision']
        entry['path'] = '../' + destination
        entry['_source'] = snapshot
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as tmp, tarfile.open(output, 'x') as archive:
            files = {}
            def add(source, name):
                safe_name(name)
                if name in files:
                    raise ValueError('duplicate bundle path')
                if not source.is_file():
                    raise ValueError('missing artifact: ' + str(source))
                files[name] = dict(bytes=source.stat().st_size, sha256=digest(source))
                # dereference model-cache links, archive only regular file payloads
                info = tarfile.TarInfo(name)
                info.size = files[name]['bytes']
                info.mode = 0o600
                with source.open('rb') as stream:
                    archive.addfile(info, stream)
            for p in sorted(paths):
                if p.is_symlink():
                    raise ValueError('artifact symlink not allowed')
                add(p, p.relative_to(root).as_posix())
            for entry in lock.values():
                source = entry.pop('_source')
                for p in sorted(source.iterdir()):
                    if p.is_file() and p.suffix in ('.json', '.txt', '.safetensors'):
                        add(p, entry['path'][3:] + '/' + p.name)
            for name, data in [('data/setup-models.json', lock), ('setup-profile.json', PROFILE)]:
                p = Path(tmp) / Path(name).name
                p.write_text(json.dumps(data, indent=2) + '\n')
                add(p, name)
            p = Path(tmp) / 'bundle-manifest.json'
            p.write_text(json.dumps(dict(version=1, files=files), indent=2) + '\n')
            archive.add(p, arcname='bundle-manifest.json', recursive=False)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    print(json.dumps(dict(bundle=str(output), bytes=output.stat().st_size, sha256=digest(output))))


def restore(bundle, root):
    """Validate entire archive before writing. Refuse symlinks, duplicates, extras and overwrites."""
    with tarfile.open(bundle, 'r:') as archive:
        members = archive.getmembers()
        if len(members) > 20000 or sum(m.size for m in members) > 20 * (1 << 30):
            raise ValueError('bundle exceeds limits')
        names = set()
        for m in members:
            safe_name(m.name)
            if not m.isfile() or m.name in names:
                raise ValueError('nonregular or duplicate bundle entry')
            names.add(m.name)
        metadata = archive.getmember('bundle-manifest.json')
        if metadata.size > 8 << 20:
            raise ValueError('manifest too large')
        manifest = json.load(archive.extractfile(metadata))
        if manifest.get('version') != 1 or names != set(manifest['files']) | {'bundle-manifest.json'}:
            raise ValueError('bundle manifest entry mismatch')
        for m in members:
            target = root / m.name
            if target.exists() or target.is_symlink():
                raise ValueError('refusing overwrite: ' + m.name)
            if any(p.is_symlink() for p in target.parents):
                raise ValueError('symlink destination parent')
            if m.name != 'bundle-manifest.json':
                h = hashlib.sha256()
                with archive.extractfile(m) as stream:
                    for block in iter(lambda: stream.read(1 << 20), b''):
                        h.update(block)
                if manifest['files'][m.name] != dict(bytes=m.size, sha256=h.hexdigest()):
                    raise ValueError('bundle checksum mismatch: ' + m.name)
        # Inputs are immutable during this operation. Copy after all checks, never tar.extract.
        for m in members:
            target = root / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as output, archive.extractfile(m) as source:
                shutil.copyfileobj(source, output, 1 << 20)
    print(json.dumps(dict(restored_files=len(members), root=str(root))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('export', 'restore'))
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    if args.operation == 'export':
        export_bundle(args.root.resolve(), args.bundle.resolve())
    else:
        restore(args.bundle.resolve(), args.root.resolve())


if __name__ == '__main__':
    main()
