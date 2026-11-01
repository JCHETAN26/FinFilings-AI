#!/usr/bin/env python3
"""Validate and decompose measured client latency and engine/sidecar timings."""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics


def summarize(values):
    values=sorted(values)
    return dict(mean=statistics.mean(values),p50=values[math.ceil(.5*len(values))-1],
                p95=values[math.ceil(.95*len(values))-1],p99=values[math.ceil(.99*len(values))-1],max=values[-1])


def analyze(directory):
    with (directory/'load/hybrid-requests.csv').open() as source:rows=list(csv.DictReader(source))
    samples={key:[] for key in ('client_seconds','outside_engine_seconds','engine_admission_seconds','retrieval_parallel_seconds',
                                'sparse_seconds','dense_roundtrip_seconds','fusion_seconds','rerank_roundtrip_seconds','engine_total_seconds','engine_other_seconds')}
    for row in rows:
        if row['status']!='200' or row['error']:raise ValueError('decomposition requires successful observations')
        elapsed=int(row['elapsed_ns'])/1e9
        data={k:float(row[k]) for k in ('engine_admission_seconds','sparse_seconds','dense_roundtrip_seconds','fusion_seconds','rerank_roundtrip_seconds','engine_total_seconds')}
        if any(not math.isfinite(x) or x<0 for x in data.values()):raise ValueError('invalid timing sample')
        retrieval=max(data['sparse_seconds'],data['dense_roundtrip_seconds'])
        other=data['engine_total_seconds']-data['engine_admission_seconds']-retrieval-data['fusion_seconds']-data['rerank_roundtrip_seconds']
        outside=elapsed-data['engine_total_seconds']
        if outside<0 or other<-.001:raise ValueError('inconsistent client/parallel stage timings')
        data.update(client_seconds=elapsed,outside_engine_seconds=outside,retrieval_parallel_seconds=retrieval,engine_other_seconds=other)
        for name,value in data.items():samples[name].append(value)
    means={k:statistics.mean(v) for k,v in samples.items()}
    components=('outside_engine_seconds','engine_admission_seconds','retrieval_parallel_seconds','fusion_seconds','rerank_roundtrip_seconds','engine_other_seconds')
    if not math.isclose(sum(means[k] for k in components),means['client_seconds'],abs_tol=1e-9):raise ValueError('decomposition does not conserve client time')
    before=json.loads((directory/'health-after-evaluation.json').read_text())['sidecar']['inference']
    after=json.loads((directory/'health-after.json').read_text())['sidecar']['inference']
    native={}
    for kind in ('dense','rank'):
        keys=[key for key in (('dense','dense_batch') if kind=='dense' else ('rank',)) if key in after]
        streams={}
        for key in keys:
            prior=before.get(key,{})
            count=after[key]['count']-prior.get('count',0)
            if count<=0:
                continue
            logical=after[key].get('requests',after[key]['count'])-prior.get('requests',prior.get('count',0))
            streams[key]=dict(calls=count,logical_requests=logical,
                work_seconds=after[key]['work_seconds']-prior.get('work_seconds',0.),
                wait_seconds=after[key]['wait_seconds']-prior.get('wait_seconds',0.))
        count=sum(v['calls'] for v in streams.values())
        logical=sum(v['logical_requests'] for v in streams.values())
        if count<=0:raise ValueError('missing load native calls')
        native[kind]=dict(calls=count,logical_requests=logical,mean_batch_size=logical/count,streams=streams,
            mean_work_seconds=sum(v['work_seconds'] for v in streams.values())/count,
            mean_wait_seconds=sum(v['wait_seconds'] for v in streams.values())/count)
        if kind=='rank':
            success_count=after[kind]['phase_count']-before[kind]['phase_count']
            if success_count!=count:raise ValueError('failed or missing profiled reranks')
            native[kind]['mean_phase_seconds']={name:(total-before[kind]['phase_totals'].get(name,0.))/count for name,total in after[kind]['phase_totals'].items()}
    return dict(observations=len(rows),seconds={k:summarize(v) for k,v in samples.items()},
                mean_client_time_fraction={k:means[k]/means['client_seconds'] for k in components},native_load=native,
                notes=['Outside-engine time includes TCP/HTTP handling, time awaiting application accept, client encode/decode and response transmission; it is not a pure network or socket-queue measurement.',
                       'Sparse/dense stages overlap: use their per-request maximum, not their sum. Mean components conserve mean client time; percentile components cannot be added.',
                       'Native load differences include five excluded timing warmups per endpoint. Rerank call count is 205; dense logical request count includes both hybrid and dense scenarios. With batching, native dense call counts and means describe batches, not individual request latency.',
                       'Very small negative engine_other values can arise from independently sampled monotonic spans; values below -1 ms are rejected and no values are clamped.'])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory',type=Path)
    args=p.parse_args();report=analyze(args.directory)
    (args.directory/'queueing-analysis.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
