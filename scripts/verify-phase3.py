#!/usr/bin/env python3
"""Own both real retrieval services for the entire live agent evaluation."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request


def main() -> None:
    root=Path(__file__).resolve().parents[1]
    os.chdir(root)
    output=root/'bench_kit/results/phase3'
    output.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,HF_HOME=os.environ.get('HF_HOME',str(root/'models/hf-cache')),
        OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='4',
        PYTHONPATH=os.pathsep.join([str(root/'py'), os.environ.get('PYTHONPATH','')]))
    commands=[('sidecar-final',[sys.executable,'-u','py/service.py','--port','8766']),
              ('bridge-final',[os.environ.get('FINFILINGS_GO_BINARY',str(root/'models/bin/finfilings')),'-mode','serve'])]
    processes=[]
    files=[]
    try:
        for name,command in commands:
            log=(output/(name+'.log')).open('w')
            files.append(log)
            processes.append(subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True))
        deadline=time.monotonic()+90
        health={}
        while time.monotonic()<deadline:
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError(f'retrieval service exited during startup: {process.returncode}')
            try:
                for port in (8765,8766):
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=2) as response:
                        health[str(port)]=json.load(response)
                if health['8765']['corpus_sha256']!=health['8766']['corpus_sha256']:
                    raise ValueError('sparse/dense corpus mismatch')
                break
            except (OSError,TimeoutError):
                time.sleep(.25)
        else:
            raise TimeoutError('local retrieval services did not become ready')
        (output/'health.json').write_text(json.dumps(health,indent=2)+'\n')
        print('Both real retrieval services healthy; starting ten audit prompts.',flush=True)
        run_directory=output/time.strftime('evaluation-%Y%m%dT%H%M%SZ',time.gmtime())
        (output/'latest-run.json').write_text(json.dumps({'directory':str(run_directory.relative_to(root))})+'\n')
        with (output/'evaluation-final.log').open('w') as log:
            evaluation=subprocess.Popen([sys.executable,'-u','bench_kit/eval_agent.py','--output',str(run_directory)],env=env,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            try:
                assert evaluation.stdout is not None
                for line in evaluation.stdout:
                    log.write(line);log.flush()
                    print(line,end='',flush=True)
                code=evaluation.wait()
            finally:
                if evaluation.poll() is None:
                    evaluation.terminate()
                    try: evaluation.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        evaluation.kill();evaluation.wait()
            if code:
                raise RuntimeError(f'agent evaluation exited {code}; see evaluation-final.log')
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait()
        for log in files: log.close()


if __name__=='__main__': main()
