#!/usr/bin/env python3
"""Independently compare real runtime benchmarks without changing their inputs."""
import argparse
import collections
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics


def read(path):return json.loads(path.read_text())


def verify(directory,data):
    quality=read(directory/'evaluation/report.json');load=read(directory/'load/report.json')
    provenance=quality['corpus_provenance']
    for name,key in [('chunks.jsonl','corpus_sha256'),('queries.jsonl','query_sha256'),('qrels.tsv','qrels_sha256')]:
        digest=hashlib.sha256()
        with (data/name).open('rb') as source:
            for block in iter(lambda:source.read(1<<20),b''):digest.update(block)
        if digest.hexdigest()!=provenance[key]:raise ValueError('input changed: '+name)
    gold={}
    with (data/'qrels.tsv').open() as source:
        for row in csv.DictReader(source,delimiter='\t'):
            relevance=int(row['score'])
            if relevance not in (0,1):raise ValueError('expected binary published judgments')
            gold.setdefault(row['query-id'],{})[row['corpus-id']]=relevance
    scores={name:[] for name in ('sparse','dense','hybrid','reranked')};rankings={}
    with (directory/'evaluation/per-query.jsonl').open() as source:
        for line in source:
            row=json.loads(line);identifier=row['id']
            if identifier in rankings or identifier not in gold:raise ValueError('unexpected or duplicate query')
            rankings[identifier]=row['rankings']
            ideal=sum(1/math.log2(i+2) for i in range(min(10,sum(gold[identifier].values()))))
            for name in scores:
                hits=row['rankings'][name]
                if len({h['id'] for h in hits})!=len(hits):raise ValueError('duplicate hit')
                dcg=sum(gold[identifier].get(h['id'],0)/math.log2(i+2) for i,h in enumerate(hits[:10]))
                scores[name].append(dcg/ideal if ideal else 0)
    if set(rankings)!=set(gold):raise ValueError('query coverage mismatch')
    computed={name:statistics.mean(v) for name,v in scores.items()}
    if any(not math.isclose(v,quality['ndcg_at_10'][k],rel_tol=0,abs_tol=1e-12) for k,v in computed.items()):raise ValueError('nDCG mismatch')
    checks={}
    for endpoint in ('hybrid','dense'):
        with (directory/f'load/{endpoint}-requests.csv').open() as source:rows=list(csv.DictReader(source))
        successful=[r for r in rows if r['status']=='200' and not r['error']]
        actual=load[endpoint]
        if len(rows)!=actual['requests'] or len(successful)!=actual['successes']:raise ValueError('request count mismatch')
        if dict(collections.Counter(r['status'] for r in rows))!=actual['statuses']:raise ValueError('HTTP status mismatch')
        for population,key in [(rows,'all_attempt_latency'),(successful,'successful_latency')]:
            values=sorted(int(r['elapsed_ns']) for r in population)
            expected={k:values[math.ceil(p*len(values))-1] for k,p in [('p50_ns',.5),('p95_ns',.95),('p99_ns',.99)]} if values else None
            if expected is not None:expected['max_ns']=values[-1]
            if expected!=actual[key]:raise ValueError('percentile mismatch')
        if not math.isclose(len(successful)/actual['seconds'],actual['successful_qps'],rel_tol=1e-12):raise ValueError('QPS mismatch')
        checks[endpoint]=dict(observations=len(rows),successes=len(successful),csv_percentiles_match=True)
    return quality,load,rankings,dict(ndcg_at_10=computed,queries=len(rankings),load=checks)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before',type=Path,required=True)
    parser.add_argument('--after',type=Path,required=True)
    parser.add_argument('--data',type=Path,required=True)
    args=parser.parse_args()
    b,bl,br,bc=verify(args.before,args.data);a,al,ar,ac=verify(args.after,args.data)
    if b['vector_manifest']!=a['vector_manifest']:raise ValueError('vector artifacts/config changed')
    changed={stage:sum([h['id'] for h in br[q][stage]]!=[h['id'] for h in ar[q][stage]] for q in br) for stage in ('sparse','dense','hybrid','reranked')}
    report=dict(before=str(args.before),after=str(args.after),independent_before=bc,independent_after=ac,
                changed_query_rankings=changed,ndcg_deltas={k:a['ndcg_at_10'][k]-b['ndcg_at_10'][k] for k in a['ndcg_at_10']},
                hybrid_successful_qps_ratio=al['hybrid']['successful_qps']/bl['hybrid']['successful_qps'],
                hybrid_p99_reduction_percent=100*(1-al['hybrid']['all_attempt_latency']['p99_ns']/bl['hybrid']['all_attempt_latency']['p99_ns']))
    (args.after/'independent-comparison.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
