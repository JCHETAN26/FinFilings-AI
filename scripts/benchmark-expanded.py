#!/usr/bin/env python3
"""Supervise real retrieval services for a frozen-query expanded-corpus benchmark."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request


def port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        return listener.getsockname()[1]


def health(base):
    with urllib.request.urlopen(base+'/health',timeout=3) as response:return json.load(response)


def stop(process):
    if process.poll() is None:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:return
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--vectors',type=Path,required=True)
    parser.add_argument('--sparse',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--inference-workers',type=int,default=1)
    parser.add_argument('--threads',type=int,default=4,help='PyTorch/FAISS CPU threads per inference job (1..32)')
    parser.add_argument('--reranker-onnx',type=Path)
    parser.add_argument('--rerank-batch-size',type=int,help='default: 1 for ONNX, 8 for PyTorch')
    parser.add_argument('--dense-batch-size',type=int,default=1)
    parser.add_argument('--dense-batch-wait-ms',type=float,default=2.)
    parser.add_argument('--dense-batch-trigger',type=int)
    args=parser.parse_args()
    if args.dense_batch_trigger is None:args.dense_batch_trigger=2*args.inference_workers
    if not 0 <= args.dense_batch_trigger <= 64:parser.error('dense batch trigger must be 0..64')
    if not 1 <= args.dense_batch_size <= 8 or not 0 <= args.dense_batch_wait_ms <= 10:parser.error('invalid dense batching bounds')
    if args.rerank_batch_size is None:args.rerank_batch_size=1 if args.reranker_onnx else 8
    if not 1 <= args.rerank_batch_size <= 32:parser.error('rerank batch size must be 1..32')
    if not 1 <= args.inference_workers <= 8 or not 1 <= args.threads <= 32:
        parser.error('require 1..8 inference workers and 1..32 CPU threads')
    root=Path(__file__).resolve().parents[1];os.chdir(root)
    args.out.mkdir(parents=True,exist_ok=False)
    env=dict(os.environ,HF_HOME=str(root/'models/hf-cache'),HF_HUB_OFFLINE='1',
             PYTHONPATH=os.pathsep.join([str(root/'py'),os.environ.get('PYTHONPATH','')]),
             OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS=str(args.threads),NUMBA_NUM_THREADS='4')
    (args.out/'parameters.json').write_text(json.dumps(dict(inference_workers=args.inference_workers,threads_per_job=args.threads,configured_intraop_thread_budget=args.inference_workers*args.threads,load_workers=50,requests_per_endpoint=200,queries=100,candidate_limit=50,max_tokens=512,reranker_backend='onnx-fp32' if args.reranker_onnx else 'torch',rerank_batch_size=args.rerank_batch_size,dense_batch_size=args.dense_batch_size,dense_batch_wait_ms=args.dense_batch_wait_ms,dense_batch_trigger=args.dense_batch_trigger),indent=2)+'\n')
    def run(name,command,timeout=1800):
        print(name,flush=True)
        with (args.out/(name+'.log')).open('w') as log:
            process=subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try:
                result=process.wait(timeout=timeout)
                if result:raise RuntimeError(f'{name} exited {result}; inspect log')
            finally:stop(process)
    bridge=(args.out/'bridge').resolve();load=(args.out/'load-client').resolve()
    run('build-bridge',['go','build','-o',str(bridge),'./cmd/finfilings'])
    run('build-load',['go','build','-o',str(load),'./bench_kit/load'])
    sidecar_port,bridge_port=port(),port()
    while bridge_port==sidecar_port:bridge_port=port()
    sidecar=f'http://127.0.0.1:{sidecar_port}';bridge_url=f'http://127.0.0.1:{bridge_port}'
    commands=[('sidecar',[sys.executable,'-u','py/service.py','--index',str(args.vectors),'--port',str(sidecar_port),'--inference-workers',str(args.inference_workers),'--threads',str(args.threads),'--rerank-batch-size',str(args.rerank_batch_size),'--dense-batch-size',str(args.dense_batch_size),'--dense-batch-wait-ms',str(args.dense_batch_wait_ms),'--dense-batch-trigger',str(args.dense_batch_trigger)]+(['--reranker-onnx',str(args.reranker_onnx)] if args.reranker_onnx else [])),
              ('bridge',[str(bridge),'-mode','serve','-index',str(args.sparse),'-sidecar',sidecar,'-listen',f'127.0.0.1:{bridge_port}'])]
    processes=[];logs=[]
    try:
        for name,command in commands:
            log=(args.out/(name+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True))
        until=time.monotonic()+120
        while True:
            if any(p.poll() is not None for p in processes):raise RuntimeError('service exited during startup')
            try:
                status={'sidecar':health(sidecar),'bridge':health(bridge_url)}
                expected=json.loads((args.data/'provenance.json').read_text())['corpus_sha256']
                if any(s['corpus_sha256']!=expected for s in status.values()):raise ValueError('corpus fingerprint mismatch')
                break
            except (OSError,TimeoutError):
                if time.monotonic()>=until:raise TimeoutError('services did not become ready')
                time.sleep(.25)
        (args.out/'health-before.json').write_text(json.dumps(status,indent=2)+'\n')
        run('evaluation',[sys.executable,'-u','bench_kit/eval_hybrid.py','--data',str(args.data),'--index',str(args.vectors),
                          '--bridge',bridge_url,'--sidecar',sidecar,'--out',str(args.out/'evaluation')])
        (args.out/'health-after-evaluation.json').write_text(json.dumps({'sidecar':health(sidecar),'bridge':health(bridge_url)},indent=2)+'\n')
        run('load',[str(load),'-workers','50','-requests','200','-queries',str(args.data/'queries.jsonl'),
                    '-search',bridge_url+'/search','-dense',sidecar+'/dense','-output',str(args.out/'load')])
        (args.out/'health-after.json').write_text(json.dumps({'sidecar':health(sidecar),'bridge':health(bridge_url)},indent=2)+'\n')
    finally:
        for process in reversed(processes):stop(process)
        for log in logs:log.close()
        # Keep build outputs out of the reviewable evidence tree.
        bridge.unlink(missing_ok=True);load.unlink(missing_ok=True)


if __name__=='__main__':main()
