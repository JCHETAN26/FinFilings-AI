#!/usr/bin/env python3
"""Own a sidecar for a longer finite dense-only test; same bounded model and HTTP budget."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--index',type=Path,required=True)
    p.add_argument('--queries',type=Path,required=True)
    p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--onnx',type=Path,required=True)
    p.add_argument('--dense-batch-size',type=int,required=True)
    p.add_argument('--requests',type=int,default=2000)
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    if not 1<=args.dense_batch_size<=8 or not 50<=args.requests<=10000:p.error('invalid finite load bounds')
    root=Path(__file__).resolve().parents[1];os.chdir(root)
    spec=importlib.util.spec_from_file_location('benchmark_runtime',root/'scripts/benchmark-expanded.py')
    runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
    args.out.mkdir(parents=True,exist_ok=False)
    env=dict(os.environ,HF_HOME=str(root/'models/hf-cache'),HF_HUB_OFFLINE='1',
             PYTHONPATH=os.pathsep.join([str(root/'py'),os.environ.get('PYTHONPATH','')]),
             OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='2',NUMBA_NUM_THREADS='4')
    (args.out/'parameters.json').write_text(json.dumps(dict(inference_workers=4,threads_per_job=2,dense_batch_size=args.dense_batch_size,
        dense_batch_wait_ms=2,dense_batch_trigger=8,rerank_batch_size=1,load_workers=50,requests=args.requests,scenario='dense'),indent=2)+'\n')
    binary=(args.out/'load-client').resolve();process=None
    try:
        with (args.out/'build.log').open('w') as log:
            subprocess.run(['go','build','-o',str(binary),'./bench_kit/load'],env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
        url=f'http://127.0.0.1:{runtime.port()}'
        with (args.out/'sidecar.log').open('w') as log:
            process=subprocess.Popen([sys.executable,'-u','py/service.py','--index',str(args.index),'--reranker-onnx',str(args.onnx),
                '--inference-workers','4','--threads','2','--rerank-batch-size','1','--dense-batch-size',str(args.dense_batch_size),
                '--dense-batch-trigger','8','--port',url.rsplit(':',1)[1]],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            until=time.monotonic()+120
            while True:
                if process.poll() is not None:raise RuntimeError('sidecar exited during startup')
                try:
                    health=runtime.health(url)
                    if health['corpus_sha256']!=json.loads((args.index/'manifest.json').read_text())['corpus_sha256']:
                        raise ValueError('corpus fingerprint mismatch')
                    break
                except (OSError,TimeoutError):
                    if time.monotonic()>=until:raise TimeoutError('sidecar startup timeout')
                    time.sleep(.25)
            (args.out/'health-before.json').write_text(json.dumps(health,indent=2)+'\n')
            with (args.out/'load.log').open('w') as load_log:
                subprocess.run([str(binary),'-scenario','dense','-workers','50','-requests',str(args.requests),'-queries',str(args.queries),
                    '-dense',url+'/dense','-output',str(args.out/'load')],env=env,stdout=load_log,stderr=subprocess.STDOUT,check=True,timeout=180)
            (args.out/'health-after.json').write_text(json.dumps(runtime.health(url),indent=2)+'\n')
            with (args.out/'concurrent-quality.log').open('w') as quality_log:
                subprocess.run([sys.executable,'bench_kit/verify_dense_http.py','--url',url,'--queries',str(args.queries),
                    '--reference',str(args.reference),'--corpus-sha256',health['corpus_sha256'],
                    '--out',str(args.out/'concurrent-quality.json')],env=env,stdout=quality_log,stderr=subprocess.STDOUT,check=True,timeout=180)
            (args.out/'health-after-quality.json').write_text(json.dumps(runtime.health(url),indent=2)+'\n')
            runtime.stop(process)
        print((args.out/'load/report.json').read_text(),flush=True)
    finally:
        if process is not None:runtime.stop(process)
        binary.unlink(missing_ok=True)


if __name__=='__main__':main()
