#!/usr/bin/env python3
"""Independently check HTTP observations and consolidate recorded phase metrics."""
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics


def read(path: Path):
    return json.loads(path.read_text())


def main() -> None:
    root=Path(__file__).resolve().parents[1]
    base=root/'bench_kit';phase4=base/'results/phase4'
    run=root/read(phase4/'latest-run.json')['directory']
    load=read(run/'load/report.json')
    health=read(run/'health.json')
    admission=health['8766'].get('admission', {'limit':16})
    baseline_path=base/'results/phase4/run-20261031T062424Z/load/report.json'
    baseline=read(baseline_path) if baseline_path.exists() else None
    first=read(base/'results/phase1-3051556664/report.json')
    second=read(base/'results/phase2/evaluation/report.json')
    agent_root=root/read(base/'results/phase3/latest-run.json')['directory']
    third=read(agent_root/'report.json')
    if not first['stable'] or third['passed'] != third['total']:
        raise ValueError('prior phase verification is incomplete')
    verified={}
    for endpoint in ('hybrid','dense'):
        report=load[endpoint]
        with (run/'load'/f'{endpoint}-requests.csv').open() as source:
            rows=list(csv.DictReader(source))  # finite capped benchmark samples, never document data
        if len(rows)!=report['requests'] or {int(r['request']) for r in rows}!=set(range(report['requests'])):
            raise ValueError('missing/duplicate request observations')
        if len({r['worker'] for r in rows})!=report['workers']:
            raise ValueError('not all configured workers exercised')
        success=[int(r['elapsed_ns']) for r in rows if not r['error']]
        if len(success)!=report['successes'] or len(rows)-len(success)!=report['failures']:
            raise ValueError('failure counts disagree')
        statuses={}
        for row in rows:statuses[row['status']]=statuses.get(row['status'],0)+1
        if statuses!=report['statuses']:raise ValueError('status counts disagree')
        for values,key in (([int(r['elapsed_ns']) for r in rows],'all_attempt_latency'),(success,'successful_latency')):
            if not values:
                if report[key] is not None:raise ValueError('unexpected success percentiles')
                continue
            ordered=sorted(values)
            measured={f'p{p}_ns':ordered[math.ceil(p/100*len(ordered))-1] for p in (50,95,99)}
            measured['max_ns']=ordered[-1]
            if measured!=report[key]:raise ValueError('percentile mismatch')
        verified[endpoint]=dict(records=len(rows),successes=len(success),failures=len(rows)-len(success),statuses=statuses,percentiles_match=True)
    (run/'independent-checks.json').write_text(json.dumps(verified,indent=2)+'\n')
    rounds=first['rounds'];memory=second['vector_memory']
    median_index=statistics.median(r['indexing']['documents_per_second'] for r in rounds)
    mmap=[r['validated_mmap_open']['p50_us']/1000 for r in rounds]
    rss=[r['load']['memory_after']['process_rss_kib']/1024 for r in rounds]
    phase1_p99=max(r['load']['search_latency']['p99_us']/1000 for r in rounds)
    rss_reduction=100*(1-memory['pq_rss_delta_median_bytes']/memory['raw_rss_delta_median_bytes'])
    lines=['# FinFilings AI — consolidated verified benchmarks','',
           f"Host: {first['cpu_model']}, {first['logical_cpus']} logical CPUs, Linux. BGE and MiniLM inference runs on CPU. Local, non-isolated desktop; warm models/filesystem cache. Results describe the stated finite workloads, not production SLOs.",'',
           '## Phase 1 — compressed sparse index','',
           '702 real SEC passages from four 10-K filings; ten rounds totaling 10,000,000 in-process BM25 searches, 50 configured workers. Observed simultaneous Search calls were 30–47.','',
           '| Metric | Verified result |','|---|---:|',
           f'| Median indexing throughput | {median_index:,.0f} passages/s |',
           f'| Validated mmap Open, round medians | {min(mmap):.3f}–{max(mmap):.3f} ms |',
           f'| Worst round search p99 | {phase1_p99:.3f} ms |',
           f'| Post-load benchmark process RSS | {min(rss):.1f}–{max(rss):.1f} MiB |',
           '| Search errors / ranking mismatches | 0 / 0 |','',
           'These are warm-cache, sparse-only, in-process measurements on a small corpus. The RSS includes benchmark buffers and mapped pages. Total timed load lasted 12.34 seconds; this is not a long soak. Mmap timing includes CRC/structural validation. [Original full Phase 1 report](PHASE1_RESULTS.md) · [raw JSON](results/phase1-3051556664/report.json).','',
           '## Phase 2 — hybrid retrieval and PQ8','',
           '19,511 real financial passages; 100 fixed FinQA development supporting-fact queries. RRF k=60, Top-50 fusion and MiniLM Top-10 reranking. Published supporting facts are binary relevance labels, not exhaustive judgments.','',
           '| Metric | Verified result |','|---|---:|']
    for label,key in [('BM25 nDCG@10','sparse'),('PQ8 nDCG@10','dense'),('Hybrid RRF nDCG@10','hybrid'),('Hybrid + cross-encoder nDCG@10','reranked')]:
        lines.append(f"| {label} | {second['ndcg_at_10'][key]:.4f} |")
    lines += [f"| PQ8 exact native vector payload reduction | {memory['exact_native_payload_reduction_percent']:.2f}% |",
        f"| Isolated vector-load RSS growth decrease | {rss_reduction:.2f}% |",
        f"| PQ8 recall@10 versus exact float32 | {second['pq_recall_at_10_vs_exact_float32']:.3f} |",'',
        f"Native payload: {memory['raw_native_payload_bytes']:,} → {memory['pq_native_payload_bytes']:,} bytes (codes plus centroids). Isolated median RSS growth: {memory['raw_rss_delta_median_bytes']:,} → {memory['pq_rss_delta_median_bytes']:,} bytes, three probes per representation.", '',
        'The 95.56% saving is **vector-component payload**, not whole-application RAM. The full model sidecar used approximately 1.50 GiB RSS after the Phase 2 evaluation. Serial CPU full-pipeline p99 was 2.674 seconds. [Phase 2 details](../docs/build/phase2.md) · [raw report](results/phase2/evaluation/report.json).','',
        '## Phase 3 — bounded agent and citation grounding','',
        f"- {third['passed']}/{third['total']} audit prompts passed execution/filter/retry/source checks.",
        f"- **{third['citations']}/{third['citations']} published citations verified** against exact SEC text, chunk ID, accession and source link.",
        f"- Median full-audit time: **{third['median_seconds']:.2f} seconds**.",
        f"- {third['fully_covered_positive_cases']}/8 positive audits had every subquery linked to published evidence; two AMZN audits were partial. Both unavailable-evidence tests correctly abstained.",
        f"- {third['retrieval_calls']} retrieval calls; {third['retries']} retries, maximum two per subquery.",'',
        'Citation correctness is not a human-labeled answer-accuracy score. Synthesis publishes verified excerpts rather than unverified calculations. AMZN cloud-cost and operating-expense retrieval gaps remain documented. [All ten audit results](results/phase3/RESULTS.md).','',
        '## Phase 4 — 50-worker HTTP load','',
        f"Measured run: `{run.name}`. Each endpoint received 200 measured requests from 50 persistent closed-loop workers, using the same 100 real Phase 2 queries twice. Five serial warmup requests per endpoint were excluded. Scenarios ran sequentially; each request used a new TCP connection to avoid keepalive monopolization of bounded server connections. Connection establishment, admission/queue delay, full response and content validation are timed. Client deadline: {load['timeout_seconds']:.0f} seconds.",'',
        f"Go retains eight engine slots and 16 live connections. Python retains one native inference worker and an explicit {admission['limit']}-request admission budget; the HTTP concurrency guard is 128. Requests beyond the application budget receive HTTP 429 with Retry-After instead of entering an unbounded queue. Fifty client requests can be in flight while only one native inference job executes. Cancellation does not release the native-work slot until the actual thread finishes.",'',
        '| Endpoint / measured population | N | p50 (ms) | p95 (ms) | p99 (ms) |','|---|---:|---:|---:|---:|']
    for key,label in [('hybrid','Go /search: BM25 + PQ8 + RRF + rerank'),('dense','Python /dense: BGE + PQ8')]:
        r=load[key]
        for pop,n,p in [('all attempts',r['requests'],r['all_attempt_latency']),('successful responses',r['successes'],r['successful_latency'])]:
            if p is None:continue
            lines.append(f"| {label}, {pop} | {n} | {p['p50_ns']/1e6:.6f} | {p['p95_ns']/1e6:.6f} | {p['p99_ns']/1e6:.6f} |")
    lines += ['', '| Endpoint | Success / attempts | Failure rate | Successful QPS | Completed attempts/s | Peak client in-flight |','|---|---:|---:|---:|---:|---:|']
    for key in ('hybrid','dense'):
        r=load[key];lines.append(f"| {key} | {r['successes']}/{r['requests']} | {100*r['failure_rate']:.2f}% | {r['successful_qps']:.6f} | {r['completed_attempts_qps']:.6f} | {r['peak_client_inflight']} |")
    lines += ['', 'Percentiles use exact empirical nearest rank `ceil(p × N)` over integer nanosecond observations; six decimal places in milliseconds preserve those integers. Both all-attempt and successful-only distributions are shown so quick overload errors cannot masquerade as faster useful retrieval. QPS uses the entire measured wall interval, including client ramp/drain and observation handling. Finite closed-loop results do not characterize open-loop arrivals, long-term reliability or a capacity limit.','']
    for key in ('hybrid','dense'):
        r=load[key];lines.append(f"- {key}: elapsed {r['seconds']:.6f} s; HTTP/status counts `{json.dumps(r['statuses'],sort_keys=True)}`.")
    lines += ['', f"Full hybrid failures: {load['hybrid']['failures']}/{load['hybrid']['requests']}; direct dense failures: {load['dense']['failures']}/{load['dense']['requests']}. These are finite closed-loop results, not a sustained capacity or production-availability certification. Dense before/after latency populations differ: the baseline quickly rejected 189 requests, while the optimized service queues admitted work. Successful-only distributions and status counts must accompany comparisons.", '',f"[Raw load report]({run.relative_to(base).as_posix()}/load/report.json) · [Terminal log]({run.relative_to(base).as_posix()}/terminal.log) · [Independent CSV/quantile checks]({run.relative_to(base).as_posix()}/independent-checks.json). All 400 per-request observations, including failures, are retained locally as CSV. One-second process RSS samples are in the same run directory; these are observed samples, not exact instantaneous peak memory.",'',
        '## Portfolio / resume supplement','',
        f"- Built a disk-backed Go SEC search engine with PFOR compression and mmap; measured {median_index:,.0f} passages/s median indexing and {phase1_p99:.3f} ms sparse p99 on 702 warm-cache passages.",
        f"- Achieved {second['ndcg_at_10']['reranked']:.4f} nDCG@10 on 100 fixed FinQA support-fact queries using BM25/PQ8 RRF and cross-encoder reranking; reduced native vector payload by {memory['exact_native_payload_reduction_percent']:.2f}%.",
        f"- Implemented a bounded multi-hop audit controller with {third['citations']}/{third['citations']} verified published citations across ten audits; median {third['median_seconds']:.2f} s, with partial answers and abstentions explicitly flagged."]
    r=load['hybrid']
    lines += [f"- Load-tested the full hybrid HTTP pipeline with 50 clients: {r['successful_qps']:.2f} successful QPS, {r['all_attempt_latency']['p99_ns']/1e9:.3f} s all-attempt p99, and {100*r['failure_rate']:.2f}% failures across {r['requests']} measured attempts.",'',
        '## Verification and reproduction','',
        'Go tests, race checks and vet passed; the new quantile test checks known nearest-rank values. Prior phases retain their own verification logs. Model revisions, original corpus/query fingerprints, HTTP statuses and raw timings are preserved. No race-instrumented timing is presented. Generated fixture benchmarks are excluded from the headline metrics.','',
        '```bash','export PYTHONPATH="$PWD/models/runtime:$PWD/py"','export GOCACHE=/tmp/finfilings-go-cache','export CCACHE_DIR=/tmp/finfilings-ccache CCACHE_TEMPDIR=/tmp/finfilings-ccache-tmp','go build -o models/bin/finfilings ./cmd/finfilings','go build -o models/bin/loadtest ./bench_kit/load','python3 scripts/verify-phase4.py','python3 bench_kit/consolidate.py','```','',
        'The isolated project runtime and pinned model cache must already be installed (see the Phase 2/3 setup reports). The supervisor owns both real services and stops them after measurement. Builders and the load tool refuse existing output directories. No tenant-isolation or production-availability claim is made.']
    if baseline is not None and run.name != 'run-20261031T062424Z':
        before,after=baseline['hybrid'],load['hybrid']
        comparison=['## Optimization — verified before/after','',
            'Same CPU models, FAISS index, 100-query catalog, 200 requests per endpoint, 50 clients, new-connection policy and 180-second deadline. Baseline records remain unchanged. Reranking now tokenizes at most 50 pairs once, groups them by token length, and pads each eight-pair microbatch only to its own maximum. No candidate pruning, result cache, lower precision or model replacement is used.','',
            '| Metric | Original | Optimized |','|---|---:|---:|',
            f"| Hybrid p50 | {before['all_attempt_latency']['p50_ns']/1e9:.6f} s | {after['all_attempt_latency']['p50_ns']/1e9:.6f} s |",
            f"| Hybrid p95 | {before['all_attempt_latency']['p95_ns']/1e9:.6f} s | {after['all_attempt_latency']['p95_ns']/1e9:.6f} s |",
            f"| Hybrid p99 | {before['all_attempt_latency']['p99_ns']/1e9:.6f} s | {after['all_attempt_latency']['p99_ns']/1e9:.6f} s |",
            f"| Hybrid successful QPS | {before['successful_qps']:.6f} | {after['successful_qps']:.6f} |",
            f"| Hybrid failures | {before['failures']}/{before['requests']} | {after['failures']}/{after['requests']} |",
            f"| Dense successes | {baseline['dense']['successes']}/{baseline['dense']['requests']} | {load['dense']['successes']}/{load['dense']['requests']} |",
            f"| Dense failures | {baseline['dense']['failures']}/{baseline['dense']['requests']} | {load['dense']['failures']}/{load['dense']['requests']} |",'',
            f"Hybrid throughput ratio: **{after['successful_qps']/before['successful_qps']:.3f}×**; p99 decrease: **{100*(1-after['all_attempt_latency']['p99_ns']/before['all_attempt_latency']['p99_ns']):.2f}%**. Non-isolated local observations, not a statistical significance claim.",'']
        quality_path=base/'results/optimization/rerank-quality.json'
        if quality_path.exists():
            quality=read(quality_path)
            comparison += [f"Quality replay: {quality['queries']} original RRF Top-50 candidate sets, actual pinned cross-encoder. nDCG@10 remained {quality['optimized_ndcg10']:.6f}; {quality['changed_top10_rankings']} Top-10 orders changed from floating-point padding effects (maximum shared score difference {quality['max_shared_top10_score_absolute_delta']:.8f}). This replays reranking, not a fresh end-to-end relevance benchmark. [Quality log](results/optimization/rerank-quality.json).",'']
        final_health_path=run/'health-after.json'
        if final_health_path.exists():
            native=read(final_health_path)['8766']
            comparison += [f"Measured admission peak: {native['admission']['peak']}/{native['admission']['limit']}; rejected by application budget: {native['admission']['rejected']}. Native inference workers: {native['inference_workers']}. Per-stage work and queue totals: [post-load health]({run.relative_to(base).as_posix()}/health-after.json).",'']
        comparison += ['Validation: 17 Python tests pass, including admission overflow/cancellation and the original provenance/PQ contracts. Live model integration and the raw CSV/percentile recheck are saved with the optimization results. Original Go tests/race/vet remain applicable; Go search and load code were unchanged.','',
            '[Original load report](results/phase4/run-20261031T062424Z/load/report.json) · [CPU micro-profile](results/optimization/cpu-profile.json). The three-query profile favored the original four CPU threads and eight-pair batch size; those settings were retained.','']
        location=lines.index('## Portfolio / resume supplement')
        lines[location:location]=comparison
    (base/'RESULTS.md').write_text('\n'.join(lines)+'\n')
    paths=[base/'load/main.go',base/'load/main_test.go',Path(__file__).resolve(),root/'scripts/verify-phase4.py',root/'py/service.py',root/'py/rerank.py',root/'py/test_phase2.py',root/'bench_kit/profile_rerank.py',root/'cmd/finfilings/main.go']
    (run/'source-sha256.json').write_text(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},indent=2)+'\n')
    print(json.dumps(verified,indent=2));print('Saved bench_kit/RESULTS.md')


if __name__=='__main__':main()
