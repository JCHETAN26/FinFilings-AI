#!/usr/bin/env python3
"""Evaluate the real Go/Python stack against published supporting-fact qrels."""
import argparse
import csv
import heapq
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time
import urllib.request
import numpy as np
from ranx import Qrels, Run, evaluate


def request(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=150) as response:
        if int(response.headers.get('Content-Length', '0')) > 4 << 20:
            raise ValueError('oversized response')
        raw = response.read((4 << 20) + 1)
        if len(raw) > 4 << 20:
            raise ValueError('oversized response')
        return json.loads(raw)


def exact_ids(vectors, query, db, k=10):
    # Raw baseline is disk-backed; at most 2048 scores plus k retained rows.
    heap = []
    for start in range(0, vectors.shape[0], 2048):
        scores = vectors[start:start + 2048] @ query
        size = min(k, len(scores))
        positions = np.argpartition(scores, len(scores)-size)[-size:]
        for pos in positions:
            item = (float(scores[pos]), start + int(pos))
            if len(heap) < k:
                heapq.heappush(heap, item)
            elif item > heap[0]:
                heapq.heapreplace(heap, item)
    result = []
    for _, row_index in sorted(heap, reverse=True):
        row = db.execute('SELECT id FROM passages WHERE row_index=?', (row_index,)).fetchone()
        if row is None:
            raise ValueError('raw vector row missing in passage store')
        result.append(row[0])
    return result


