#!/usr/bin/env python3
"""Ten real-model audit runs. Structural/provenance checks, not a semantic accuracy gold set."""
import argparse
import asyncio
import json
from pathlib import Path
import platform
import re
import statistics
import sys
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py'))
from agent.models import State
from agent.provider import ClaudeJSON
from agent.provenance import ProvenanceRegistry
from agent.retrieval import HybridClient
from agent.state_machine import Agent
from agent.synthesizer import normalize


def cases() -> list[dict]:
    return [
        dict(id='audit01', prompt='Compare AAPL filing year 2023 and NVDA filing year 2024: find the disclosed dependence on third-party manufacturing and supply-chain risks. Retrieve evidence separately for each company.', scopes=[['AAPL',2023],['NVDA',2024]], negative=False),
        dict(id='audit02', prompt='Compare MSFT and AMZN, both filing year 2024: retrieve evidence on cloud business competition and the operating costs of their cloud infrastructure. Use separate company-specific retrieval steps.', scopes=[['MSFT',2024],['AMZN',2024]], negative=False),
        dict(id='audit03', prompt='Audit NVDA filing year 2024: separately retrieve export control risks and dependence on external manufacturing capacity. Explain with filing excerpts only.', scopes=[['NVDA',2024]], negative=False),
        dict(id='audit04', prompt='Audit AMZN filing year 2024: separately retrieve AWS sales disclosures and operating expense disclosures. Support both topics with direct excerpts.', scopes=[['AMZN',2024]], negative=False),
        dict(id='audit05', prompt='Audit AAPL filing year 2023: separately retrieve share repurchase authorization disclosures and cash dividend disclosures. Provide original supporting excerpts for both.', scopes=[['AAPL',2023]], negative=False),
        dict(id='audit06', prompt='Audit MSFT filing year 2024: separately retrieve cloud services growth disclosures and cybersecurity risks. Ground both topics in the filing.', scopes=[['MSFT',2024]], negative=False),
        dict(id='audit07', prompt='Audit AMZN filing year 2024: separately retrieve liquidity and cash-flow disclosures and operating lease obligations. Present supporting excerpts without deriving new numbers.', scopes=[['AMZN',2024]], negative=False),
        dict(id='audit08', prompt='Audit NVDA filing year 2024: separately retrieve Data Center revenue growth disclosures and gross margin disclosures. Use evidence for each topic without calculating a new ratio.', scopes=[['NVDA',2024]], negative=False),
        dict(id='audit09', prompt='Audit AAPL filing year 2025 only: separately retrieve revenue disclosures and share repurchase disclosures. Do not substitute filing year 2023 or any other year if 2025 evidence is unavailable.', scopes=[['AAPL',2025]], negative=True),
        dict(id='audit10', prompt='Audit MSFT filing year 2024, with the exact SectionItem filter "Item 1A": separately retrieve cybersecurity risk and cloud competition risk. Do not relax the exact section filter if unclassified passages are all that is available.', scopes=[['MSFT',2024]], negative=True, section='Item 1A'),
    ]


