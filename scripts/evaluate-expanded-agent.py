#!/usr/bin/env python3
"""Enroll acquired SEC provenance and supervise live expanded-corpus agent audits.

Default: the ten historical regression audits (unchanged behaviour).
`--suite FILE`: the frozen 30-case broad validation suite (see bench_kit/build_broad_suite.py). The suite and its
hash are saved to the fresh output directory, and corpus/index/model/source/code hashes are recorded, before the
first request. Owned services are always stopped. No data is fetched and no credentials are touched.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import hashlib


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1<<20),b''):h.update(block)
    return h.hexdigest()


def tree_hashes(directory):
    return {str(p):digest(p) for p in sorted(directory.rglob('*')) if p.is_file()}


# Defaults reproduce the historical v2 run exactly; every artifact path can be redirected for a new corpus version.
PATH_DEFAULTS=dict(corpus_dir='data/sec-scale-v2-evaluation-20261031',ingest_report='indexes/sec-scale-v2-20261031/report.json',
    original_provenance='data/sec/provenance.json',vector_index='indexes/vector-sec-scale-v2-pq128-20261031',
    sparse_index='indexes/sparse-sec-scale-v2-20261031',reranker_model='models/reranker-onnx-fp32-optimized-20261031')


def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    for name,default in PATH_DEFAULTS.items():
        parser.add_argument('--'+name.replace('_','-'),type=Path,default=Path(default),help=f'default: {default}')
    parser.add_argument('--audit-id', choices=[f'audit{i:02}' for i in range(1,11)],help='Historical mode: run one regression audit')
    parser.add_argument('--suite',type=Path,help='Frozen broad validation suite JSON; runs every case unless --suite-case is given')
    parser.add_argument('--expected-suite-sha256',help='Pin the exact suite bytes; mismatch aborts before any service starts')
    parser.add_argument('--suite-case',action='append',help='Partial suite run (repeatable); the offline verifier then needs --allow-subset')
    parser.add_argument('--diagnose-only',action='store_true')
    parser.add_argument('--rank-diagnostic',action='store_true',help='Start the bounded services, replay saved queries via bench_kit/diagnose_ranks.py (no agent/LLM calls), stop')
    parser.add_argument('--replay',type=Path,action='append',default=[],help='saved raw case JSON for --rank-diagnostic (repeatable)')
    parser.add_argument('--replay-subquery',type=int)
    parser.add_argument('--review-target',action='append',default=[],help='REVIEW-ONLY passage ID reported by --rank-diagnostic; never sent to retrieval')
    return parser


def main(argv=None):
    parser=build_parser()
    args=parser.parse_args(argv)
    if args.rank_diagnostic and (args.suite or args.audit_id or args.diagnose_only or not args.replay):
        parser.error('--rank-diagnostic needs --replay and excludes --suite/--audit-id/--diagnose-only')
    if args.suite and (args.audit_id or args.diagnose_only):
        parser.error('--suite cannot be combined with --audit-id/--diagnose-only')
    if args.suite_case and not args.suite:
        parser.error('--suite-case requires --suite')
    root=Path(__file__).resolve().parents[1];os.chdir(root)
    sys.path.insert(0,str(root/'bench_kit'))
    import agent_suite
    spec=importlib.util.spec_from_file_location('benchmark',root/'scripts/benchmark-expanded.py')
    support=importlib.util.module_from_spec(spec);spec.loader.exec_module(support)
    data=args.corpus_dir
    meta=json.loads((data/'provenance.json').read_text())
    original=args.original_provenance;report=args.ingest_report
    if digest(report)!=meta['new_sec_report_sha256'] or digest(data/'chunks.jsonl')!=meta['corpus_sha256']:
        raise ValueError('expanded corpus/report checksum mismatch')
    baseline=json.loads(original.read_text())
    if digest(Path('data/sec/chunks.jsonl'))!=baseline['corpus_sha256'] or baseline['corpus_sha256']!=meta['sec_sha256']:
        raise ValueError('original SEC provenance checksum mismatch')
    suite=suite_digest=None
    if args.suite:  # Validate and pin the frozen input before creating anything.
        suite,suite_digest=agent_suite.load_suite(args.suite,args.expected_suite_sha256)
        if suite['corpus_sha256']!=meta['corpus_sha256']:
            raise ValueError('suite was frozen against a different corpus')
    lineage,lineage_problems=agent_suite.check_report_lineage(report,root)
    if lineage_problems:raise ValueError('ingestion report lineage invalid: '+'; '.join(lineage_problems))
    sources=agent_suite.build_trusted_sources(original,report)
    args.out.mkdir(parents=True,exist_ok=False)
    status=dict(status='started',started_utc=datetime.now(timezone.utc).isoformat(),mode='suite' if args.suite else 'rank-diagnostic' if args.rank_diagnostic else 'historical',argv=sys.argv)
    status_path=args.out/'run-status.json'
    status_path.write_text(json.dumps(status,indent=2)+'\n')
    if args.suite:  # Saved input suite + hash precede the first request (and the first service start).
        (args.out/'suite.json').write_bytes(args.suite.read_bytes())
        (args.out/'suite.sha256').write_text(f'{suite_digest}  suite.json\n')
    provenance=args.out/'agent-provenance.json'
    provenance.write_text(json.dumps(dict(corpus_sha256=meta['corpus_sha256'],sources=sources,source_manifests=[dict(path=str(p),sha256=digest(p)) for p in [original,report]]),indent=2)+'\n')
    env=dict(os.environ,HF_HOME=str(root/'models/hf-cache'),HF_HUB_OFFLINE='1',PYTHONPATH=os.pathsep.join([str(root/'models/runtime'),str(root/'py')]),OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='2',NUMBA_NUM_THREADS='4',GOCACHE='/tmp/finfilings-go-cache',CCACHE_DIR='/tmp/finfilings-ccache',CCACHE_TEMPDIR='/tmp/finfilings-ccache-tmp')
    vector_index,model_dir,sparse_index=args.vector_index,args.reranker_model,args.sparse_index
    if args.suite:
        code_files=[Path(f) for f in agent_suite.code_files(root)]
        head=subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True).stdout.strip()
        dirty=subprocess.run(['git','status','--porcelain'],capture_output=True,text=True).stdout.splitlines()
        (args.out/'artifact-hashes.json').write_text(json.dumps(dict(
            suite_sha256=suite_digest,corpus_sha256=meta['corpus_sha256'],
            ingest_report_sha256=digest(report),original_provenance_sha256=digest(original),
            previous_ingest_report_sha256=lineage.get('previous_report_sha256'),ingest_lineage=lineage,
            artifact_paths={k:str(getattr(args,k)) for k in PATH_DEFAULTS},
            enrolled_source_manifest_sha256=digest(provenance),enrolled_filings=len(sources),
            vector_index=tree_hashes(vector_index),reranker_model=tree_hashes(model_dir),sparse_index=tree_hashes(sparse_index),
            code={str(p):digest(p) for p in code_files},git_head=head,git_uncommitted_paths=len(dirty),
            paths_relative_to_repository_root=True,note='Hashes are taken before the first request. Raw filing HTML for parser-v2 acquisitions is not stored locally; its sha256 values come from the ingestion report. In a mixed-lineage report only the targeted filing was reparsed; see ingest_lineage.'),indent=2)+'\n')
    bridge=(args.out/'bridge').resolve()
    processes=[];logs=[]
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(143))  # Make `finally` cleanup run on termination too.
    code=None
    try:
        subprocess.run(['go','build','-o',str(bridge),'./cmd/finfilings'],env=env,check=True)
        sp,bp=support.port(),support.port()
        while bp==sp:bp=support.port()
        sidecar=f'http://127.0.0.1:{sp}';backend=f'http://127.0.0.1:{bp}'
        commands=[('sidecar',[sys.executable,'-u','py/service.py','--index',str(vector_index),'--port',str(sp),'--inference-workers','4','--threads','2','--reranker-onnx',str(model_dir),'--rerank-batch-size','1','--dense-batch-size','8','--dense-batch-trigger','8']),('bridge',[str(bridge),'-mode','serve','-index',str(sparse_index),'-sidecar',sidecar,'-listen',f'127.0.0.1:{bp}'])]
        for name,command in commands:
            log=(args.out/(name+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True))
        deadline=time.monotonic()+180
        while True:
            if any(p.poll() is not None for p in processes):raise RuntimeError('service exited during startup')
            try:
                health=dict(sidecar=support.health(sidecar),bridge=support.health(backend))
                if any(s['corpus_sha256']!=meta['corpus_sha256'] for s in health.values()):raise ValueError('backend fingerprint mismatch')
                break
            except (OSError,TimeoutError):
                if time.monotonic()>deadline:raise TimeoutError('startup timeout')
                time.sleep(.25)
        (args.out/'health-before.json').write_text(json.dumps(health,indent=2)+'\n')
        if args.diagnose_only:
            subprocess.run([sys.executable,'bench_kit/diagnose_cloud.py','--backend',backend,'--output',str(args.out/'diagnostic.json')],env=env,check=True)
            return
        if args.rank_diagnostic:
            command=[sys.executable,'-u','bench_kit/diagnose_ranks.py','--backend',backend,'--corpus',str(data/'chunks.jsonl'),'--provenance',str(provenance),'--output',str(args.out/'rank-diagnostic.json')]
            for path in args.replay:command+=['--replay',str(path)]
            for target in args.review_target:command+=['--review-target',target]
            if args.replay_subquery is not None:command+=['--subquery',str(args.replay_subquery)]
            try:subprocess.run(command,env=env,check=True)
            finally:
                try:(args.out/'health-after.json').write_text(json.dumps(dict(sidecar=support.health(sidecar),bridge=support.health(backend)),indent=2)+'\n')
                except Exception as exc:(args.out/'health-after.error.txt').write_text(f'{type(exc).__name__}: {exc}\n')
            status['status']='completed'
            return
        command=[sys.executable,'-u','bench_kit/eval_agent.py','--backend',backend,'--corpus',str(data/'chunks.jsonl'),'--provenance',str(provenance),'--output',str(args.out/'evaluation')]
        if args.suite:
            command+=['--suite',str(args.out/'suite.json'),'--expected-suite-sha256',suite_digest]
            for case in args.suite_case or []:command+=['--case',case]
        elif args.audit_id:command+=['--case',args.audit_id]
        with (args.out/'evaluation.log').open('w') as log:
            process=subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            processes.append(process)
            try:code=process.wait(timeout=7200 if args.suite else 3600)
            finally:support.stop(process)
        try:
            (args.out/'health-after.json').write_text(json.dumps(dict(sidecar=support.health(sidecar),bridge=support.health(backend)),indent=2)+'\n')
        except Exception as exc:  # A dead service must not mask the evaluation outcome.
            (args.out/'health-after.error.txt').write_text(f'{type(exc).__name__}: {exc}\n')
        if code:raise RuntimeError('agent evaluation failed; inspect evaluation.log and evaluation/*.failure.json')
        status['status']='completed'
    except BaseException as exc:
        status['status']='failed';status['error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        for process in reversed(processes):support.stop(process)
        for log in logs:log.close()
        bridge.unlink(missing_ok=True)
        status.update(finished_utc=datetime.now(timezone.utc).isoformat(),evaluator_exit_code=code,
            service_exit_codes=[p.returncode for p in processes],owned_processes_stopped=all(p.poll() is not None for p in processes))
        status_path.write_text(json.dumps(status,indent=2)+'\n')


if __name__=='__main__':main()