def rank_order(hits):
    """Preserve returned tie order in ranx. These ordinal scores encode rank,
    not model relevance; original numeric scores remain in the raw evidence.
    """
    return {hit['id']:float(len(hits)-rank) for rank,hit in enumerate(hits)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data', type=Path, default=Path('data/phase2'))
    ap.add_argument('--index', type=Path, default=Path('indexes/vector-phase2'))
    ap.add_argument('--bridge', default='http://127.0.0.1:8765')
    ap.add_argument('--sidecar', default='http://127.0.0.1:8766')
    ap.add_argument('--out', type=Path, default=Path('bench_kit/results/phase2/evaluation'))
    args = ap.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    sys.path.insert(0, str(Path('py').resolve()))
    from common import check_artifacts, open_store, sha256
    manifest = check_artifacts(args.index)
    provenance = json.loads((args.data / 'provenance.json').read_text())
    for file, key in [('chunks.jsonl','corpus_sha256'),('queries.jsonl','query_sha256'),('qrels.tsv','qrels_sha256')]:
        if sha256(args.data / file) != provenance[key]:
            raise ValueError(f'changed evaluation input: {file}')
    for endpoint in (args.bridge, args.sidecar):
        if request(endpoint + '/health')['corpus_sha256'] != provenance['corpus_sha256']:
            raise ValueError('evaluation corpus/service mismatch')
    judgments = {}
    with (args.data / 'qrels.tsv').open() as f:
        for row in csv.DictReader(f, delimiter='\t'):
            judgments.setdefault(row['query-id'], {})[row['corpus-id']] = int(row['score'])
            if len(judgments) > 10000:
                raise ValueError('evaluation metadata exceeds 10000-query bound')
    runs = {name:{} for name in ('sparse','dense','hybrid','reranked')}
    latencies, recalls = [], []
    vectors = np.load(args.index / 'vectors.f32.npy', mmap_mode='r')
    db = open_store(args.index / 'passages.sqlite')
    with (args.data / 'queries.jsonl').open() as questions, (args.out / 'per-query.jsonl').open('w') as raw:
        for number, line in enumerate(questions, 1):
            if number > 10000:
                raise ValueError('too many evaluation queries')
            q = json.loads(line)
            if q['id'] not in judgments or q['id'] in runs['sparse']:
                raise ValueError('missing judgment or repeated query ID')
            for doc_id in judgments[q['id']]:
                if db.execute('SELECT 1 FROM passages WHERE id=?', (doc_id,)).fetchone() is None:
                    raise ValueError('gold document absent from corpus')
            begin = time.perf_counter()
            result = request(args.bridge + '/search', dict(query=q['query'], rerank=True))
            elapsed = time.perf_counter()-begin
            latencies.append(elapsed)
            for name in runs:
                hits = result[name]
                if not hits or len({h['id'] for h in hits}) != len(hits) or any(not math.isfinite(h['score']) for h in hits):
                    raise ValueError('invalid/missing actual ranking')
                runs[name][q['id']] = rank_order(hits)
            if len(result['hybrid']) > 50 or len(result['reranked']) > 10:
                raise ValueError('candidate budget violated')
            dense = request(args.sidecar + '/dense', dict(query=q['query'], k=10, include_vector=True))
            exact = exact_ids(vectors, np.asarray(dense['query_vector'], dtype=np.float32), db)
            recall = len(set(exact) & {h['id'] for h in dense['hits']}) / len(exact)
            recalls.append(recall)
            raw.write(json.dumps(dict(id=q['id'], query=q['query'], seconds=elapsed, pq_recall_at_10=recall, rankings=result)) + '\n')
            raw.flush()
            if number % 10 == 0:
                print(f'evaluated {number} real labeled queries', flush=True)
    db.close()
    vectors._mmap.close()
    qrels = Qrels(judgments)
    if set(runs['sparse']) != set(judgments):
        raise ValueError('query set does not match qrels exactly')
    ndcg = {name:float(evaluate(qrels, Run(ranking, name=name), 'ndcg@10')) for name, ranking in runs.items()}
    memory = {mode:[] for mode in ('raw','pq')}
    for mode in memory:
        for _ in range(3):
            output = subprocess.check_output([sys.executable,'bench_kit/pq_memory.py','--index',str(args.index),'--mode',mode], text=True)
            memory[mode].append(json.loads(output))
    raw_bytes = memory['raw'][0]['native_payload_bytes']
    pq_bytes = memory['pq'][0]['native_payload_bytes']
    samples = sorted(latencies)
    report = dict(ndcg_at_10=ndcg, hybrid_minus_sparse=ndcg['hybrid']-ndcg['sparse'], reranked_minus_sparse=ndcg['reranked']-ndcg['sparse'],
                  evaluated_queries=len(samples), pq_recall_at_10_vs_exact_float32=float(statistics.mean(recalls)),
                  vector_memory=dict(raw_native_payload_bytes=raw_bytes, pq_native_payload_bytes=pq_bytes,
                                     exact_native_payload_reduction_percent=100*(1-pq_bytes/raw_bytes),
                                     raw_rss_delta_median_bytes=statistics.median(x['observed_rss_delta_bytes'] for x in memory['raw']),
                                     pq_rss_delta_median_bytes=statistics.median(x['observed_rss_delta_bytes'] for x in memory['pq']), probes=memory),
                  end_to_end_serial_query_seconds=dict(p50=statistics.median(samples),p99=samples[math.ceil(.99*len(samples))-1],max=max(samples)),
                  vector_manifest=manifest, corpus_provenance=provenance,
                  notes=['Published FinQA binary supporting-fact labels are not exhaustive relevance judgments or answer correctness.',
                         'ranx receives descending ordinal scores to preserve the returned rank order, including Go deterministic ties; original real model/RRF scores are preserved in raw per-query rankings.',
                         f'First {len(samples)} eligible dev queries chosen before scoring; no relevance tuning. Corpus contains train/dev context, as an unsupervised retrieval collection, not QA answer/program text.',
                         'PQ8 is IndexPQ(d=384,M=48,nbits=8), not scalar SQ8. Exact native payload includes codebooks and materialized auxiliary arrays; it excludes unknown allocator headers/capacity, model, and passage store.',
                         'RSS is separately observed in isolated real FAISS-only subprocesses, three per mode. Whole application RAM does not shrink by the vector payload percentage.',
                         'CPU-only serial end-to-end timings include BGE query encoding, Go parallel sparse/dense bridge, RRF and real MiniLM Top-50 to Top-10 reranking; not the Phase 1 sparse-only p99.',
                         'ID recall vs exact float32 may penalize interchangeable ties from identical text at different source locations.'])
    (args.out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('vector_manifest','corpus_provenance')}, indent=2), flush=True)


if __name__ == '__main__':
    main()
