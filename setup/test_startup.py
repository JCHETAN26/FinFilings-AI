"""Offline startup contracts. Fixture routing is not a retrieval-quality benchmark."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Startup(unittest.TestCase):
    def test_packaged_start_preserves_interpreter_and_clears_overlay(self):
        start = load('start')
        artifacts = load('setup-artifacts')
        with tempfile.TemporaryDirectory(prefix='checkout with spaces ') as tmp:
            root = Path(tmp)
            (root / 'setup-profile.json').write_text(json.dumps(artifacts.PROFILE))
            with patch.object(start, 'ROOT', root), patch.object(sys, 'argv', ['start.py', '--check-only']), \
                 patch.object(sys, 'prefix', '/fixture-venv'), patch.object(sys, 'base_prefix', '/fixture-system'), \
                 patch.dict(os.environ, {'PYTHONPATH': '/untrusted/models/runtime'}), \
                 patch.object(start.subprocess, 'call', return_value=0) as call:
                self.assertEqual(start.main(), 0)
            args = call.call_args.args[0]
            self.assertEqual(args[0], sys.executable)
            self.assertIn('--isolated-runtime', args)
            self.assertEqual(args[-1], '--check-only')
            self.assertIn('--model-lock', args)
            self.assertIn('complete_report', args)
            self.assertEqual(call.call_args.kwargs['cwd'], root)
            self.assertEqual(call.call_args.kwargs['env']['PYTHONPATH'], '')
            self.assertEqual(call.call_args.kwargs['env']['PYTHONNOUSERSITE'], '1')

    def test_start_refuses_global_python(self):
        start = load('start')
        with patch.object(sys, 'prefix', sys.base_prefix), patch.object(start.subprocess, 'call') as call:
            with self.assertRaisesRegex(SystemExit, 'virtual environment'):
                start.main()
            call.assert_not_called()

    def test_invalid_profile_never_starts_process(self):
        start = load('start')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'setup-profile.json').write_text('{}')
            with patch.object(start, 'ROOT', root), patch.object(sys, 'prefix', '/fixture-venv'), \
                 patch.object(sys, 'base_prefix', '/fixture-system'), patch.object(start.subprocess, 'call') as call:
                with self.assertRaisesRegex(SystemExit, 'Invalid setup profile'):
                    start.main()
                call.assert_not_called()

    def test_check_only_exits_before_provider_or_process_creation(self):
        launcher = load('serve-audit-ui')
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd(); previous_path = sys.path[:]; previous_mask = os.umask(0o077)
            try:
                with patch.object(launcher, 'ROOT', Path(tmp)), patch.object(launcher, 'require_artifacts'), \
                     patch('audit_web.enrollment.enroll', return_value={'filings': 1, 'corpus_sha256': 'a'*64}), \
                     patch('agent.provider.ClaudeJSON', side_effect=AssertionError('provider constructed')), \
                     patch('audit_web.runtime.build_runtime', side_effect=AssertionError('runtime constructed')), \
                     patch('subprocess.Popen', side_effect=AssertionError('process started')):
                    self.assertEqual(launcher.main(['--check-only', '--isolated-runtime']), 0)
                    self.assertFalse((Path(tmp) / 'data/audit-ui').exists())
            finally:
                os.chdir(previous); sys.path[:] = previous_path; os.umask(previous_mask)

    def test_relative_model_lock_uses_its_parent_not_cwd(self):
        from common import lock_model, sha256
        with tempfile.TemporaryDirectory(prefix='portable models ') as tmp:
            root = Path(tmp); data = root / 'data'; data.mkdir()
            snapshot = root / 'models/snapshot'; snapshot.mkdir(parents=True)
            weights = snapshot / 'model.safetensors'; weights.write_bytes(b'unit fixture weights')
            lock = data / 'models.json'
            lock.write_text(json.dumps({'fixture': {'path': '../models/snapshot',
                'revision': 'unit-fixture', 'weights_sha256': sha256(weights)}}))
            resolved = lock_model(lock, 'fixture')
            self.assertEqual(Path(resolved['path']), snapshot.resolve())
            weights.write_bytes(b'corrupt fixture weights')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                lock_model(lock, 'fixture')

    def test_native_cpu_artifacts_roundtrip(self):
        import faiss
        import numpy as np
        import onnx
        from onnx import TensorProto, helper
        import onnxruntime as ort
        import torch
        torch.set_num_threads(2); faiss.omp_set_num_threads(2)
        self.assertEqual(torch.version.cuda, None, 'CI must install CPU-only torch')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vectors = np.eye(8, dtype='float32')
            index = faiss.IndexFlatIP(8); index.add(vectors)
            faiss.write_index(index, str(root / 'index.faiss'))
            _, ids = faiss.read_index(str(root / 'index.faiss')).search(vectors[:1], 1)
            self.assertEqual(int(ids[0, 0]), 0)
            graph = helper.make_graph([helper.make_node('Identity', ['x'], ['y'])], 'setup-wheel-fixture',
                [helper.make_tensor_value_info('x', TensorProto.FLOAT, [1, 8])],
                [helper.make_tensor_value_info('y', TensorProto.FLOAT, [1, 8])])
            model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)])
            model.ir_version = 9
            onnx.save(model, root / 'fixture.onnx')
            options = ort.SessionOptions(); options.intra_op_num_threads = 2; options.inter_op_num_threads = 1
            session = ort.InferenceSession(str(root / 'fixture.onnx'), options, providers=['CPUExecutionProvider'])
            self.assertTrue(np.array_equal(session.run(None, {'x': vectors[:1]})[0], vectors[:1]))


if __name__ == '__main__':
    unittest.main()
