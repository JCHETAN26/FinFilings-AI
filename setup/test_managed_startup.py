"""Managed startup selects a verified immutable payload; no generation or native inference."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from artifact_testkit import release
from artifact_manager import Store

ROOT=Path(__file__).resolve().parents[1]

class ManagedStartup(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'managed store')
        bundle,data,raw=release(self.root);self.store.install(data,raw,bundle);self.store.activate('test-one')
        spec=importlib.util.spec_from_file_location('managed_start',ROOT/'scripts/start.py')
        self.start=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.start)

    def test_managed_start_requires_no_legacy_data_or_profile_in_code_checkout(self):
        code=self.root/'clean checkout';code.mkdir()
        with patch.object(self.start,'ROOT',code), patch.object(sys,'prefix','/test-venv'),patch.object(sys,'base_prefix','/test-system'), \
             patch.object(sys,'argv',['start.py','--release-store',str(self.store.root),'--check-only']), \
             patch.object(self.start.subprocess,'call',return_value=0) as call:
            self.assertEqual(self.start.main(),0)
        args=call.call_args.args[0]
        payload=self.store.directory('test-one')/'payload'
        self.assertEqual(args[args.index('--artifact-root')+1],str(payload))
        self.assertEqual(args[args.index('--model-lock')+1],str(payload/'data/models.json'))
        self.assertEqual(args[args.index('--work-dir')+1],str(self.store.root/'runs'))
        self.assertEqual(args[-1],'--check-only')
        self.assertFalse((code/'data').exists())

    def test_start_refuses_tampering_before_spawning_anything(self):
        (self.store.directory('test-one')/'payload/setup-profile.json').write_text('{}')
        with patch.object(sys,'prefix','/test-venv'),patch.object(sys,'base_prefix','/test-system'), \
             patch.object(sys,'argv',['start.py','--release-store',str(self.store.root)]), \
             patch.object(self.start.subprocess,'call') as call:
            with self.assertRaisesRegex(ValueError,'checksum'):
                self.start.main()
            call.assert_not_called()

    def test_real_managed_preflight_enrolls_without_loading_models(self):
        import os
        spec=importlib.util.spec_from_file_location('managed_launcher',ROOT/'scripts/serve-audit-ui.py')
        launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
        release,payload=self.store.active()
        args=['--isolated-runtime','--check-only','--artifact-root',str(payload),
              '--work-dir',str(self.store.root/'runs')]
        for key,value in release['profile'].items():
            args += ['--'+key.replace('_','-'),value if key=='source_enrollment' else str(payload/value)]
        previous=Path.cwd();previous_path=sys.path[:];mask=os.umask(0o077)
        try:
            with patch('agent.provider.ClaudeJSON',side_effect=AssertionError('provider constructed')), \
                 patch('audit_web.runtime.build_runtime',side_effect=AssertionError('runtime constructed')), \
                 patch('subprocess.Popen',side_effect=AssertionError('process started')):
                self.assertEqual(launcher.main(args),0)
            self.assertFalse((self.store.root/'runs').exists())
        finally:
            os.chdir(previous);sys.path[:]=previous_path;os.umask(mask)

    def test_mutable_runs_are_outside_the_release_payload(self):
        spec=importlib.util.spec_from_file_location('run_dirs_launcher',ROOT/'scripts/serve-audit-ui.py')
        launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
        run=launcher.prepare_work_dir(self.store.root/'runs')
        self.assertTrue(run.is_dir())
        self.assertFalse(run.is_relative_to(self.store.active()[1]))

    def test_activation_changes_do_not_delete_a_running_versions_payload(self):
        old=self.store.active()[1]
        other,data,raw=release(self.root,'two');self.store.install(data,raw,other);self.store.activate('test-two')
        self.assertTrue((old/'data/corpus/chunks.jsonl').is_file())
        self.assertNotEqual(old,self.store.active()[1])

if __name__=='__main__':unittest.main()
