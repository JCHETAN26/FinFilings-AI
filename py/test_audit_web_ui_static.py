"""Static UI contract, bind policy and launcher safety helpers. Browser behaviour (rendering, focus, XSS display) is verified separately in a real browser."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest.mock
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_web import limits
from audit_web.app import allowed_hosts_for
from audit_web.enrollment import enroll
from audit_web.server import uvicorn_config, validate_bind

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'ui'
HTML, JS, CSS = (UI / 'index.html').read_text(), (UI / 'app.js').read_text(), (UI / 'app.css').read_text()


def load_launcher():
    spec = importlib.util.spec_from_file_location('serve_audit_ui', ROOT / 'scripts/serve-audit-ui.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StaticAssets(unittest.TestCase):
    def test_no_inline_script_style_handlers_or_external_resources(self):
        self.assertEqual(re.findall(r'<script\b[^>]*>', HTML), ['<script src="/app.js" defer>'])
        self.assertNotRegex(HTML, r'<script[^>]*>\s*\S[^<]*</script>')
        self.assertNotRegex(HTML, r'\son[a-z]+\s*=')
        self.assertNotRegex(HTML, r'\sstyle\s*=')
        self.assertNotIn('<style', HTML)
        self.assertEqual(sorted(set(re.findall(r'(?:src|href)="([^"#]+)"', HTML))), ['/app.css', '/app.js', '/favicon.svg'])
        self.assertNotRegex(HTML + CSS + JS, r'https?://(?!www\.sec\.gov)')  # no CDN, font host or analytics URL anywhere
        self.assertNotIn('@import', CSS)
        self.assertNotRegex(CSS, r'url\(')
        self.assertIn('system-ui', CSS)

    def test_javascript_never_parses_markup_or_persists_results(self):
        for forbidden in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function', 'localStorage', 'sessionStorage', 'indexedDB',
                          'XMLHttpRequest', 'WebSocket', 'importScripts', 'DOMParser', 'srcdoc', 'javascript:', 'cookie'):
            self.assertNotIn(forbidden, JS.replace('never innerHTML', ''), forbidden)
        self.assertNotRegex(JS, r"setAttribute\(\s*['\"]on")
        self.assertNotRegex(JS, r"\.(?:href|src)\s*=[^=]")  # links are only created through the validating helper
        self.assertEqual(re.findall(r"fetch\(([^,)]*)", JS), ['path'])  # one same-origin fetch wrapper
        self.assertIn("url.hostname === 'www.sec.gov'", JS)
        self.assertIn("startsWith('/Archives/')", JS)
        self.assertIn("'noopener noreferrer'", JS)

    def test_every_element_id_used_by_the_script_exists_once_in_the_page(self):
        ids = re.findall(r'\sid="([^"]+)"', HTML)
        self.assertEqual(len(ids), len(set(ids)), 'duplicate ids')
        used = set(re.findall(r"\$\('([a-z-]+)'\)", JS))
        self.assertLessEqual(used, set(ids), used - set(ids))

    def test_required_controls_and_accessibility_labels_exist(self):
        for fragment in ('id="question"', 'id="submit-button"', 'id="cancel-button"', 'id="status-pill"', 'id="status-elapsed"', 'id="samples"', 'id="corpus-list"',
                         'id="history-list"', 'id="evidence-dialog"', 'id="evidence-close"', 'aria-live="polite"', 'role="alert"', 'class="skip-link"', 'lang="en"'):
            self.assertIn(fragment, HTML, fragment)
        self.assertRegex(HTML, r'<label for="question">')
        self.assertIn(':focus-visible', CSS)
        self.assertIn('prefers-reduced-motion', CSS)

    def test_only_real_statuses_are_displayed(self):
        statuses = set(re.findall(r"(\w+): '([A-Z][a-z]+)'", JS.split('STATUS_LABEL')[1].split('};')[0]))
        self.assertEqual({k for k, _ in statuses}, {'pending', 'running', 'completed', 'failed', 'cancelled'})
        self.assertNotRegex(JS, r'(?i)\b\d+\s*%|progress|percent|step \d of')  # no invented progress stages

    @unittest.skipUnless(shutil.which('node'), 'node not installed')
    def test_javascript_parses(self):
        subprocess.run(['node', '--check', str(UI / 'app.js')], check=True)


class BindPolicy(unittest.TestCase):
    def test_non_loopback_binds_are_refused(self):
        for host in ('0.0.0.0', '::', '192.168.1.5', '10.0.0.1', 'example.com', '', '127.0.0.1.evil.example', '0.0.0.0/0', '127.0.0.2', 'LOCALHOST6'):
            with self.assertRaises(ValueError, msg=host):
                validate_bind(host, 8766)
        self.assertEqual(validate_bind('localhost', 8766), '127.0.0.1')
        self.assertEqual(validate_bind('127.0.0.1', 8766), '127.0.0.1')
        self.assertEqual(validate_bind('::1', 8766), '::1')

    def test_ports_are_validated(self):
        for port in (0, 80, 1023, 65536, -1):
            with self.assertRaises(ValueError):
                validate_bind('127.0.0.1', port)
        with self.assertRaises(ValueError):
            validate_bind('127.0.0.1', 8766, (80,))

    def test_allowed_hosts_are_exact(self):
        self.assertEqual(allowed_hosts_for('127.0.0.1', 8766), {'127.0.0.1:8766', 'localhost:8766'})
        self.assertEqual(allowed_hosts_for('127.0.0.1', 8766, (9000,)), {'127.0.0.1:8766', 'localhost:8766', '127.0.0.1:9000', 'localhost:9000'})
        self.assertEqual(allowed_hosts_for('::1', 8766), {'[::1]:8766', 'localhost:8766'})

    def test_uvicorn_ingress_bounds(self):
        config = uvicorn_config(lambda *a: None, '127.0.0.1', 8766)
        self.assertEqual((config.limit_concurrency, config.backlog, config.h11_max_incomplete_event_size, config.access_log, config.host),
                         (limits.UVICORN_LIMIT_CONCURRENCY, limits.UVICORN_BACKLOG, limits.MAX_HEADER_BYTES, False, '127.0.0.1'))
        self.assertEqual((config.http, config.ws, config.server_header), ('h11', 'none', False))


class LauncherSafety(unittest.TestCase):
    def setUp(self):
        self.launcher = load_launcher()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        self.launcher.ROOT = self.root

    def test_documented_defaults_match_the_requested_current_v3_artifacts(self):
        args = self.launcher.build_parser().parse_args([])
        self.assertEqual((str(args.corpus_dir), str(args.ingest_report), str(args.sparse_index), str(args.vector_index), str(args.reranker_model), args.port, args.host, args.browser),
                         ('data/sec-scale-v3-evaluation-20261101', 'indexes/sec-scale-v3-20261101/report.json', 'indexes/sparse-sec-scale-v3-20261101',
                          'indexes/vector-sec-scale-v3-pq128-20261101', 'models/reranker-onnx-fp32-optimized-20261031', 8766, '127.0.0.1', False))
        self.assertEqual(str(args.original_provenance), 'data/sec/provenance.json')

    def test_work_dir_is_created_fresh_and_private(self):
        run = self.launcher.prepare_work_dir()
        self.assertEqual(run.parent, self.root / 'data' / 'audit-ui')
        self.assertTrue(run.is_dir())
        if stat.S_IMODE(run.stat().st_mode) != 0o777:  # exFAT cannot represent modes; elsewhere they must be private
            self.assertEqual(stat.S_IMODE(run.parent.stat().st_mode) & 0o022, 0)

    def test_symlinked_or_foreign_or_stray_files_are_refused(self):
        base = self.root / 'data' / 'audit-ui'
        target = self.root / 'elsewhere'
        target.mkdir()
        base.symlink_to(target)
        with self.assertRaises(SystemExit):
            self.launcher.prepare_work_dir()
        base.unlink()
        base.mkdir()
        (base / 'bridge').write_text('stale binary lookalike')
        with self.assertRaises(SystemExit):
            self.launcher.prepare_work_dir()
        (base / 'bridge').unlink()
        (base / 'link').symlink_to(target)
        with self.assertRaises(SystemExit):
            self.launcher.prepare_work_dir()
        (base / 'link').unlink()
        os.chmod(base, 0o777)
        if base.stat().st_mode & 0o022:  # only meaningful where the filesystem keeps modes
            with self.assertRaises(SystemExit):
                self.launcher.prepare_work_dir()
        (self.root / 'data').rename(self.root / 'data-real')
        (self.root / 'data').symlink_to(self.root / 'data-real')
        with self.assertRaises(SystemExit):
            self.launcher.prepare_work_dir()

    def test_stop_ends_only_the_process_group_it_owns(self):
        owned = subprocess.Popen(['sleep', '60'], start_new_session=True)
        unrelated = subprocess.Popen(['sleep', '60'])  # same group as this test process: must never be signalled
        self.addCleanup(lambda: (unrelated.kill(), unrelated.wait()))
        self.launcher.stop(owned)
        self.assertIsNotNone(owned.returncode)
        time.sleep(0.1)
        self.assertIsNone(unrelated.poll())

    def test_stop_signals_only_the_child_when_it_is_not_a_group_leader(self):
        shared = subprocess.Popen(['sleep', '60'])  # not its own session: killpg would hit the test runner
        self.launcher.stop(shared)
        self.assertEqual(shared.returncode, -signal.SIGTERM)

    def test_exit_classification_separates_operator_stops_from_failures(self):
        classify = self.launcher.classify_exit
        self.assertEqual(classify(KeyboardInterrupt()), ('stopped', 'interrupt', 130))
        self.assertEqual(classify(self.launcher.Terminated(143)), ('stopped', 'SIGTERM', 143))
        for failure in (SystemExit(143), SystemExit(1), SystemExit('refusing: something'), SystemExit(0), RuntimeError('service exited'), TimeoutError(), OSError('port in use')):
            self.assertEqual(classify(failure), ('failed', None, None), repr(failure))  # a lookalike exit code from elsewhere is not our SIGTERM

    def test_startup_timeout_must_be_finite_and_between_1_and_600(self):
        parse = self.launcher.build_parser().parse_args
        self.assertEqual(parse([]).startup_timeout, 180)
        self.assertEqual((parse(['--startup-timeout', '1']).startup_timeout, parse(['--startup-timeout', '600']).startup_timeout), (1, 600))
        for bad in ('nan', 'inf', '-inf', '0', '0.5', '-5', '601', '1e9', 'abc', ''):
            with self.assertRaises(SystemExit, msg=bad), unittest.mock.patch('sys.stderr'):
                parse(['--startup-timeout', bad])

    def test_missing_artifacts_are_reported_not_downloaded(self):
        args = self.launcher.build_parser().parse_args(['--corpus-dir', 'nope'])
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)
        with self.assertRaises(SystemExit) as ctx:
            self.launcher.require_artifacts(args)
        self.assertIn('nothing is downloaded', str(ctx.exception))
        self.assertIn('nope/chunks.jsonl', str(ctx.exception))

    def test_launcher_has_no_network_fetch_or_kill_by_name(self):
        text = (ROOT / 'scripts/serve-audit-ui.py').read_text()
        for forbidden in ('pkill', 'killall', 'git ', 'curl', 'wget', 'snapshot_download', 'requests.'):
            self.assertNotIn(forbidden, text)
        self.assertIn("GOPROXY='off'", text)
        self.assertIn("HF_HUB_OFFLINE='1'", text)


class Enrollment(unittest.TestCase):
    def test_checksum_mismatch_between_report_and_corpus_provenance_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / 'chunks.jsonl').write_text('{}\n')
            report = directory / 'report.json'
            report.write_text('{}')
            (directory / 'provenance.json').write_text(json.dumps(dict(new_sec_report_sha256='0' * 64, corpus_sha256='1' * 64, sec_sha256='2' * 64)))
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                enroll(root=ROOT, corpus_dir=directory, ingest_report=report, original_provenance=report)


if __name__ == '__main__':
    unittest.main()
