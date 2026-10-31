#!/usr/bin/env python3
"""Replay cached SEC filings, build actual PQ8, verify shard-backed hybrid HTTP.

No SEC requests, LLM calls, synthetic passages or quality/latency benchmark claims.
The local EDGAR protocol fixture is owned by the Go replay test. Models are pinned
local snapshots. All artifacts and logs require fresh run directories.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def request_json(url: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=125) as response:
        return json.load(response)


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='fresh logs/report directory')
    parser.add_argument('--artifacts', type=Path, required=True, help='fresh sparse/vector artifact directory')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    output, artifacts = args.output.resolve(), args.artifacts.resolve()
    for path in (output, artifacts):
        if path.exists() or path.is_symlink():
            raise FileExistsError(path)
    output.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    env = dict(os.environ, HF_HOME=str(root/'models/hf-cache'), HF_HUB_OFFLINE='1',
               OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='4',
               PYTHONPATH=os.pathsep.join([str(root/'py'), os.environ.get('PYTHONPATH', '')]),
               GOCACHE=os.environ.get('GOCACHE', '/tmp/finfilings-go-cache'))

    def run(name: str, command: list[str], extra: dict | None = None) -> None:
        print(name, flush=True)
        with (output/(name+'.log')).open('w') as log:
            process = subprocess.Popen(command, env=env | (extra or {}), stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = process.wait(timeout=1200)
                if code:
                    raise RuntimeError(f'{name} exited {code}; see {log.name}')
            finally:
                stop(process)

    sparse, vectors = artifacts/'sparse', artifacts/'vectors'
    run('replay', ['go', 'test', '-race', '-count=1', './pkg/ingest', '-run', '^TestRealFilingReplayPipeline$', '-v'],
        {'FINFILINGS_REPLAY_MANIFEST': str(root/'bench_kit/results/ingest/go-edgar/replay-manifest.jsonl'),
         'FINFILINGS_REPLAY_REPORT': str(output/'replay-report.json'),
         'FINFILINGS_REPLAY_EXPORT': str(sparse)})
    run('embed', [sys.executable, '-u', 'py/embed.py', '--corpus', str(sparse/'passages.jsonl'), '--out', str(vectors)])
    binary = artifacts/'finfilings'
    run('build-bridge', ['go', 'build', '-o', str(binary), './cmd/finfilings'])
    digest = hashlib.sha256()
    passages = {}
    with (sparse/'passages.jsonl').open('rb') as source:
        for line in source:
            digest.update(line)
            chunk = json.loads(line)
            if chunk['ID'] in passages:
                raise ValueError('duplicate passage ID')
            passages[chunk['ID']] = chunk
    fingerprint = digest.hexdigest()
    if (sparse/'segments/CORPUS_SHA256').read_text().strip() != fingerprint:
        raise ValueError('sparse fingerprint differs from passage stream')
    manifest = json.loads((vectors/'manifest.json').read_text())
    if manifest['corpus_sha256'] != fingerprint:
        raise ValueError('vector fingerprint differs from passage stream')
    sidecar_port, bridge_port = free_port(), free_port()
    while sidecar_port == bridge_port:
        bridge_port = free_port()
    commands = [('sidecar', [sys.executable, '-u', 'py/service.py', '--index', str(vectors), '--port', str(sidecar_port)]),
                ('bridge', [str(binary), '-mode', 'serve', '-index', str(sparse/'segments'),
                            '-sidecar', f'http://127.0.0.1:{sidecar_port}', '-listen', f'127.0.0.1:{bridge_port}'])]
    processes, logs = [], []
    try:
        for name, command in commands:
            log = (output/(name+'.log')).open('w')
            logs.append(log)
            processes.append(subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
        deadline = time.monotonic()+120
        while True:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError('service exited; inspect sidecar/bridge logs')
            try:
                health = {name: request_json(f'http://127.0.0.1:{port}/health')
                          for name, port in [('sidecar', sidecar_port), ('bridge', bridge_port)]}
                if any(h['corpus_sha256'] != fingerprint for h in health.values()):
                    raise ValueError('health fingerprint mismatch')
                break
            except (OSError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise TimeoutError('services did not become ready')
                time.sleep(.25)
        cases = []
        identities = sorted({(c['Ticker'], c['FilingYear'], c['ID'][:18]) for c in passages.values()})
        for ticker, year, accession in identities:
            for section, query in [('Item 1A', 'risk competition supply'), ('Item 7', 'revenue operating income'),
                                   ('Item 8', 'financial statements cash flows')]:
                cases.append({'name': ticker+' '+section, 'query': query, 'rerank': True,
                              'filters': dict(ticker=ticker, year=year, accession=accession, section=section)})
        cases.append({'name': 'missing accession', 'query': 'revenue', 'rerank': True,
                      'filters': {'accession': '000000000000000000'}})
        results = []
        with (output/'queries.jsonl').open('w') as stream:
            for case in cases:
                eligible = {identifier: c for identifier, c in passages.items() if all(
                    (identifier.startswith(value+'#') if key == 'accession' else c[{'ticker':'Ticker', 'year':'FilingYear', 'section':'SectionItem'}[key]] == value)
                    for key, value in case['filters'].items())}
                start = time.perf_counter()
                result = request_json(f'http://127.0.0.1:{bridge_port}/search', {k:v for k,v in case.items() if k != 'name'})
                elapsed = time.perf_counter()-start
                for stage in ('sparse', 'dense', 'hybrid', 'reranked'):
                    hits = result.get(stage) or []
                    if len({h['id'] for h in hits}) != len(hits) or any(h['id'] not in eligible for h in hits):
                        raise ValueError(f'{case["name"]}: invalid {stage} IDs/filter')
                reranked = result.get('reranked') or []
                if len(reranked) != min(10, len(eligible)):
                    raise ValueError(f'{case["name"]}: incomplete reranked passages')
                for hit in reranked:
                    original = passages[hit['id']]
                    if hit['text'] != original['Text'] or any(hit['metadata'][k] != original[k] for k in hit['metadata']):
                        raise ValueError('reranked passage differs from original source')
                row = dict(case=case, seconds=elapsed, eligible=len(eligible), result=result, checks='PASS')
                stream.write(json.dumps(row)+'\n'); stream.flush()
                results.append(dict(name=case['name'], seconds=elapsed, eligible=len(eligible),
                                    sparse=len(result.get('sparse') or []), dense=len(result.get('dense') or []),
                                    reranked=len(reranked), checks='PASS'))
                print(f'{case["name"]}: PASS ({len(reranked)} source-matched passages)', flush=True)
        final_health = request_json(f'http://127.0.0.1:{sidecar_port}/health')
        report = dict(scope='Functional real-model integration on four cached SEC filings; not relevance evaluation or a load benchmark. No live SEC or LLM calls.',
                      documents=len(passages), corpus_sha256=fingerprint, vector_manifest=manifest,
                      health_before=health, sidecar_health_after=final_health, queries=results,
                      passed=len(results), checked_reranked_passages=sum(r['reranked'] for r in results))
        (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    finally:
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()


if __name__ == '__main__':
    main()
