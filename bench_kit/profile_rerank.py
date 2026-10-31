#!/usr/bin/env python3
"""Real checkpoint/candidate replay: timing and quality regression against saved Phase 2 ranks."""
import json
from pathlib import Path
import sys
import time
import torch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'py'))
from rerank import Reranker
from ranx import Qrels, Run, evaluate


def main() -> None:
    torch.set_num_threads(4)
    output=Path('bench_kit/results/optimization');output.mkdir(parents=True,exist_ok=True)
    reranker=Reranker('data/phase2-models.json','indexes/vector-phase2/passages.sqlite')
    qrels={}
    with Path('data/phase2/qrels.tsv').open() as source:
        for line in source:
            fields=line.split()
            if fields == ['query-id','corpus-id','score']:
                continue
            if len(fields)==4:qid,_,doc,grade=fields
            elif len(fields)==3:qid,doc,grade=fields
            else:raise ValueError('invalid qrel')
            qrels.setdefault(qid,{})[doc]=int(grade)
    old_run,new_run={},{};seconds=[];changes=0;score_delta=0.
    with Path('bench_kit/results/phase2/evaluation/per-query.jsonl').open() as source, (output/'rerank-replay.jsonl').open('w') as sink:
        for line in source:
            case=json.loads(line)
            before=case['rankings']['reranked']
            start=time.perf_counter()
            after=reranker.rank(case['query'],[h['id'] for h in case['rankings']['hybrid']])
            elapsed=time.perf_counter()-start;seconds.append(elapsed)
            old_ids=[h['id'] for h in before];new_ids=[h['id'] for h in after]
            changes+=old_ids!=new_ids
            old_scores={h['id']:h['score'] for h in before}
            score_delta=max(score_delta,max((abs(h['score']-old_scores[h['id']]) for h in after if h['id'] in old_scores),default=0.))
            old_run[case['id']]={id:float(10-i) for i,id in enumerate(old_ids)}
            new_run[case['id']]={id:float(10-i) for i,id in enumerate(new_ids)}
            sink.write(json.dumps(dict(id=case['id'],seconds=elapsed,work=reranker.last_work,
                rankings_identical=old_ids==new_ids,reranked=[dict(id=h['id'],score=h['score']) for h in after]))+'\n');sink.flush()
    report=dict(queries=len(seconds),model_revision=reranker.lock['revision'],
        changed_top10_rankings=changes,max_shared_top10_score_absolute_delta=score_delta,
        original_ndcg10=float(evaluate(Qrels(qrels),Run(old_run),'ndcg@10')),
        optimized_ndcg10=float(evaluate(Qrels(qrels),Run(new_run),'ndcg@10')),
        total_rerank_seconds=sum(seconds),mean_rerank_seconds=sum(seconds)/len(seconds),
        note='Actual CPU model inference on the same 100 saved RRF Top-50 candidate sets. No retrieval quality labels used for tuning. Not a fresh end-to-end retrieval evaluation.')
    (output/'rerank-quality.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2),flush=True)
    if report['optimized_ndcg10']<report['original_ndcg10']-1e-12:raise SystemExit('retrieval quality regressed')


if __name__=='__main__':main()
