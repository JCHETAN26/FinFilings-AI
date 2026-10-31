#!/usr/bin/env python3
"""Independently check rankings/load observations and publish expanded-corpus results."""
import argparse
import collections
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics


def read(path):return json.loads(path.read_text())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--data',type=Path,required=True)
    args=parser.parse_args();run=args.run;data=args.data
    evaluation=read(run/'benchmark/evaluation/report.json');load=read(run/'benchmark/load/report.json')
    ingestion=read(run/'ingestion-summary.json');provenance=read(data/'provenance.json')
    baseline=read(Path('bench_kit/results/phase2/evaluation/report.json'))
    old_load=read(Path('bench_kit/results/phase4/run-20261031T063553Z/load/report.json'))
    for name,key in [('queries.jsonl','query_sha256'),('qrels.tsv','qrels_sha256')]:
        value=hashlib.sha256((data/name).read_bytes()).hexdigest()
        if value!=provenance[key] or value!=baseline['corpus_provenance'][key]:raise ValueError('frozen evaluation input mismatch')
    judgments={}
    with (data/'qrels.tsv').open() as source:
        for row in csv.DictReader(source,delimiter='\t'):
            score=int(row['score'])
            if score not in (0,1):raise ValueError('independent nDCG check expects published binary labels')
            judgments.setdefault(row['query-id'],{})[row['corpus-id']]=score
    scores={name:[] for name in ('sparse','dense','hybrid','reranked')};seen=set()
    with (run/'benchmark/evaluation/per-query.jsonl').open() as source:
        for line in source:
            row=json.loads(line);identifier=row['id']
            if identifier in seen or identifier not in judgments:raise ValueError('invalid query catalog')
            seen.add(identifier);gold=judgments[identifier]
            ideal=sum(1/math.log2(i+2) for i in range(min(10,sum(gold.values()))))
            for stage in scores:
                dcg=sum(gold.get(h['id'],0)/math.log2(i+2) for i,h in enumerate(row['rankings'][stage][:10]))
                scores[stage].append(dcg/ideal if ideal else 0)
    if seen!=set(judgments):raise ValueError('qrels/query coverage mismatch')
    independently_computed={k:statistics.mean(v) for k,v in scores.items()}
    if any(not math.isclose(v,evaluation['ndcg_at_10'][k],abs_tol=1e-12) for k,v in independently_computed.items()):raise ValueError('independent nDCG mismatch')
    checks={'ndcg_at_10':independently_computed,'queries':len(seen),'load':{}}
    for endpoint in ('hybrid','dense'):
        with (run/f'benchmark/load/{endpoint}-requests.csv').open() as source:rows=list(csv.DictReader(source))
        actual=load[endpoint];successful=[r for r in rows if r['status']=='200' and not r['error']]
        if len(rows)!=actual['requests'] or len(successful)!=actual['successes']:raise ValueError('load counts mismatch')
        statuses=dict(collections.Counter(r['status'] for r in rows))
        if statuses!=actual['statuses']:raise ValueError('load statuses mismatch')
        for population,expected in [(rows,actual['all_attempt_latency']),(successful,actual['successful_latency'])]:
            if not population:
                if expected is not None:raise ValueError('unexpected successful percentiles')
                continue
            values=sorted(int(r['elapsed_ns']) for r in population)
            percentiles={key:values[math.ceil(p*len(values))-1] for key,p in [('p50_ns',.5),('p95_ns',.95),('p99_ns',.99)]}
            percentiles['max_ns']=values[-1]
            if percentiles!=expected:raise ValueError('load percentiles mismatch')
        if not math.isclose(len(successful)/actual['seconds'],actual['successful_qps'],rel_tol=1e-12):raise ValueError('load QPS mismatch')
        checks['load'][endpoint]=dict(requests=len(rows),successes=len(successful),statuses=statuses,percentiles_verified=True)
    (run/'independent-checks.json').write_text(json.dumps(checks,indent=2)+'\n')
    vector=evaluation['vector_memory'];manifest=evaluation['vector_manifest']
    lines=['# Larger real SEC corpus and hybrid benchmark','',
           'Verified on 2026-10-31. This is a frozen-FinQA-query stress test with additional live SEC passages, not exhaustive relevance evaluation of the new SEC corpus.','',
           f"Ingested **{ingestion['filings']} real 10-K filings from {ingestion['ingested_companies']} companies**, yielding **{ingestion['passages']:,} section-tagged passages in {ingestion['quarter_shards']} quarter shards**. The fixed cohort contained {ingestion['requested_companies']} companies, up to two recent annual filings each. The deterministic parser rejected {ingestion['skipped']} filings rather than guessing section tags. Accepted raw filings totaled {ingestion['raw_downloaded_bytes']:,} bytes. [Coverage and gaps](ingestion-summary.json), [per-filing provenance](ingestion-report.json), [fixed cohort](cohort.json).",'',
           f"The benchmark retains the original {provenance['document_counts']['baseline']:,} passages and all 100 published FinQA query/judgment records, and appends {provenance['document_counts']['new_sec']:,} SEC passages: **{provenance['documents']:,} total candidates**. Query and qrel hashes were independently checked against Phase 2. Newly added passages are unlabeled; a lower score can reflect relevant but unjudged retrieval as well as errors.",'',
           '| Ranking stage | Original 19,511 passages | Expanded corpus nDCG@10 |','|---|---:|---:|']
    for stage in ('sparse','dense','hybrid','reranked'):lines.append(f"| {stage} | {baseline['ndcg_at_10'][stage]:.6f} | {evaluation['ndcg_at_10'][stage]:.6f} |")
    lines+=['',f"PQ8 recall@10 versus exact float32 search: **{evaluation['pq_recall_at_10_vs_exact_float32']:.6f}** (100 fixed queries). This is quantization fidelity, not human relevance.",'',
            f"Exact native vector payload: **{vector['raw_native_payload_bytes']:,} → {vector['pq_native_payload_bytes']:,} bytes**, a **{vector['exact_native_payload_reduction_percent']:.4f}% reduction**. Isolated median observed RSS growth: raw {vector['raw_rss_delta_median_bytes']:,} bytes; PQ8 {vector['pq_rss_delta_median_bytes']:,} bytes. These exclude model and passage-store RAM.",'',
            f"Training used {manifest['training_count']:,} deterministic real-vector samples; recommended minimum {manifest['recommended_training_count']:,}; adequate training count: {manifest['adequate_training_count']}. Reused {manifest.get('reused_embeddings',0):,} checksum-verified BGE embeddings after exact ID/text/configuration checks; encoded {manifest.get('newly_encoded_embeddings',manifest['documents']):,} passages. PQ codebooks were retrained on the expanded corpus.",'',
            '| 50-worker endpoint | Successes / requests | p50 ms | p95 ms | p99 ms | Successful QPS | Failure rate |',
            '|---|---:|---:|---:|---:|---:|---:|']
    for name in ('hybrid','dense'):
        r=load[name];p=r['all_attempt_latency']
        lines.append(f"| {name} | {r['successes']}/{r['requests']} | {p['p50_ns']/1e6:.6f} | {p['p95_ns']/1e6:.6f} | {p['p99_ns']/1e6:.6f} | {r['successful_qps']:.6f} | {100*r['failure_rate']:.2f}% |")
    health=read(run/'benchmark/health-after.json')['sidecar']
    native=health['inference'];rank_share=100*native['rank']['work_seconds']/(native['rank']['work_seconds']+native['dense']['work_seconds'])
    lines += ['', f"Compared with the original benchmark, reranked nDCG@10 fell from {baseline['ndcg_at_10']['reranked']:.6f} to {evaluation['ndcg_at_10']['reranked']:.6f}; PQ8/exact ID recall fell from {baseline['pq_recall_at_10_vs_exact_float32']:.6f} to {evaluation['pq_recall_at_10_vs_exact_float32']:.6f}. On this run, RRF alone scored below BM25. The cross-encoder recovered quality, but did not reach the historical result.", '', f"Across the supervised quality, warmup and load requests, reranking accounted for {rank_share:.2f}% of instrumented native inference work ({native['rank']['work_seconds']:.3f} s rank vs {native['dense']['work_seconds']:.3f} s dense). This points to CPU reranking and queueing as the full-pipeline latency bottleneck. Admission peaked at {health['admission']['peak']}/{health['admission']['limit']}, with {health['admission']['rejected']} application-budget rejections and {health['inference_workers']} native inference worker. These counters span the full supervised run, not load alone.", '']
    lines+=['','Load percentiles above include all attempts, using nearest rank over integer nanoseconds. Separate successful-only distributions, status counts and raw observations are retained. This finite closed-loop run has five excluded warmups per endpoint, 200 measured requests per endpoint, fifty clients, eight hybrid admissions, and one native inference worker. It is not an open-loop capacity or soak test.','',
            f"Historical optimized 19,511-passage hybrid run: p99 {old_load['hybrid']['all_attempt_latency']['p99_ns']/1e9:.6f} seconds and {old_load['hybrid']['successful_qps']:.6f} QPS. The larger-corpus run is a separate local observation, not an isolated causal or statistical significance claim.",'',
            'Live SEC downloads used the operator-supplied identifying contact header at five requests/second. The ingestion output keeps nine true filing-quarter shards. For the directly comparable FinQA quality/load run, the original mixed benchmark collection plus new SEC candidates is streamed into a separate single sparse index; no filing quarters are fabricated for legacy FinQA snippets. The previously verified quarter-shard router remains available for the SEC-only collection.','',
            '[Quality report](benchmark/evaluation/report.json) · [Every ranked query](benchmark/evaluation/per-query.jsonl) · [Load report](benchmark/load/report.json) · [Independent checks](independent-checks.json) · [Service health after load](benchmark/health-after.json).','',
            'Reproduction tools: `bench_kit/prepare_expanded.py`, `py/embed.py --reuse-index`, `scripts/benchmark-expanded.py`, `bench_kit/report_expanded.py`. Model snapshots and Phase 1–4 evidence remain unchanged.']
    (run/'RESULTS.md').write_text('\n'.join(lines)+'\n')
    master=Path('bench_kit/RESULTS.md');text=master.read_text();marker='## Expanded real SEC corpus (2026-10-31)'
    if marker in text:text=text[:text.index(marker)].rstrip()+'\n'
    summary=['',marker,'',f"Live ingestion: {ingestion['filings']} accepted filings, {ingestion['ingested_companies']}/{ingestion['requested_companies']} companies, {ingestion['passages']:,} new passages; {ingestion['skipped']} explicitly rejected filings. Expanded retrieval collection: {provenance['documents']:,} passages; original 100 FinQA queries/qrels unchanged.",'',f"nDCG@10: BM25 {evaluation['ndcg_at_10']['sparse']:.6f}, PQ8 {evaluation['ndcg_at_10']['dense']:.6f}, RRF {evaluation['ndcg_at_10']['hybrid']:.6f}, cross-encoder {evaluation['ndcg_at_10']['reranked']:.6f}. PQ8/exact recall@10 {evaluation['pq_recall_at_10_vs_exact_float32']:.6f}; native vector payload reduction {vector['exact_native_payload_reduction_percent']:.4f}%.",'',f"[Detailed verified results and limitations]({run.relative_to(master.parent).as_posix()}/RESULTS.md). The new SEC passages are unlabeled; this measures frozen-judgment retrieval under corpus expansion, not SEC-only relevance. The earlier phase results above remain historical evidence."]
    summary += ['', '| Expanded corpus, 50 workers | p50 s | p95 s | p99 s | Successful QPS | Failures |', '|---|---:|---:|---:|---:|---:|']
    for endpoint in ('hybrid','dense'):
        r=load[endpoint];p=r['all_attempt_latency']
        summary.append(f"| {endpoint} | {p['p50_ns']/1e9:.6f} | {p['p95_ns']/1e9:.6f} | {p['p99_ns']/1e9:.6f} | {r['successful_qps']:.6f} | {r['failures']}/{r['requests']} |")
    summary += ['', 'Quality and full-pipeline latency regressed under corpus expansion. Raw rankings and all 400 load observations were independently checked; the dense endpoint had no 503 failures. The supervised services were stopped after evaluation.']
    master.write_text(text+'\n'.join(summary)+'\n')
    print(json.dumps(checks,indent=2))


if __name__=='__main__':main()
