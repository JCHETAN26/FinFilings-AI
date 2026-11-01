#!/usr/bin/env python3
"""Real bounded dense-batch CPU replay and rank verification; no relevance-label tuning."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import sys
import time
sys.path.insert(0,str(Path('py').resolve()))
from service import Retrieval, DenseRequest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--index',type=Path,required=True)
    p.add_argument('--queries',type=Path,required=True)
    p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    if args.out.exists():raise FileExistsError(args.out)
    retrieval=Retrieval(args.index,'data/phase2-models.json',2,4,'models/reranker-onnx-fp32-optimized-20261031',1)
    cases=[]
    with args.queries.open() as source:
        for line in source:
            cases.append(json.loads(line))
            if len(cases)>100:raise ValueError('100-query profile bound exceeded')
    with args.reference.open() as source:
        baseline={row['id']:row['rankings']['dense'] for row in map(json.loads,source)}
    free=queue.Queue()
    for i in range(4):free.put(i)
    def run(group):
        worker=free.get()
        try:
            return retrieval.dense_batch([DenseRequest(query=q['query'],k=100) for q in group],worker)
        finally:free.put(worker)
    results=[]
    for size in (1,2,4,8):
        run(cases[:size])
        groups=[cases[start:start+size] for start in range(0,len(cases),size)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            began=time.perf_counter();cpu=time.process_time();chunks=list(pool.map(run,groups))
            elapsed=time.perf_counter()-began;cpu_seconds=time.process_time()-cpu
        changed=[];max_delta=0.;records=[]
        for q,response in zip(cases,[r for group in chunks for r in group]):
            original=baseline[q['id']];hits=response['hits']
            if [h['id'] for h in original]!=[h['id'] for h in hits]:changed.append(q['id'])
            scores={h['id']:h['score'] for h in original}
            max_delta=max(max_delta,max((abs(h['score']-scores[h['id']]) for h in hits if h['id'] in scores),default=0.))
            records.append(dict(id=q['id'],hits=hits))
        result=dict(batch_size=size,queries=len(cases),workers=4,threads=2,seconds=elapsed,qps=len(cases)/elapsed,
                    process_cpu_seconds=cpu_seconds,cpu_seconds_per_query=cpu_seconds/len(cases),
                    changed_top100_rankings=changed,max_shared_score_delta=max_delta,rankings=records)
        results.append(result);print(json.dumps({k:v for k,v in result.items() if k!='rankings'}),flush=True)
    args.out.write_text(json.dumps(dict(results=results,note='Fixed real 100-query replay, four native jobs maximum, same float32 BGE and PQ128. No qrels used for performance selection; all Top-100 IDs compared with saved serial retrieval.'),indent=2)+'\n')


if __name__=='__main__':main()
