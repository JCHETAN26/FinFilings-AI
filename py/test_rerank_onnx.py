"""Real pinned-model ONNX integration checks; no generated inputs enter benchmarks."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
import torch
from rerank import Reranker


@unittest.skipUnless(importlib.util.find_spec('onnxruntime'), 'optional ONNX Runtime not installed')
class ONNXIntegration(unittest.TestCase):
    def test_dynamic_shapes_match_pytorch_fp32_logits(self):
        artifact = Path('models/reranker-onnx-fp32-optimized-20261031')
        if not artifact.exists():self.skipTest('requires exported real pinned MiniLM artifact')
        torch.set_num_threads(2)
        store = 'indexes/vector-sec-scale-v2-pq128-20261031/passages.sqlite'
        reference = Reranker('data/phase2-models.json', store)
        optimized = Reranker('data/phase2-models.json', store, onnx_directory=artifact, threads=2)
        self.assertEqual(reference.batch_size, 8)
        self.assertEqual(optimized.batch_size, 1)
        for batch, length in ((1,8),(2,129),(2,512)):
            with self.subTest(batch=batch,length=length):
                # Valid token IDs and masks test dynamic graph dimensions and masked padding.
                ids = np.tile(np.arange(length, dtype=np.int64)%1000+100, (batch,1))
                mask = np.ones_like(ids);mask[:,-3:]=0
                types = np.zeros_like(ids);types[:,length//2:]=1
                feed = dict(input_ids=ids, attention_mask=mask, token_type_ids=types)
                with torch.inference_mode():
                    expected=reference.model(**{k:torch.from_numpy(v) for k,v in feed.items()}).logits.numpy()
                actual=optimized.session.run(['logits'],feed)[0]
                np.testing.assert_allclose(actual,expected,atol=1e-4,rtol=1e-4)
        self.assertEqual(optimized.session.get_providers(),['CPUExecutionProvider'])
        self.assertEqual(optimized.session.get_session_options().intra_op_num_threads,2)
        # Mismatched provenance must fail before native session construction.
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            (directory/'manifest.json').write_text(json.dumps(dict(files={},model='wrong-model')))
            with self.assertRaisesRegex(ValueError,'pinned FP32 reranker'):
                Reranker('data/phase2-models.json',store,onnx_directory=directory)


    def test_default_single_pair_rank_reports_real_work_without_padding(self):
        artifact=Path('models/reranker-onnx-fp32-optimized-20261031')
        store=Path('indexes/vector-sec-scale-v2-pq128-20261031/passages.sqlite')
        replay=Path('bench_kit/results/ingest/scale-v2-20261031/benchmark-pq128-onnx-workers4-threads2/evaluation/per-query.jsonl')
        if not all(p.exists() for p in (artifact,store,replay)):
            self.skipTest('requires acquired corpus, real saved candidates and exported model')
        with replay.open() as source:case=json.loads(next(source))
        model=Reranker('data/phase2-models.json',store,onnx_directory=artifact,threads=2)
        ids=[case['rankings']['hybrid'][i]['id'] for i in (0,15,49)]
        hits=model.rank(case['query'],ids,k=3)
        self.assertEqual({h['id'] for h in hits},set(ids))
        self.assertEqual(model.last_work['pairs'],3)
        self.assertEqual(model.last_work['padded_tokens'],model.last_work['unpadded_tokens'])
        phases=model.last_work['timings']
        self.assertTrue(all(value>=0 for value in phases.values()))
        self.assertGreater(phases['inference_seconds'],0)
        self.assertLessEqual(sum(phases.values()),model.last_work['total_seconds'])


if __name__=='__main__':unittest.main()
