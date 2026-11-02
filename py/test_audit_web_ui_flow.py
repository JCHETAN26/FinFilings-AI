"""Runs py/ui_flow/harness.js: frontend flow tests (fake DOM + scripted fetch; TEST DOUBLES) for ui/app.js."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'node not installed')
class FrontendFlows(unittest.TestCase):
    def test_result_association_races_timeouts_and_cancel(self):
        done = subprocess.run(['node', str(Path(__file__).parent / 'ui_flow' / 'harness.js')], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout.count('ok   '), 7, done.stdout)


if __name__ == '__main__':
    unittest.main()
