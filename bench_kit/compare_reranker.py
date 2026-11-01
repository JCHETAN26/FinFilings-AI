#!/usr/bin/env python3
"""Independently verify a bounded reranker optimization against frozen retrieval evidence."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from compare_runtime import verify, read


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--before',type=Path,required=True)
    ap.add_argument('--after',type=Path,required=True)
    ap.add_argument('--data',type=Path,required=True)
    ap.add_argument('--onnx',type=Path,required=True)
    ap.add_argument('--allow-worker-rebalance',action='store_true',help='permit worker/thread changes only within the same nominal CPU budget and eight-worker cap')
    args=ap.parse_args()
    b,bl,br,bc=verify(args.before,args.data)
    a,al,ar,ac=verify(args.after,args.data)
    if b['vector_manifest']!=a['vector_manifest']:
        raise ValueError('vector artifact/config changed')
    bp,aparam=read(args.before/'parameters.json'),read(args.after/'parameters.json')
    fixed_keys=('configured_intraop_thread_budget','load_workers','requests_per_endpoint','queries','candidate_limit','max_tokens')
    if not args.allow_worker_rebalance:fixed_keys+=('inference_workers','threads_per_job')
    for key in fixed_keys:
        if bp[key]!=aparam[key]:raise ValueError('changed concurrency or retrieval budget: '+key)
    changed={stage:sum(br[q][stage]!=ar[q][stage] for q in br) for stage in ('sparse','dense','hybrid')}
    if any(changed.values()):raise ValueError('upstream retrieval changed')
    gold={}
    with (args.data/'qrels.tsv').open() as source:
        for row in csv.DictReader(source,delimiter='\t'):
            gold.setdefault(row['query-id'],{})[row['corpus-id']]=int(row['score'])
    per_query_quality_changes=[]
    changed_top10=0;max_delta=0.
    for q in br:
        old,new=br[q]['reranked'],ar[q]['reranked']
        ideal=sum(1/math.log2(i+2) for i in range(min(10,sum(gold[q].values()))))
        def ndcg(hits):
            return sum(gold[q].get(h['id'],0)/math.log2(i+2) for i,h in enumerate(hits[:10]))/ideal if ideal else 0.
        if not math.isclose(ndcg(old),ndcg(new),abs_tol=1e-12):
            per_query_quality_changes.append(dict(id=q,before=ndcg(old),after=ndcg(new)))
        changed_top10 += [h['id'] for h in old]!=[h['id'] for h in new]
        scores={h['id']:h['score'] for h in old}
        max_delta=max(max_delta,max((abs(h['score']-scores[h['id']]) for h in new if h['id'] in scores),default=0.))
        old_by_id={h['id']:h for h in old}
        for hit in new:
            if hit['id'] in old_by_id:
                for field in ('text','metadata'):
                    if hit.get(field)!=old_by_id[hit['id']].get(field):raise ValueError('passage content changed')
    if a['ndcg_at_10']['reranked']<b['ndcg_at_10']['reranked']-1e-12:
        raise ValueError('reranked nDCG regressed')
    if not math.isclose(a['pq_recall_at_10_vs_exact_float32'],b['pq_recall_at_10_vs_exact_float32'],abs_tol=1e-12):
        raise ValueError('PQ recall changed')
    bh,ah=read(args.before/'health-after.json')['sidecar'],read(args.after/'health-after.json')['sidecar']
    for key in ('encoder_revision','reranker_revision'):
        if bh[key]!=ah[key]:raise ValueError('runtime/model contract changed')
    for health,parameters in ((bh,bp),(ah,aparam)):
        if (not 1 <= health['inference_workers'] <= 8 or health['inference_workers']!=parameters['inference_workers']
                or health['threads']!=parameters['threads_per_job']
                or health['inference_workers']*health['threads']!=parameters['configured_intraop_thread_budget']):
            raise ValueError('worker/thread contract mismatch')
    if bh['admission']['limit']!=ah['admission']['limit'] or ah['admission']['peak']>ah['admission']['limit'] or ah['admission']['active']:
        raise ValueError('admission bound changed or workers did not drain')
    graph_manifest=read(args.onnx/'manifest.json')
    digest=hashlib.sha256()
    with (args.onnx/'model.onnx').open('rb') as source:
        for block in iter(lambda:source.read(1<<20),b''):digest.update(block)
    if digest.hexdigest()!=graph_manifest['files']['model.onnx'] or digest.hexdigest()!=ah['reranker_artifact_sha256']:
        raise ValueError('served ONNX graph hash mismatch')
    lock=read(Path('data/phase2-models.json'))['cross-encoder/ms-marco-MiniLM-L6-v2']
    if graph_manifest['dtype']!='float32' or graph_manifest['weights_sha256']!=lock['weights_sha256'] or graph_manifest['model_revision']!=ah['reranker_revision']:
        raise ValueError('model provenance/precision mismatch')
    timing={}
    for name,h in [('before',bh),('after',ah)]:
        timing[name]={kind:dict(count=record['count'],mean_native_work_seconds=record['work_seconds']/record['count'],
                               mean_wait_seconds=record['wait_seconds']/record['count']) for kind,record in h['inference'].items()}
    report=dict(before=str(args.before),after=str(args.after),before_checks=bc,after_checks=ac,
                unchanged_upstream_scores_and_rankings=True,unchanged_vector_artifacts=True,
                unchanged_nominal_cpu_and_candidate_budgets=True,
                worker_rebalance=dict(before_workers=bh["inference_workers"],after_workers=ah["inference_workers"],before_threads=bh["threads"],after_threads=ah["threads"]),changed_top10_rankings=changed_top10,
                max_shared_top10_score_absolute_delta=max_delta,per_query_ndcg_changes=per_query_quality_changes,ndcg_before=b['ndcg_at_10'],ndcg_after=a['ndcg_at_10'],
                serial_query_before=b['end_to_end_serial_query_seconds'],serial_query_after=a['end_to_end_serial_query_seconds'],
                before_load=bl,after_load=al,native_timing=timing,onnx_manifest=graph_manifest,
                hybrid_qps_ratio=al['hybrid']['successful_qps']/bl['hybrid']['successful_qps'],
                hybrid_p99_reduction_percent=100*(1-al['hybrid']['all_attempt_latency']['p99_ns']/bl['hybrid']['all_attempt_latency']['p99_ns']))
    (args.after/'independent-comparison.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('before_load','after_load')},indent=2))


if __name__=='__main__':main()
