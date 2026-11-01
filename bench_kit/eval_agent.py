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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_suite
from agent.models import State
from agent.provider import ClaudeJSON
from agent.provenance import ProvenanceRegistry, digest
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
    """Structural/provenance checks. Expected unavailability is audited per requested scope.

    `case['expected_scopes']` lists one availability row per requested (ticker, year). A subquery whose scope is
    unavailable must exhaust its retries with no evidence; subqueries of available scopes are checked structurally and
    may honestly end in a partial gap unless `case['require_citations']` (historical regression behaviour) is set.
    """
    assert result.states == [State.PLAN, State.RETRIEVE_EVALUATE, State.SYNTHESIZE], 'invalid state sequence'
    assert 2 <= len(result.plan.subqueries) <= 3, 'invalid decomposition'
    by_scope = {(row['ticker'], row['year']): row for row in case['expected_scopes']}
    planned = {(q.filters.ticker,q.filters.year) for q in result.plan.subqueries}
    assert planned == set(by_scope), f'plan changed requested filing scope: {planned}'
    for query in result.plan.subqueries:
        assert query.filters.section == case.get('section', ''), 'plan changed exact section scope'
        expected_accession = by_scope[(query.filters.ticker, query.filters.year)]['accession']
        assert query.filters.accession == expected_accession, 'plan resolved an untrusted accession'
    unavailable = []
    for position, query in enumerate(result.plan.subqueries):
        hops = [hop for hop in result.hops if hop.subquery == position]
        assert 1 <= len(hops) <= 3 and [h.attempt for h in hops] == list(range(len(hops))), 'unbounded retries'
        assert all(h.filters == query.filters for h in hops), 'rewrite changed immutable filters'
        assert all(not h.accepted for h in hops[:-1]), 'retried an accepted query'
        assert hops[-1].accepted or len(hops) == 3, 'stopped retrying before the retry bound'
        for hop in hops:
            assert 1 <= hop.score <= 5 and (not hop.accepted or hop.score >= 4), 'invalid critic acceptance'
            assert len(hop.evidence) <= 5 and len(hop.retained_evidence_ids) <= 2, 'context bound exceeded'
            for evidence in hop.evidence:
                verified = registry.verify(dict(id=evidence.id, text=evidence.text, metadata=evidence.metadata), query.filters)
                assert verified == evidence, 'unverified evidence in trace'
        if not by_scope[(query.filters.ticker, query.filters.year)]['available']:
            unavailable.append(position)
            assert all(h.score == 1 and not h.accepted and not h.evidence for h in hops), 'missing evidence accepted'
            assert len(hops) == 3, 'two-retry bound not exercised'
            assert len({h.query for h in hops}) >= 2, 'critic did not rewrite query'
            assert f'Subquery {position} has no evidence meeting threshold after 2 retries.' in result.flags, 'unavailable scope not flagged'
    accepted = {e.id: e for h in result.hops if h.accepted for e in h.evidence}
    available_accessions = {row['accession'] for row in case['expected_scopes'] if row['available']}
    for citation in result.citations:
        assert citation.chunk_id in accepted, 'citation refers to unaccepted evidence'
        assert citation.chunk_id.split('#')[0] in available_accessions, 'citation from an unavailable scope'
        item = accepted[citation.chunk_id]
        assert citation.quote in normalize(item.text), 'financial claim differs from SEC quote'
        assert citation.accession == item.accession and re.fullmatch(r'\d{10}-\d{2}-\d{6}', citation.accession), 'invalid accession'
        assert citation.tag == item.tag and citation.url == item.url, 'incorrect citation tag/link'
        assert f'[{citation.tag}]({citation.url})' in result.markdown, 'missing inline citation'
        assert f'`{citation.chunk_id}`' in result.markdown and f'`{citation.accession}`' in result.markdown, 'missing source identity'
    negative = len(unavailable) == len(result.plan.subqueries)
    if negative:
        assert not result.citations and result.flags, 'unsupported audit was not flagged'
    elif case.get('require_citations'):
        assert result.citations, 'available evidence produced no grounded claims'
    cited_ids = {c.chunk_id for c in result.citations}
    cited_positions = [bool(cited_ids.intersection(e.id for h in result.hops if h.subquery == pos and h.accepted for e in h.evidence))
                       for pos in range(len(result.plan.subqueries))]
    covered = sum(cited_positions)
    available_positions = [p for p in range(len(result.plan.subqueries)) if p not in unavailable]
    outcome = 'expected_unavailable' if negative else agent_suite.evidence_outcome(
        [f for f in result.flags if not any(f.startswith(f'Subquery {p} ') for p in unavailable)], len(result.citations),
        len(available_positions), sum(cited_positions[p] for p in available_positions))
    return dict(id=case['id'], status='pass', negative=negative, subqueries=len(result.plan.subqueries),
        retrieval_calls=len(result.hops), retries=sum(h.attempt > 0 for h in result.hops),
        accepted_subqueries=sum(h.accepted for h in result.hops), cited_subqueries=covered,
        citations=len(result.citations), complete_coverage=(not negative and covered == len(result.plan.subqueries)),
        evidence_outcome=outcome, unavailable_subqueries=unavailable,
        flags=result.flags, model_calls=len(result.model_calls), seconds=result.elapsed_seconds)


