#!/usr/bin/env python3
"""Integration checks against actual running FAISS/BGE/MiniLM/Go services."""
import concurrent.futures
import json
from pathlib import Path
import urllib.error
import urllib.request
import numpy as np
import faiss
from eval_hybrid import request


def rejected(url,payload,statuses):
    try:
        request(url,payload)
    except urllib.error.HTTPError as exc:
        if exc.code not in statuses:
            raise AssertionError(f'expected {statuses}, got {exc.code}') from exc
    else:
        raise AssertionError('invalid request accepted')


def main():
    sidecar='http://127.0.0.1:8766'
    bridge='http://127.0.0.1:8765'
    index=faiss.read_index('indexes/vector-phase2/index.pq8.faiss')
    saved=np.load('indexes/vector-phase2/centroids.npy',allow_pickle=False)
    actual=faiss.vector_to_array(index.pq.centroids).reshape(saved.shape)
    if not np.array_equal(saved,actual) or index.pq.nbits!=8 or not index.is_trained:
        raise AssertionError('saved centroids do not match real trained PQ8')
    health=request(sidecar+'/health')
    if request(bridge+'/health')['corpus_sha256']!=health['corpus_sha256']:
        raise AssertionError('corpus mismatch')
    query='What are the risks of foreign currency exchange rates?'
    dense=request(sidecar+'/dense',dict(query=query,k=100,include_vector=True))
    if len(dense['hits'])!=100 or abs(np.linalg.norm(dense['query_vector'])-1)>1e-5:
        raise AssertionError('incorrect real query embedding/dense retrieval')
    ranked=request(bridge+'/search',dict(query=query,rerank=True))
    if len(ranked['hybrid'])!=50 or len(ranked['reranked'])!=10:
        raise AssertionError('candidate limits not satisfied')
    allowed={h['id'] for h in ranked['hybrid']}
    if not all(h['id'] in allowed for h in ranked['reranked']):
        raise AssertionError('reranker invented a passage ID')
    if any(not h.get('text') or h['metadata']['ID'] != h['id'] for h in ranked['reranked']):
        raise AssertionError('reranker did not return actual passage text/metadata')
    if len({h['score'] for h in ranked['reranked']})<2:
        raise AssertionError('cross-encoder returned constant scores')
    rejected(sidecar+'/rerank',dict(query=query,ids=['missing-real-id'],k=10),{404})
    rejected(sidecar+'/rerank',dict(query=query,ids=[ranked['hybrid'][0]['id']]*2,k=10),{422})
    rejected(sidecar+'/dense',dict(query='x'*40000,k=10),{413})
    rejected(sidecar+'/dense',dict(query='   ',k=10),{422})
    # Real concurrent calls, not replayed/constant transport responses.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        calls=list(pool.map(lambda unused:request(bridge+'/search',dict(query=query,rerank=False)),range(4)))
    if any(c['hybrid']!=ranked['hybrid'] for c in calls):
        raise AssertionError('concurrent retrieval changed ranking')
    report=dict(real_models=True,documents=index.ntotal,pq8=True,saved_centroids_match=True,
                query_vector_unit_norm=True,rrf_candidates=50,cross_encoder_results=10,
                actual_concurrent_queries=4,concurrent_rankings_identical=True,invalid_requests_rejected=True,
                health=health)
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
