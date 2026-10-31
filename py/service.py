#!/usr/bin/env python3
"""Local bounded FAISS PQ8 + PyTorch sidecar. No external inference API or mock path."""
import argparse
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import heapq
import time
from pathlib import Path
import faiss
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn
from common import Encoder, check_artifacts, open_store
from rerank import Reranker
from agent.filters import FilingFilter


class BodyLimit:
    """Bound request bodies before JSON parsing, including chunked requests."""
    def __init__(self, app, max_bytes=32768):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > self.max_bytes:
                await send({'type':'http.response.start', 'status':413, 'headers':[(b'content-type', b'text/plain')]})
                await send({'type':'http.response.body', 'body':b'request exceeds 32 KiB'})
                return
            if not message.get('more_body', False):
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type':'http.request', 'body':bytes(body), 'more_body':False}
            return await receive()
        return await self.app(scope, replay, send)


class DenseRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8192)
    k: int = Field(default=100, ge=1, le=1000)
    include_vector: bool = False
    filters: FilingFilter = Field(default_factory=FilingFilter)


class RerankRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8192)
    ids: list[str] = Field(min_length=1, max_length=50)
    k: int = Field(default=10, ge=1, le=10)


class Admission:
    """Bound active request bodies and inference waiters before reading input."""
    def __init__(self, app, budget):
        self.app, self.budget = app, budget

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') not in ('/dense', '/rerank'):
            return await self.app(scope, receive, send)
        budget = self.budget
        if budget['active'] >= budget['limit']:
            budget['rejected'] += 1
            await send({'type':'http.response.start', 'status':429,
                        'headers':[(b'content-type',b'text/plain'),(b'retry-after',b'1')]})
            await send({'type':'http.response.body', 'body':b'inference admission budget exhausted'})
            return
        budget['active'] += 1
        budget['peak'] = max(budget['peak'], budget['active'])
        try:
            return await self.app(scope, receive, send)
        finally:
            budget['active'] -= 1


class Retrieval:
    def __init__(self, directory, model_lock, threads):
        directory = Path(directory)
        self.manifest = check_artifacts(directory)
        self.index = faiss.read_index(str(directory / 'index.pq8.faiss'))
        if not isinstance(self.index, faiss.IndexPQ) or self.index.pq.nbits != 8 or self.index.ntotal != self.manifest['documents']:
            raise ValueError('expected matching trained PQ8 index')
        self.store = directory / 'passages.sqlite'
        with closing(open_store(self.store)) as db:
            if db.execute('SELECT count(*) FROM passages').fetchone()[0] != self.index.ntotal:
                raise ValueError('passage store/index size mismatch')
        self.encoder = Encoder(model_lock, threads)
        if self.encoder.lock['revision'] != self.manifest['model_revision'] or self.encoder.dimension != self.index.d:
            raise ValueError('query encoder does not match indexed vectors')
        self.reranker = Reranker(model_lock, self.store)
        self.threads = threads

    def dense(self, request):
        faiss.omp_set_num_threads(self.threads)
        vector = self.encoder.encode([request.query], query=True)
        if request.filters == FilingFilter():
            scores, positions = self.index.search(vector, min(request.k, self.index.ntotal))
        else:
            scores, positions = self.filtered_search(vector, request.k, request.filters)
        hits = []
        with closing(open_store(self.store)) as db:
            for score, position in zip(scores[0], positions[0]):
                if position < 0:
                    continue
                row = db.execute('SELECT id FROM passages WHERE row_index=?', (int(position),)).fetchone()
                if row is None or not np.isfinite(score):
                    raise ValueError('invalid FAISS row/score')
                hits.append(dict(id=row[0], score=float(score)))
        response = dict(hits=hits, corpus_sha256=self.manifest['corpus_sha256'])
        if request.include_vector:
            response['query_vector'] = vector[0].tolist()
        return response


    def filtered_search(self, vector, k, filters):
        """Exact top-k of PQ approximate scores within the predicate.

        IndexPQ does not support IDSelector. Stream eligible IDs from SQLite in
        blocks of 2048 and search copied PQ codes with the ORIGINAL centroids.
        No raw embedding materialization or corpus-sized ID/score arrays.
        """
        where, values = filters.sql()
        codes = faiss.rev_swig_ptr(self.index.codes.data(), self.index.codes.size()).reshape(-1, self.index.code_size)
        block = faiss.IndexPQ(self.index.d, self.index.pq.M, 8, self.index.metric_type)
        faiss.copy_array_to_vector(faiss.vector_to_array(self.index.pq.centroids), block.pq.centroids)
        block.is_trained = True
        heap = []
        with closing(open_store(self.store)) as db:
            cursor = db.execute('SELECT row_index FROM passages WHERE ' + where + ' ORDER BY row_index', values)
            while rows := cursor.fetchmany(2048):
                mapping = np.asarray([r[0] for r in rows], dtype=np.int64)
                faiss.copy_array_to_vector(np.ascontiguousarray(codes[mapping]).reshape(-1), block.codes)
                block.ntotal = len(mapping)
                scores, ids = block.search(vector, min(k, len(mapping)))
                for score, local in zip(scores[0], ids[0]):
                    item = (float(score), -int(mapping[local]))
                    if len(heap) < k:
                        heapq.heappush(heap, item)
                    elif item > heap[0]:
                        heapq.heapreplace(heap, item)
        ranked = sorted(heap, reverse=True)
        return np.asarray([[s for s, _ in ranked]], dtype=np.float32), np.asarray([[-p for _, p in ranked]], dtype=np.int64)


