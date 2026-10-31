#!/usr/bin/env python3
"""Run real local services and a finite 50-worker HTTP load; preserve every outcome."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import psutil


def main() -> None:
    root=Path(__file__).resolve().parents[1];os.chdir(root)
    output=root/'bench_kit/results/phase4'/time.strftime('run-%Y%m%dT%H%M%SZ',time.gmtime())
    output.mkdir(parents=True,exist_ok=False)
    (output.parent/'latest-run.json').write_text(json.dumps({'directory':str(output.relative_to(root))})+'\n')
    env=dict(os.environ,HF_HOME=str(root/'models/hf-cache'),OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='4',PYTHONPATH=os.pathsep.join([str(root/'py'),str(root/'models/runtime'),os.environ.get('PYTHONPATH','')]))
    commands=[('sidecar',[sys.executable,'-u','py/service.py']),('bridge',[str(root/'models/bin/finfilings'),'-mode','serve'])]
    services=[];logs=[];stop=threading.Event();sampler=None
    try:
        for name,command in commands:
            log=(output/(name+'.log')).open('w');logs.append(log)
            services.append(subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True))
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            if any(p.poll() is not None for p in services):raise RuntimeError('service exited; see service logs')
            try:
                health={}
                for port in (8765,8766):
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=2) as response:health[str(port)]=json.load(response)
                if health['8765']['corpus_sha256']!=health['8766']['corpus_sha256']:raise ValueError('corpus mismatch')
                (output/'health.json').write_text(json.dumps(health,indent=2)+'\n');break
            except OSError:time.sleep(.25)
        else:raise TimeoutError('services not ready')
        def sample() -> None:
            with (output/'memory-samples.jsonl').open('w') as file:
                while not stop.is_set():
                    record={'unix_seconds':time.time()}
                    for (name,_),process in zip(commands,services):
                        try:record[name+'_rss_bytes']=psutil.Process(process.pid).memory_info().rss
                        except psutil.NoSuchProcess:record[name+'_rss_bytes']=None
                    file.write(json.dumps(record)+'\n');file.flush();stop.wait(1)
        sampler=threading.Thread(target=sample);sampler.start()
        command=[str(root/'models/bin/loadtest'),'-workers','50','-requests','200','-output',str(output/'load')]
        print('Healthy real services; starting 200 requests per endpoint, 50 workers.',flush=True)
        with (output/'terminal.log').open('w') as log:
            process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            try:
                assert process.stdout is not None
                for line in process.stdout:print(line,end='',flush=True);log.write(line);log.flush()
                code=process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
            if code:raise RuntimeError(f'load tool exited {code}')
        final_health={}
        for port in (8765,8766):
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=5) as response:
                final_health[str(port)]=json.load(response)
        (output/'health-after.json').write_text(json.dumps(final_health,indent=2)+'\n')
        print('Saved '+str(output.relative_to(root)),flush=True)
    finally:
        stop.set()
        if sampler is not None:sampler.join(timeout=5)
        for process in reversed(services):
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
        for log in logs:log.close()


if __name__=='__main__':main()
