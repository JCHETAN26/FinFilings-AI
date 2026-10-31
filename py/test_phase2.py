"""Offline contract tests; integration validation below exercises real trained models."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path('py').resolve()))
sys.path.insert(0, str(Path('bench_kit').resolve()))
from common import check_artifacts, lock_model
from prepare_finqa import row_text
from service import DenseRequest, RerankRequest, BodyLimit
from pydantic import ValidationError


class Contracts(unittest.TestCase):
    def test_uniform_table_rendering(self):
        self.assertEqual(row_text(['company','revenue','year'],['example','12','2023']), 'company the example of revenue is 12 ; the example of year is 2023 ;')

    def test_artifact_corruption_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)
            (p/'codes').write_bytes(b'unit-test artifact')
            (p/'manifest.json').write_text(json.dumps({'files':{'codes':hashlib.sha256(b'unit-test artifact').hexdigest()}}))
            check_artifacts(p)
            (p/'codes').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                check_artifacts(p)

    def test_model_weights_checksum_is_required(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            (directory/'model.safetensors').write_bytes(b'unit test corrupt weight file')
            lock=directory/'lock.json'
            lock.write_text(json.dumps({'model':{'revision':'a'*40,'path':str(directory),'weights_sha256':'0'*64}}))
            with self.assertRaises(ValueError):
                lock_model(lock,'model')

    def test_request_budgets(self):
        for args in ({'query':'x','k':1001},{'query':''}):
            with self.assertRaises(ValidationError):
                DenseRequest(**args)
        with self.assertRaises(ValidationError):
            RerankRequest(query='x',ids=['id']*51)

    def test_chunked_body_limit_without_invoking_inference(self):
        # These are ASGI transport messages, not fake model/vector implementations.
        sent=[]
        async def never_called(scope,receive,send):
            self.fail('oversized payload reached inference app')
        async def receive():
            return {'type':'http.request','body':b'x'*20000,'more_body':True}
        async def send(message):
            sent.append(message)
        asyncio.run(BodyLimit(never_called)({'type':'http'},receive,send))
        self.assertEqual(sent[0]['status'],413)

class EvaluationOrder(unittest.TestCase):
    def test_equal_model_scores_preserve_the_returned_ranking(self):
        from eval_hybrid import rank_order
        from ranx import Qrels, Run, evaluate
        ranking=rank_order([{'id':'relevant','score':0.01},{'id':'other','score':0.01}])
        result=evaluate(Qrels({'q':{'relevant':1}}), Run({'q':ranking}), 'ndcg@10')
        self.assertEqual(result,1.0)



class AdmissionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_budget_is_bounded_and_cancelled_requests_release(self):
        from service import Admission
        budget=dict(limit=2,active=0,peak=0,rejected=0)
        entered=asyncio.Event()
        async def transport(scope,receive,send):
            if budget['active']==2:entered.set()
            await asyncio.Event().wait()
        async def receive():return {'type':'http.request','body':b''}
        sent=[]
        async def send(message):sent.append(message)
        app=Admission(transport,budget)
        scope=dict(type='http',path='/dense')
        tasks=[asyncio.create_task(app(scope,receive,send)) for _ in range(2)]
        await asyncio.wait_for(entered.wait(),2)
        await app(scope,receive,send)
        self.assertEqual(sent[0]['status'],429)
        self.assertEqual(budget,dict(limit=2,active=2,peak=2,rejected=1))
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.assertEqual(budget['active'],0)

if __name__ == '__main__':
    unittest.main()