def legacy_expected(case: dict, registry: ProvenanceRegistry) -> dict:
    """Per-scope availability from the checksum-verified corpus (replaces the old any-source classification)."""
    sections = agent_suite.registry_sections(registry)
    sources = list(registry.sources.values())
    rows = [agent_suite.scope_availability(sources, sections, ticker, year, case.get('section', '')) for ticker, year in case['scopes']]
    return dict(case, expected_scopes=rows, negative=not any(row['available'] for row in rows), require_citations=not all(not r['available'] for r in rows))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + '\n')


def prepare_suite_run(args, registry: ProvenanceRegistry, output: Path) -> tuple[dict, str, list[dict]]:
    """Everything that must hold BEFORE the first request: pinned suite, trusted identities, section availability."""
    suite, digest = agent_suite.load_suite(Path(args.suite), args.expected_suite_sha256)
    if suite['corpus_sha256'] != digest_of_corpus(args.corpus):
        raise agent_suite.SuiteError('suite was frozen against a different corpus')
    agent_suite.check_expected_scopes(suite, list(registry.sources.values()), agent_suite.registry_sections(registry), compare_counts=False)
    known = {case['id'] for case in suite['cases']}
    selected_ids = args.case or [case['id'] for case in suite['cases']]
    if not set(selected_ids) <= known or len(set(selected_ids)) != len(selected_ids):
        raise SystemExit(f'--case must be unique ids from the suite: {sorted(known)}')
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f'evaluation output {output} is not fresh')
    output.mkdir(parents=True, exist_ok=True)
    frozen = Path(args.suite).read_bytes()
    (output / 'suite.json').write_bytes(frozen)
    (output / 'suite.sha256').write_text(f'{digest}  suite.json\n')
    write_json(output / 'run-manifest.json', dict(started_utc=datetime.now(timezone.utc).isoformat(), suite_id=suite['suite_id'],
        suite_sha256=digest, corpus_sha256=suite['corpus_sha256'], case_count=suite['case_count'], selected=selected_ids,
        full_suite=selected_ids == suite['case_ids'], model_requested=args.model, backend=args.backend, python=platform.python_version(),
        note='Written before the first request. The agent receives case prompts only; anchors and expectations are evaluator metadata.'))
    cases = [dict(case, require_citations=False) for case in suite['cases'] if case['id'] in selected_ids]
    return suite, digest, cases


def digest_of_corpus(path) -> str:
    return digest(Path(path))


async def run_case(agent: Agent, registry: ProvenanceRegistry, case: dict, output: Path, suite_mode: bool) -> dict:
    """Run one case to completion. A failure becomes a record, never an early exit."""
    try:
        result = await agent.run(agent_suite.agent_view(case)['prompt'])
    except Exception as exc:
        record = agent_suite.failure_record(case['id'], 'agent_run', exc)
        if suite_mode:
            write_json(output / (case['id'] + '.failure.json'), record)
        return record
    (output / (case['id'] + '.json')).write_text(result.model_dump_json(indent=2))
    (output / (case['id'] + '.md')).write_text(result.markdown + '\n')
    try:
        record = verify(result, case, registry)
    except Exception as exc:
        record = agent_suite.failure_record(case['id'], 'in_process_verification', exc)
        if suite_mode:
            write_json(output / (case['id'] + '.failure.json'), record)
    return record