def app_for(retrieval, executor, max_pending=64):
    if not 1 <= max_pending <= 256:
        raise ValueError('admission budget must be 1..256')
    app = FastAPI(title='FinFilings PQ8 and cross-encoder sidecar')
    app.add_middleware(BodyLimit)
    budget = dict(limit=max_pending, active=0, peak=0, rejected=0)
    app.add_middleware(Admission, budget=budget)
    metrics = {}
    slots = asyncio.Semaphore(1)  # A single active CPU model/FAISS request; connection cap bounds the queue.

    async def invoke(function, *args):
        queued = time.perf_counter()
        await slots.acquire()
        began = time.perf_counter()
        try:
            future = asyncio.get_running_loop().run_in_executor(executor, function, *args)
        except BaseException:
            slots.release()
            raise
        # Cancellation of the HTTP task must not release the slot while the
        # native inference thread is still active. Only actual completion does.
        def finished(completed):
            elapsed = time.perf_counter() - began
            record = metrics.setdefault(function.__name__, dict(count=0, work_seconds=0., wait_seconds=0., max_work_seconds=0.))
            record['count'] += 1
            record['work_seconds'] += elapsed
            record['wait_seconds'] += began - queued
            record['max_work_seconds'] = max(record['max_work_seconds'], elapsed)
            # Retrieve failures even when the client was cancelled.
            if not completed.cancelled():
                completed.exception()
            slots.release()
        future.add_done_callback(finished)
        try:
            return await asyncio.shield(future)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get('/health')
    async def health():
        return dict(status='ready', documents=retrieval.index.ntotal, corpus_sha256=retrieval.manifest['corpus_sha256'],
                    encoder_revision=retrieval.encoder.lock['revision'], reranker_revision=retrieval.reranker.lock['revision'],
                    encoder_device=str(next(retrieval.encoder.model.parameters()).device),
                    reranker_device=str(next(retrieval.reranker.model.parameters()).device),
                    admission=budget.copy(), inference_workers=1, inference=metrics.copy(),
                    threads=retrieval.threads, rerank_batch_size=retrieval.reranker.batch_size,
                    rerank_length_bucketing=True)

    @app.post('/dense')
    async def dense(request: DenseRequest):
        if not request.query.strip():
            raise HTTPException(status_code=422, detail='query must contain text')
        return await invoke(retrieval.dense, request)

    @app.post('/rerank')
    async def rerank(request: RerankRequest):
        if not request.query.strip():
            raise HTTPException(status_code=422, detail='query must contain text')
        if len(set(request.ids)) != len(request.ids) or any(not i or len(i) > 256 for i in request.ids):
            raise HTTPException(status_code=422, detail='candidate IDs must be unique, nonempty and <=256 characters')
        hits = await invoke(retrieval.reranker.rank, request.query, request.ids, request.k)
        return dict(hits=hits, corpus_sha256=retrieval.manifest['corpus_sha256'])

    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--index', default='indexes/vector-phase2')
    ap.add_argument('--model-lock', default='data/phase2-models.json')
    ap.add_argument('--threads', type=int, default=4)
    ap.add_argument('--port', type=int, default=8766)
    ap.add_argument('--max-pending', type=int, default=64)
    ap.add_argument('--http-concurrency', type=int, default=128)
    args = ap.parse_args()
    if not 1 <= args.max_pending <= 256 or not args.max_pending < args.http_concurrency <= 512:
        ap.error('require 1 <= max-pending <= 256 and max-pending < http-concurrency <= 512')
    retrieval = Retrieval(args.index, args.model_lock, args.threads)
    print(json.dumps(dict(ready=True, manifest=retrieval.manifest)), flush=True)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='finfilings-inference') as executor:
        uvicorn.run(app_for(retrieval, executor, args.max_pending), host='127.0.0.1', port=args.port,
                    limit_concurrency=args.http_concurrency, timeout_keep_alive=5)


if __name__ == '__main__':
    main()
