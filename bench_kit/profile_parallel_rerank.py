#!/usr/bin/env python3
"""Profile real Top-50 reranking under bounded contention; never reads relevance labels."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import statistics
import sys
import time
import torch
sys.path.insert(0,str(Path('py').resolve()))
from rerank import Reranker


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--replay',type=Path,required=True)
    p.add_argument('--store',type=Path,required=True)
    p.add_argument('--onnx',type=Path)
    p.add_argument('--batch-size',type=int,required=True)
    p.add_argument('--workers',type=int,default=4)
    p.add_argument('--threads',type=int,default=2)
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    if not 1<=args.workers<=8 or not 1<=args.threads<=8 or not 1<=args.batch_size<=32:p.error('invalid bounded configuration')
    if args.out.exists():raise FileExistsError(args.out)
    torch.set_num_threads(args.threads)
    models=[Reranker('data/phase2-models.json',args.store,batch_size=args.batch_size,onnx_directory=args.onnx,threads=args.threads) for _ in range(args.workers)]
    slots=queue.Queue()
    for i in range(len(models)):slots.put(i)
    cases=[]
    with args.replay.open() as source:
        for line in source:
            cases.append(json.loads(line))
            if len(cases)==12:break
    if len(cases)!=12:raise ValueError('need twelve real query/candidate sets')
    def run(case):
        worker=slots.get()
        try:
            start=time.perf_counter();cpu=time.thread_time()
            hits=models[worker].rank(case['query'],[h['id'] for h in case['rankings']['hybrid']])
            return dict(id=case['id'],worker=worker,seconds=time.perf_counter()-start,caller_cpu_seconds=time.thread_time()-cpu,
                        work=models[worker].last_work,rankings=[dict(id=h['id'],score=h['score']) for h in hits])
        finally:slots.put(worker)
    # Touch every model, with real inputs, before the measured concurrent set.
    for model in models:model.rank(cases[0]['query'],[h['id'] for h in cases[0]['rankings']['hybrid'][:2]])
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        start=time.perf_counter();cpu=time.process_time()
        runs=list(pool.map(run,cases))
        seconds=time.perf_counter()-start;cpu_seconds=time.process_time()-cpu
    report=dict(backend=models[0].backend,workers=args.workers,threads_per_job=args.threads,batch_size=args.batch_size,
                queries=len(runs),seconds=seconds,qps=len(runs)/seconds,process_cpu_seconds=cpu_seconds,
                average_busy_cpu_cores=cpu_seconds/seconds,mean_native_seconds=statistics.mean(r['seconds'] for r in runs),
                cpu_affinity=sorted(os.sched_getaffinity(0)),runs=runs,
                note='First twelve saved actual Top-50 sets, no qrels or response cache; executor and replicas both capped. Native work includes contention; caller CPU excludes intra-op helper threads.')
    args.out.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({k:v for k,v in report.items() if k!='runs'},indent=2),flush=True)


if __name__=='__main__':main()
