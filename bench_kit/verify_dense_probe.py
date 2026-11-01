#!/usr/bin/env python3
"""Verify longer dense-only probes from actual CSV observations and concurrent rank checks."""
import argparse
import collections
import csv
import hashlib
import json
import math
from pathlib import Path


def read(path):return json.loads(path.read_text())


def verify(path):
    report=read(path/'load/report.json');actual=report['dense'];params=read(path/'parameters.json')
    with (path/'load/dense-requests.csv').open() as source:rows=list(csv.DictReader(source))
    if len(rows)!=params['requests'] or len(rows)!=actual['requests']:raise ValueError('probe count mismatch')
    if {int(row['request']) for row in rows}!=set(range(len(rows))):raise ValueError('duplicate/missing observation IDs')
    if any(row['status']!='200' or row['error'] for row in rows):raise ValueError('probe request failure')
    if dict(collections.Counter(row['status'] for row in rows))!=actual['statuses']:raise ValueError('status aggregation mismatch')
    values=sorted(int(row['elapsed_ns']) for row in rows)
    quantiles={name:values[math.ceil(p*len(values))-1] for name,p in [('p50_ns',.5),('p95_ns',.95),('p99_ns',.99)]}
    quantiles['max_ns']=values[-1]
    if actual['all_attempt_latency']!=quantiles or actual['successful_latency']!=quantiles:raise ValueError('probe percentile mismatch')
    if actual['successes']!=len(rows) or actual['failures'] or not math.isclose(len(rows)/actual['seconds'],actual['successful_qps'],rel_tol=1e-12):raise ValueError('probe success/QPS mismatch')
    quality=read(path/'concurrent-quality.json')
    if quality['queries']!=100 or quality['changed_top100_rankings'] or quality['max_shared_score_delta']>1e-5:raise ValueError('concurrent ranking contract changed')
    reference_path=Path(quality['reference'])
    if hashlib.sha256(reference_path.read_bytes()).hexdigest()!=quality['reference_sha256']:raise ValueError('reference rankings changed')
    with reference_path.open() as source:reference={r['id']:r['rankings']['dense'] for r in map(json.loads,source)}
    if {r['id'] for r in quality['rankings']}!=set(reference):raise ValueError('concurrent quality query coverage mismatch')
    for row in quality['rankings']:
        if [h['id'] for h in row['hits']]!=[h['id'] for h in reference[row['id']]]:raise ValueError('concurrent Top-100 IDs changed')
        old={h['id']:h['score'] for h in reference[row['id']]}
        if any(abs(h['score']-old[h['id']])>1e-5 for h in row['hits']):raise ValueError('dense score tolerance exceeded')
    if quality['query_sha256']!=report['query_sha256']:raise ValueError('query input changed')
    before=read(path/'health-before.json');after=read(path/'health-after.json');last=read(path/'health-after-quality.json')
    for health in (after,last):
        if health['corpus_sha256']!=before['corpus_sha256'] or health['admission']['active'] or health['admission']['rejected']:raise ValueError('health or admission mismatch')
        if health['inference_workers']!=4 or health['threads']!=2:raise ValueError('native CPU bound changed')
        batch=health['dense_batching']
        if batch['batch_size']!=params['dense_batch_size'] or batch['pending']:raise ValueError('dense batch queue did not drain')
        if batch['metrics']:
            if batch['metrics']['max_batch_seen']>params['dense_batch_size'] or batch['metrics']['rejected']:raise ValueError('batch cap/rejections changed')
    keys=[key for key in ('dense','dense_batch') if key in after['inference']]
    if sum(after['inference'][key]['requests'] for key in keys)!=len(rows)+5 or sum(last['inference'][key]['requests'] for key in keys)!=len(rows)+105:raise ValueError('logical request accounting mismatch')
    return dict(parameters=params,verified_observations=len(rows),all_successful=True,load=actual,
                concurrent_quality_queries=100,max_shared_score_delta=quality['max_shared_score_delta'],
                native_calls=sum(after['inference'][key]['count'] for key in keys),logical_requests=sum(after['inference'][key]['requests'] for key in keys),
                model_revision=after['encoder_revision'],corpus_sha256=after['corpus_sha256'])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--before',type=Path,required=True)
    p.add_argument('--after',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args();before,after=verify(args.before),verify(args.after)
    for key in ('inference_workers','threads_per_job','load_workers','requests','scenario','rerank_batch_size','dense_batch_wait_ms','dense_batch_trigger'):
        if before['parameters'][key]!=after['parameters'][key]:raise ValueError('probe configuration mismatch: '+key)
    if before['model_revision']!=after['model_revision'] or before['corpus_sha256']!=after['corpus_sha256']:raise ValueError('model/corpus changed')
    result=dict(before=before,after=after,dense_p99_reduction_percent=100*(1-after['load']['all_attempt_latency']['p99_ns']/before['load']['all_attempt_latency']['p99_ns']),
                dense_qps_ratio=after['load']['successful_qps']/before['load']['successful_qps'],
                note='Independent empirical percentile, count, status and QPS checks for finite dense-only probes; concurrent HTTP quality replays are separate from timing.')
    args.out.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))


if __name__=='__main__':main()
