"""Offline setup integrity contracts; never imports a generation provider."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('artifacts', Path(__file__).resolve().parents[1] / 'scripts/setup-artifacts.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

class BundleTests(unittest.TestCase):
    def bundle(self, path, name='data/a.txt', payload=b'passages', checksum=None, symlink=False):
        files = {name: dict(bytes=len(payload), sha256=checksum or hashlib.sha256(payload).hexdigest())}
        with tarfile.open(path, 'w') as archive:
            info = tarfile.TarInfo(name)
            if symlink:
                info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            content = json.dumps(dict(version=1, files=files)).encode()
            info = tarfile.TarInfo('bundle-manifest.json'); info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    def test_portable_export_can_be_exported_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); root = base / 'source'; root.mkdir()
            for key, name in a.PROFILE.items():
                if key in ('source_enrollment', 'model_lock'):
                    continue
                path = root / name
                if key in ('ingest_report', 'original_provenance'):
                    path.parent.mkdir(parents=True, exist_ok=True); path.write_text('{}')
                else:
                    path.mkdir(parents=True, exist_ok=True); (path / 'payload').write_bytes(b'unit fixture')
            (root / 'data/sec/chunks.jsonl').write_bytes(b'unit fixture')
            snapshot = root / 'models/setup-snapshots/revision'
            snapshot.mkdir(parents=True); (snapshot / 'model.safetensors').write_bytes(b'unit weights')
            (root / a.PROFILE['model_lock']).write_text(json.dumps({'model': {
                'path': '../models/setup-snapshots/revision', 'revision': 'revision',
                'weights_sha256': a.digest(snapshot / 'model.safetensors')}}))
            a.export_bundle(root, base / 'first.tar')
            fresh = base / 'fresh'; a.restore(base / 'first.tar', fresh)
            lock = json.loads((fresh / a.PROFILE['model_lock']).read_text())
            self.assertFalse(Path(lock['model']['path']).is_absolute())
            a.export_bundle(fresh, base / 'second.tar')
            second = base / 'second'; a.restore(base / 'second.tar', second)
            self.assertEqual((second / a.PROFILE['model_lock']).read_bytes(),
                             (fresh / a.PROFILE['model_lock']).read_bytes())

    def test_restore_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive = root / 'bundle.tar'; target = root / 'fresh checkout'
            self.bundle(archive); a.restore(archive, target)
            self.assertEqual((target / 'data/a.txt').read_bytes(), b'passages')
            with self.assertRaisesRegex(ValueError, 'overwrite'): a.restore(archive, target)

    def test_corruption_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive = root / 'bundle.tar'; target = root / 'out'
            self.bundle(archive, checksum='0' * 64)
            with self.assertRaisesRegex(ValueError, 'checksum'): a.restore(archive, target)
            self.assertFalse(target.exists())

    def test_traversal_and_links(self):
        for name, link in [('../escape', False), ('/escape', False), ('data/link', True)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); archive = root / 'bundle.tar'
                self.bundle(archive, name=name, symlink=link)
                with self.assertRaises(ValueError): a.restore(archive, root / 'out')

    def test_destination_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive = root / 'bundle.tar'; target = root / 'out'
            target.mkdir(); (target / 'data').symlink_to(root, target_is_directory=True)
            self.bundle(archive)
            with self.assertRaisesRegex(ValueError, 'symlink'): a.restore(archive, target)

if __name__ == '__main__': unittest.main()