def verify(result, case: dict, registry: ProvenanceRegistry) -> dict:
    assert result.states == [State.PLAN, State.RETRIEVE_EVALUATE, State.SYNTHESIZE], 'invalid state sequence'
    assert 2 <= len(result.plan.subqueries) <= 3, 'invalid decomposition'
    scopes = {tuple(scope) for scope in case['scopes']}
    planned = {(q.filters.ticker,q.filters.year) for q in result.plan.subqueries}
    assert planned == scopes, f'plan changed requested filing scope: {planned}'
    for query in result.plan.subqueries:
        assert query.filters.section == case.get('section', ''), 'plan changed exact section scope'
    for position, query in enumerate(result.plan.subqueries):
        hops = [hop for hop in result.hops if hop.subquery == position]
        assert 1 <= len(hops) <= 3 and [h.attempt for h in hops] == list(range(len(hops))), 'unbounded retries'
        assert all(h.filters == query.filters for h in hops), 'rewrite changed immutable filters'
        assert all(not h.accepted for h in hops[:-1]), 'retried an accepted query'
        for hop in hops:
            assert 1 <= hop.score <= 5 and (not hop.accepted or hop.score >= 4), 'invalid critic acceptance'
            for evidence in hop.evidence:
                verified = registry.verify(dict(id=evidence.id, text=evidence.text, metadata=evidence.metadata), query.filters)
                assert verified == evidence, 'unverified evidence in trace'
    accepted = {e.id: e for h in result.hops if h.accepted for e in h.evidence}
    for citation in result.citations:
        assert citation.chunk_id in accepted, 'citation refers to unaccepted evidence'
        item = accepted[citation.chunk_id]
        assert citation.quote in normalize(item.text), 'financial claim differs from SEC quote'
        assert citation.accession == item.accession and re.fullmatch(r'\d{10}-\d{2}-\d{6}', citation.accession), 'invalid accession'
        assert citation.tag == item.tag and citation.url == item.url, 'incorrect citation tag/link'
        assert f'[{citation.tag}]({citation.url})' in result.markdown, 'missing inline citation'
        assert f'`{citation.chunk_id}`' in result.markdown and f'`{citation.accession}`' in result.markdown, 'missing source identity'
    if case['negative']:
        assert not result.citations and result.flags, 'unsupported audit was not flagged'
        assert all(h.score == 1 and not h.accepted for h in result.hops), 'missing evidence accepted'
        assert len(result.hops) == 3*len(result.plan.subqueries), 'two-retry bound not exercised'
        for pos in range(len(result.plan.subqueries)):
            queries = [h.query for h in result.hops if h.subquery == pos]
            assert len(set(queries)) >= 2, 'critic did not rewrite query'
    else:
        assert result.citations, 'available evidence produced no grounded claims'
    covered = sum(bool({c.chunk_id for c in result.citations}.intersection(e.id for e in h.evidence)) for h in result.hops if h.accepted)
    return dict(id=case['id'], status='pass', negative=case['negative'], subqueries=len(result.plan.subqueries),
        retrieval_calls=len(result.hops), retries=sum(h.attempt > 0 for h in result.hops),
        accepted_subqueries=sum(h.accepted for h in result.hops), cited_subqueries=covered,
        citations=len(result.citations), complete_coverage=(not case['negative'] and covered == len(result.plan.subqueries)),
        flags=result.flags, model_calls=len(result.model_calls), seconds=result.elapsed_seconds)


async def evaluate(args) -> None:
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    registry = ProvenanceRegistry(Path(args.corpus), Path(args.provenance))
    model = ClaudeJSON(model=args.model)
    records = []
    async with HybridClient(args.backend) as backend:
        agent = Agent(model, backend, registry)
        for case in cases()[:args.limit]:
            try:
                result = await agent.run(case['prompt'])
                (output / (case['id'] + '.json')).write_text(result.model_dump_json(indent=2))
                (output / (case['id'] + '.md')).write_text(result.markdown+'\n')
                record = verify(result, case, registry)
            except Exception as exc:
                record = dict(id=case['id'], status='fail', error=f'{type(exc).__name__}: {exc}')
            records.append(record)
            print(json.dumps(record), flush=True)
    passed = [r for r in records if r['status']=='pass']
    report = dict(utc=datetime.now(timezone.utc).isoformat(), python=platform.python_version(),
        model_requested=args.model, backend=args.backend, cases=records, passed=len(passed), total=len(records),
        citations=sum(r.get('citations',0) for r in records), retrieval_calls=sum(r.get('retrieval_calls',0) for r in records),
        retries=sum(r.get('retries',0) for r in records),
        fully_covered_positive_cases=sum(r.get('complete_coverage',False) for r in records),
        negative_cases=sum(r.get('negative',False) for r in records),
        median_seconds=statistics.median([r['seconds'] for r in passed]) if passed else None,
        evaluation_scope='Live multi-step retrieval, immutable filing filters, bounded critic retries, exact SEC quote/source identity and inline citation checks. No human semantic correctness labels; not an answer-accuracy score.')
    (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2), flush=True)
    if len(passed) != len(records):
        raise SystemExit(1)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', default='http://127.0.0.1:8765')
    parser.add_argument('--model', default='haiku')
    parser.add_argument('--corpus', default='data/sec/chunks.jsonl')
    parser.add_argument('--provenance', default='data/sec/provenance.json')
    parser.add_argument('--output', default='bench_kit/results/phase3/evaluation')
    parser.add_argument('--limit', type=int, choices=range(1,11), default=10)
    asyncio.run(evaluate(parser.parse_args()))
