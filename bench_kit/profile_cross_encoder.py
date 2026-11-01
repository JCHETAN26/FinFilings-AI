#!/usr/bin/env python3
"""Compare bounded FP32 reranker batch widths on saved real candidates, without qrels."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
import torch
sys.path.insert(0, str(Path('py').resolve()))
from rerank import Reranker


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--replay',type=Path,required=True)
    p.add_argument('--store',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--onnx',type=Path)
    p.add_argument('--batches',type=int,nargs='+',default=[8,1,2,4,16])
    args=p.parse_args()
    if any(not 1 <= b <= 32 for b in args.batches):p.error('batches must be 1..32')
    torch.set_num_threads(2)
    reranker=Reranker('data/phase2-models.json',args.store,onnx_directory=args.onnx,threads=2)
    cases=[]
    with args.replay.open() as f:
        for line in f:
            cases.append(json.loads(line))
            if len(cases)==6: break
    results=[]
    for batch in args.batches:
        reranker.batch_size=batch
        reranker.rank(cases[0]['query'],[h['id'] for h in cases[0]['rankings']['hybrid']])
        runs=[]
        for case in cases:
            begin=time.perf_counter()
            hits=reranker.rank(case['query'],[h['id'] for h in case['rankings']['hybrid']])
            runs.append(dict(id=case['id'],seconds=time.perf_counter()-begin,work=reranker.last_work,
                             ids=[h['id'] for h in hits], scores=[h['score'] for h in hits]))
        results.append(dict(batch_size=batch,mean_seconds=statistics.mean(r['seconds'] for r in runs),runs=runs))
        print(batch,results[-1]['mean_seconds'],flush=True)
    args.out.write_text(json.dumps(dict(threads=2,backend=reranker.backend, attention=reranker.model.config._attn_implementation if reranker.backend == 'torch' else 'fused ONNX',
       method='first six saved actual Top-50 candidates; no qrels used; same FP32 weights and 512-token inputs',results=results),indent=2)+'\n')


if __name__=='__main__':main()
