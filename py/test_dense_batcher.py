"""Scheduling contracts and real-model dense batch equivalence; fixtures are never benchmark metrics."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import unittest
import numpy as np
from dense_batcher import DenseBatcher
from service import app_for, DenseRequest, Retrieval
from agent.filters import FilingFilter


class BatchScheduling(unittest.IsolatedAsyncioTestCase):
    async def test_response_mapping_partial_batches_and_execution_bound(self):
        active=0;peak=0;seen=[]
        async def execute(group):
            nonlocal active,peak
            active+=1;peak=max(peak,active);seen.append(list(group))
            try:
                await asyncio.sleep(.01)
                return [str(item) for item in group]
            finally:active-=1
        batcher=DenseBatcher(execute,batch_size=4,coordinators=2,capacity=32,wait_ms=1)
        try:
            values=await asyncio.wait_for(asyncio.gather(*(batcher.submit(i) for i in range(19))),2)
            self.assertEqual(values,list(map(str,range(19))))
            self.assertLessEqual(peak,2)
            self.assertTrue(all(1<=len(group)<=4 for group in seen))
            self.assertEqual(await asyncio.wait_for(batcher.submit('partial'),1),'partial')
        finally:await batcher.close()
        self.assertTrue(all(task.done() for task in batcher.tasks))
        self.assertEqual(batcher.queue.qsize(),0)

    async def test_overflow_error_propagation_and_cancelled_queue_entries(self):
        release=asyncio.Event()
        async def execute(group):
            await release.wait()
            if 'failure' in group:raise ValueError('explicit scheduling fixture failure')
            return group
        batcher=DenseBatcher(execute,batch_size=1,coordinators=1,capacity=1,wait_ms=0)
        first=asyncio.create_task(batcher.submit('hold'))
        queued=None
        try:
            for _ in range(100):
                if batcher.metrics['batches']:break
                await asyncio.sleep(.001)
            queued=asyncio.create_task(batcher.submit('cancel'))
            await asyncio.sleep(0)
            with self.assertRaises(asyncio.QueueFull):await batcher.submit('overflow')
            self.assertEqual(batcher.metrics['rejected'],1)
            queued.cancel();await asyncio.gather(queued,return_exceptions=True)
            release.set();self.assertEqual(await first,'hold')
            await batcher.queue.join()
            self.assertEqual(batcher.metrics['cancelled_before_dispatch'],1)
            with self.assertRaisesRegex(ValueError,'fixture failure'):await batcher.submit('failure')
        finally:
            release.set()
            await batcher.close()
            await asyncio.gather(first,*([queued] if queued else []),return_exceptions=True)

    async def test_cancelled_http_batch_keeps_native_slots_until_completion(self):
        release=threading.Event();seen=[]
        class Replicas:
            encoders=[object(),object()]
            def dense_batch(self,requests,worker):
                seen.append((tuple(r.query for r in requests),worker))
                if any(r.query=='hold' for r in requests) and not release.wait(5):
                    raise TimeoutError('fixture did not release native execution')
                return [dict(hits=[],corpus_sha256='scheduling-only') for _ in requests]
        with ThreadPoolExecutor(max_workers=3) as executor:
            app=app_for(Replicas(),executor,16,workers=2,dense_batch_size=4,dense_batch_wait_ms=0,dense_batch_trigger=0)
            async with app.router.lifespan_context(app):
                route=next(r for r in app.routes if getattr(r,'path','')=='/dense').endpoint
                tasks=[asyncio.create_task(route(DenseRequest(query='hold'))) for _ in range(8)]
                following=None
                try:
                    for _ in range(200):
                        if len(seen)==2:break
                        await asyncio.sleep(.005)
                    self.assertEqual(len(seen),2)
                    for task in tasks:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                    following=asyncio.create_task(route(DenseRequest(query='next')))
                    await asyncio.sleep(.05)
                    self.assertEqual(len(seen),2,'native slot released on HTTP cancellation')
                    release.set();await asyncio.wait_for(following,2)
                    self.assertEqual(len(seen),3)
                finally:
                    release.set()
                    for task in tasks+([following] if following else []):
                        if not task.done():task.cancel()
                    await asyncio.gather(*tasks,*([following] if following else []),return_exceptions=True)


    async def test_low_pressure_requests_keep_the_immediate_path(self):
        seen=[]
        class Replicas:
            encoders=[object(),object()]
            def dense(self,request,worker):
                seen.append(('single',request.query))
                return dict(hits=[],corpus_sha256='scheduling-only')
            def dense_batch(self,requests,worker):
                raise AssertionError('low-pressure request entered the collection queue')
        with ThreadPoolExecutor(max_workers=2) as executor:
            app=app_for(Replicas(),executor,16,workers=2,dense_batch_size=8)
            async with app.router.lifespan_context(app):
                route=next(r for r in app.routes if getattr(r,'path','')=='/dense').endpoint
                await route(DenseRequest(query='low-pressure'))
                self.assertEqual(seen,[('single','low-pressure')])


class DenseModelBatch(unittest.TestCase):
    def test_mixed_filtered_requests_match_single_query_execution(self):
        directory=Path('indexes/vector-sec-scale-v2-pq128-20261031')
        artifact=Path('models/reranker-onnx-fp32-optimized-20261031')
        if not directory.exists() or not artifact.exists():self.skipTest('requires acquired real model/vector artifacts')
        retrieval=Retrieval(directory,'data/phase2-models.json',2,1,artifact,1)
        requests=[DenseRequest(query='Apple revenue and risk factors',k=5,include_vector=True),
                  DenseRequest(query='Apple revenue and risk factors',k=10,include_vector=True,filters=FilingFilter(ticker='AAPL',year=2023)),
                  DenseRequest(query='Apple revenue and risk factors',k=1,include_vector=True,filters=FilingFilter(ticker='MISSING'))]
        expected=[retrieval.dense(r) for r in requests]
        actual=retrieval.dense_batch(requests)
        self.assertEqual(len(actual),len(expected))
        for before,after in zip(expected,actual):
            self.assertEqual([h['id'] for h in before['hits']],[h['id'] for h in after['hits']])
            np.testing.assert_allclose([h['score'] for h in before['hits']],[h['score'] for h in after['hits']],atol=1e-6,rtol=1e-6)
            np.testing.assert_allclose(before['query_vector'],after['query_vector'],atol=1e-6,rtol=1e-6)
        with self.assertRaises(ValueError):retrieval.dense_batch([])
        with self.assertRaises(ValueError):retrieval.dense_batch(requests*3)


if __name__=='__main__':unittest.main()
