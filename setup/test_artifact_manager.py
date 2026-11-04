"""Versioned artifact integrity/activation contracts; all data are synthetic unit fixtures."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from artifact_testkit import archive, encode, payload, release, sha
import artifact_manager as m


class ArtifactManager(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='artifact store with spaces ')
        self.addCleanup(self.temp.cleanup); self.root=Path(self.temp.name)
        self.store=m.Store(self.root/'store')
        self.bundle,self.release,self.raw=release(self.root)

    def install(self):
        return self.store.install(self.release,self.raw,self.bundle)

    def test_install_activate_and_rollback_preserve_old_version(self):
        self.assertFalse(self.install()['activated']); self.assertIsNone(self.store.state()['active'])
        self.store.activate('test-one')
        other,version,raw=release(self.root,'two'); self.store.install(version,raw,other)
        self.store.activate('test-two')
        self.assertEqual(self.store.state()['previous'],'test-one')
        self.assertEqual(self.store.activate(rollback=True)['active'],'test-one')
        self.assertEqual(self.store.active()[0]['corpus'],self.release['corpus'])
        self.assertTrue(self.store.directory('test-two').is_dir())

    def test_idempotent_install_and_release_id_immutability(self):
        self.install()
        self.assertEqual(self.install()['status'],'already_installed')
        changed={**self.release,'source_commit':'b'*40}
        with self.assertRaisesRegex(ValueError,'immutable'):
            self.store.install(changed,encode(changed),self.bundle)

    def test_manifest_hash_is_independently_required(self):
        path=self.root/'release.json';path.write_bytes(self.raw)
        with self.assertRaisesRegex(ValueError,'manifest checksum'):
            m.load_release(path,'0'*64)
        self.assertEqual(m.load_release(path,sha(self.raw))[0],self.release)

    def test_corrupt_bundle_never_installs_or_activates(self):
        self.bundle.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'bundle identity'):
            self.install()
        self.assertIsNone(self.store.state()['active'])
        self.assertFalse(self.store.directory('test-one').exists())

    def test_installed_corruption_blocks_activation_and_keeps_active(self):
        self.install();self.store.activate('test-one')
        other,version,raw=release(self.root,'two');self.store.install(version,raw,other)
        (self.store.directory('test-two')/'payload/data/corpus/chunks.jsonl').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'checksum'):
            self.store.activate('test-two')
        self.assertEqual(self.store.state()['active'],'test-one')

    def test_rollback_refuses_damaged_previous_release(self):
        self.install();self.store.activate('test-one')
        other,version,raw=release(self.root,'two');self.store.install(version,raw,other);self.store.activate('test-two')
        (self.store.directory('test-one')/'payload/setup-profile.json').write_text('{}')
        with self.assertRaises(ValueError):self.store.activate(rollback=True)
        self.assertEqual(self.store.state()['active'],'test-two')

    def test_failed_state_replace_keeps_old_state(self):
        self.install();self.store.activate('test-one')
        other,version,raw=release(self.root,'two');self.store.install(version,raw,other)
        with patch('artifact_manager.os.replace',side_effect=OSError('injected storage failure')):
            with self.assertRaises(OSError):self.store.activate('test-two')
        self.assertEqual(self.store.state()['active'],'test-one')
        self.assertEqual(list(self.store.root.glob('*.tmp')),[])

    def test_store_serializes_writers_without_unbounded_wait(self):
        with self.store.locked():
            with self.assertRaisesRegex(ValueError,'busy'):
                with m.Store(self.store.root).locked():pass

    def test_failed_extraction_cleans_staging_and_keeps_old_state(self):
        with patch('artifact_manager.inspect_bundle',side_effect=OSError('injected extraction failure')):
            with self.assertRaises(OSError):self.install()
        self.assertEqual(list((self.store.root/'releases').iterdir()),[])
        self.assertIsNone(self.store.state()['active'])

    def test_insufficient_disk_space_never_activates(self):
        from types import SimpleNamespace
        with patch('artifact_manager.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError,'insufficient free space'):
                self.install()
        self.assertIsNone(self.store.state()['active'])

    def test_raw_manifest_must_match_release_object(self):
        with self.assertRaisesRegex(ValueError,'manifest bytes'):
            self.store.install(self.release,encode({**self.release,'release_id':'changed'}),self.bundle)

    def test_unsafe_paths_and_duplicate_json_keys_are_rejected(self):
        for value in ('../a','/a','a/../b','a//b','a\\b','.'):
            with self.subTest(value=value),self.assertRaises(ValueError):m.relative(value)
        with self.assertRaisesRegex(ValueError,'duplicate JSON'):m.json_bytes(b'{"a":1,"a":2}')

    def test_links_are_refused_in_store_and_payload(self):
        target=self.root/'actual';target.mkdir();link=self.root/'link';link.symlink_to(target)
        with self.assertRaisesRegex(ValueError,'symlink'):m.Store(link)
        self.install();path=self.store.directory('test-one')/'payload/data/corpus/chunks.jsonl'
        path.unlink();path.symlink_to(self.bundle)
        with self.assertRaisesRegex(ValueError,'symlink'):self.store.verify('test-one')

    def test_no_rollback_without_previous(self):
        with self.assertRaisesRegex(ValueError,'no previous'):self.store.activate(rollback=True)

    def test_cross_artifact_corruption_is_rejected_even_with_valid_outer_hashes(self):
        variants={
            'wrong sparse':('indexes/sparse/CORPUS_SHA256',b'0'*64),
            'wrong model':('data/models.json',None),
            'wrong section':('indexes/ingest/report.json',None),
            'wrong vector':('indexes/vector/manifest.json',None)}
        for label,(name,replacement) in variants.items():
            files=payload()
            if replacement is None:
                data=json.loads(files[name])
                if 'models' in name:next(iter(data.values()))['weights_sha256']='0'*64
                elif 'vector' in name:data['documents']=99
                else:data['raw_retained']=False
                replacement=encode(data)
                if name=='indexes/ingest/report.json':
                    prov=json.loads(files['data/corpus/provenance.json']);prov['new_sec_report_sha256']=sha(replacement);files['data/corpus/provenance.json']=encode(prov)
            files[name]=replacement
            with self.subTest(label=label),self.assertRaises(ValueError):m.inspect_bundle(archive(self.root/'bad.tar',files))

    def test_official_recipe_refuses_synthetic_bundle_and_writes_no_release(self):
        root=Path(__file__).resolve().parents[1]
        recipe=m.read_json(root/'releases/parser-v3-20261104/recipe.json')
        with self.assertRaisesRegex(ValueError,'pinned release recipe'):
            m.prepare(self.bundle,recipe,'https://example.invalid/bundle.tar',self.root/'official')
        self.assertFalse((self.root/'official').exists())

    def test_release_preparation_never_overwrites(self):
        result=m.inspect_bundle(self.bundle)
        recipe=dict(release_id='new',source_commit='a'*40,corpus=result['corpus'],models=result['models'])
        with self.assertRaisesRegex(ValueError,'fresh'):
            m.prepare(self.bundle,recipe,'https://example.invalid/bundle.tar',self.root)

    def test_new_archive_inspector_rejects_traversal_duplicates_and_links(self):
        import io
        import tarfile
        variants=[('../escape',False),('setup-profile.json',False),('link',True)]
        for name,link in variants:
            bad=archive(self.root/'unsafe.tar',payload())
            with tarfile.open(bad,'a') as tar:
                entry=tarfile.TarInfo(name)
                if link:
                    entry.type=tarfile.SYMTYPE;entry.linkname='/etc/passwd';tar.addfile(entry)
                else:
                    entry.size=1;tar.addfile(entry,io.BytesIO(b'x'))
            with self.subTest(name=name),self.assertRaises(ValueError):m.inspect_bundle(bad)
        self.assertFalse((self.root/'escape').exists())

    def test_state_and_download_cache_symlinks_are_rejected(self):
        self.store.root.mkdir()
        other=self.root/'other';other.write_text('{}')
        (self.store.root/'state.json').symlink_to(other)
        with self.assertRaisesRegex(ValueError,'symlink'):self.store.state()
        part=self.root/'part';part.symlink_to(other)
        with self.assertRaisesRegex(ValueError,'symlink'):
            m.download(dict(url='https://example.invalid/a',bytes=2,sha256='0'*64),part)

    def test_missing_tokenizer_files_cannot_be_released(self):
        files=payload();target=next(k for k in files if k.endswith('/vocab.txt'));del files[target]
        with self.assertRaisesRegex(ValueError,'incomplete local model'):
            m.inspect_bundle(archive(self.root/'missing-tokenizer.tar',files))

    def test_urls_require_https_and_disallow_credentials(self):
        for url in ('http://example.com/a','file:///tmp/a','https://user:pass@example.com/a'):
            with self.subTest(url=url),self.assertRaises(ValueError):m.check_url(url)
        self.assertEqual(m.check_url('http://127.0.0.1:1234/a',True),'http://127.0.0.1:1234/a')


if __name__=='__main__':unittest.main()
