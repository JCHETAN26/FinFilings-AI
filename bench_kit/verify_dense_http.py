#!/usr/bin/env python3
"""Verify real concurrent HTTP dense rankings against frozen serial retrieval evidence."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import urllib.request


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',required=True)
    p.add_argument('--queries',type=Path,required=True)
    p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--corpus-sha256',required=True)
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    if args.out.exists():raise FileExistsError(args.out)
    with args.queries.open() as source:questions=[json.loads(line) for line in source]
    if len(questions)!=100:raise ValueError('expected the fixed 100 queries')
    with args.reference.open() as source:reference={r['id']:r['rankings']['dense'] for r in map(json.loads,source)}
    if set(reference)!={q['id'] for q in questions}:raise ValueError('query ID mismatch')
    def request(q):
        body=json.dumps(dict(query=q['query'],k=100)).encode()
        req=urllib.request.Request(args.url+'/dense',data=body,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=120) as response:
            raw=response.read((1<<20)+1)
        if len(raw)>1<<20:raise ValueError('oversized dense response')
        value=json.loads(raw)
        if value['corpus_sha256']!=args.corpus_sha256:raise ValueError('corpus mismatch')
        hits=value['hits']
        if len(hits)!=100 or len({h['id'] for h in hits})!=100 or any(not math.isfinite(h['score']) for h in hits):raise ValueError('invalid actual dense ranking')
        old=reference[q['id']];same=[h['id'] for h in hits]==[h['id'] for h in old]
        scores={h['id']:h['score'] for h in old}
        delta=max((abs(h['score']-scores[h['id']]) for h in hits if h['id'] in scores),default=0.)
        return dict(id=q['id'],same_order=same,max_shared_score_delta=delta,hits=hits)
    with ThreadPoolExecutor(max_workers=50) as pool:rows=list(pool.map(request,questions))
    changed=[r['id'] for r in rows if not r['same_order']]
    report=dict(queries=100,http_workers=50,corpus_sha256=args.corpus_sha256,
                query_sha256=hashlib.sha256(args.queries.read_bytes()).hexdigest(),
                reference=str(args.reference),reference_sha256=hashlib.sha256(args.reference.read_bytes()).hexdigest(),
                changed_top100_rankings=changed,max_shared_score_delta=max(r['max_shared_score_delta'] for r in rows),rankings=rows,
                note='Actual concurrent /dense calls; no latency claims from this verification replay. Every Top-100 ID/order compared with saved serial retrieval.')
    args.out.write_text(json.dumps(report,indent=2)+'\n')
    if changed or report['max_shared_score_delta']>1e-5:raise ValueError('concurrent dense quality/score contract changed')
    print(json.dumps({k:v for k,v in report.items() if k!='rankings'},indent=2))


if __name__=='__main__':main()