async def evaluate(args) -> None:
    output = Path(args.output)
    registry = ProvenanceRegistry(Path(args.corpus), Path(args.provenance))
    suite_mode = bool(args.suite)
    try:
        if suite_mode:
            suite, suite_digest, selected = prepare_suite_run(args, registry, output)
        else:
            output.mkdir(parents=True, exist_ok=True)
            selected = [legacy_expected(case, registry) for case in cases() if not args.case or case['id'] in args.case][:args.limit]
        model = ClaudeJSON(model=args.model)
        records = []
        async with HybridClient(args.backend) as backend:
            agent = Agent(model, backend, registry)
            for case in selected:
                record = await run_case(agent, registry, case, output, suite_mode)
                if suite_mode:
                    record = dict(record, kind=case['kind'])
                records.append(record)
                if suite_mode:
                    with (output / 'records.jsonl').open('a') as stream:
                        stream.write(json.dumps(record) + '\n')
                print(json.dumps(record), flush=True)
        passed = [r for r in records if r['status']=='pass']
        report = dict(utc=datetime.now(timezone.utc).isoformat(), python=platform.python_version(),
            model_requested=args.model, backend=args.backend, corpus_sha256=digest(Path(args.corpus)), registered_filings=len(registry.sources), cases=records, passed=len(passed), total=len(records),
            citations=sum(r.get('citations',0) for r in records), retrieval_calls=sum(r.get('retrieval_calls',0) for r in records),
            retries=sum(r.get('retries',0) for r in records),
            fully_covered_positive_cases=sum(r.get('complete_coverage',False) for r in records),
            negative_cases=sum(r.get('negative',False) for r in records),
            median_seconds=statistics.median([r['seconds'] for r in passed]) if passed else None,
            evaluation_scope='Live multi-step retrieval, immutable filing filters, bounded critic retries, exact SEC quote/source identity and inline citation checks. No human semantic correctness labels; not an answer-accuracy score.')
        if suite_mode:
            report.update(suite_id=suite['suite_id'], suite_sha256=suite_digest, selected=[c['id'] for c in selected],
                failures_by_category={k: sum(r.get('category') == k for r in records) for k in agent_suite.FAILURE_CATEGORIES},
                evidence_outcomes={k: sum(r.get('evidence_outcome') == k for r in records) for k in agent_suite.EVIDENCE_OUTCOMES},
                note='Failures (transport/model/structural) are distinct from evidence_outcomes (supported/partial_gap/abstained/expected_unavailable), which are honest results. Run bench_kit/verify_broad_agent.py for independent verification; this summary is not trusted by it.')
        write_json(output/'report.json', report)
        print(json.dumps(report, indent=2), flush=True)
    finally:
        registry.close()
    if len(passed) != len(records):
        raise SystemExit(1)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', default='http://127.0.0.1:8765')
    parser.add_argument('--model', default='haiku')
    parser.add_argument('--corpus', default='data/sec/chunks.jsonl')
    parser.add_argument('--provenance', default='data/sec/provenance.json')
    parser.add_argument('--output', default='bench_kit/results/phase3/evaluation')
    parser.add_argument('--case', action='append', help='Run one audit (repeatable). Legacy: audit01..audit10; suite mode: any suite id (partial run)')
    parser.add_argument('--limit', type=int, choices=range(1,11), default=10)
    parser.add_argument('--suite', help='Frozen broad validation suite JSON (runs all cases, with per-case failure isolation)')
    parser.add_argument('--expected-suite-sha256', help='Pin the exact suite bytes; mismatch aborts before any request')
    arguments = parser.parse_args()
    if not arguments.suite:
        legacy_ids = [case['id'] for case in cases()]
        if arguments.case and not set(arguments.case) <= set(legacy_ids):
            parser.error(f'--case must be one of {legacy_ids}')
    asyncio.run(evaluate(arguments))
