#!/usr/bin/env python3
"""Collect a real ORT operator trace separately from uninstrumented latency measurements."""
import argparse
import collections
import gzip
import json
from pathlib import Path
import sys
import onnxruntime as ort
sys.path.insert(0,str(Path('py').resolve()))
from rerank import Reranker


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--replay',type=Path,required=True)
    p.add_argument('--store',type=Path,required=True)
    p.add_argument('--onnx',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args();args.out.mkdir(parents=True,exist_ok=False)
    with args.replay.open() as source:case=json.loads(next(source))
    model=Reranker('data/phase2-models.json',args.store,batch_size=2,onnx_directory=args.onnx,threads=2)
    options=model.session.get_session_options();options.enable_profiling=True
    options.profile_file_prefix=str((args.out/'trace').resolve())
    model.session=ort.InferenceSession(str(args.onnx/'model.onnx'),options,providers=['CPUExecutionProvider'])
    hits=model.rank(case['query'],[h['id'] for h in case['rankings']['hybrid']])
    trace=Path(model.session.end_profiling());events=json.loads(trace.read_text())
    operators=collections.defaultdict(lambda:dict(calls=0,total_kernel_us=0.))
    groups=collections.defaultdict(float)
    for event in events:
        if event.get('cat')!='Node' or not event.get('name','').endswith('_kernel_time'):continue
        name=event.get('args',{}).get('op_name','unknown');value=float(event['dur'])
        operators[name]['calls']+=1;operators[name]['total_kernel_us']+=value
        node=event['name']
        if '/intermediate/' in node or ('/output/dense/' in node and '/attention/' not in node):group='feed_forward'
        elif name=='Attention' or '/attention/' in node:group='attention'
        else:group='other'
        groups[group]+=value
    with gzip.GzipFile(filename=str(args.out/'operator-trace.json.gz'),mode='wb',mtime=0) as sink:sink.write(trace.read_bytes())
    trace.unlink()
    report=dict(id=case['id'],work=model.last_work,operators=dict(sorted(operators.items(),key=lambda x:-x[1]['total_kernel_us'])),
                node_groups_kernel_us=dict(groups),top10_ids=[h['id'] for h in hits],
                note='Single actual Top-50 call, CPU FP32, batch two, two threads. ORT Node event durations are wall-clock microseconds, not summed CPU thread time. Tracing adds overhead; this is attribution, not the latency benchmark.')
    (args.out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
