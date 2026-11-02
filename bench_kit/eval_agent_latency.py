#!/usr/bin/env python3
"""Paired legacy-vs-fast agent latency benchmark over ONE shared backend, plus its independent offline verifier.

`run`    executes the frozen suite prompts, unchanged, sequentially (one audit active at a time, one shared 2-slot model
         pool, one shared retrieval client). Each (case, repeat) is a PAIR run once per mode; the order alternates AB/BA by pair.
         The schedule is capped at 24 runs and written to run-manifest.json BEFORE the first request. Every raw AgentResult,
         failure record and timing is preserved. No result cache exists; nothing is retried or re-run for luck.
`verify` re-audits every saved trace from the frozen suite and the original corpus bytes (verify_broad_agent.audit_case, the
         same checks as the 30-case verifier, mode-aware) and only then computes the comparison from those recomputed records.

Latency is wall-clock from live Claude CLI calls on a small paired subset. It is not a throughput, p99 or answer-quality claim.
Run through scripts/evaluate-expanded-agent.py --paired-latency, which owns the 4x2 sidecar and the bridge.
"""
import argparse
import asyncio
import collections
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_suite as lib  # noqa: E402

MODES = ('legacy', 'fast')
DEFAULT_CASES = ('audit04', 'audit07', 'broad25', 'broad21', 'audit10', 'broad30')
MAX_RUNS = 24
LATENCY_TARGET = 0.15  # preregistered: reduction of the MEDIAN end-to-end seconds across the primary supported runs per mode
PRIMARY_CASES = ('audit04', 'audit07', 'broad25', 'broad21')  # preregistered supported cases (pre-run-plan.json); the other two are descriptive
MAX_WALL_OVERHEAD = 5.0  # seconds the runner's wall clock may exceed the agent's own elapsed time (bookkeeping around agent.run)
RUN_FILE = re.compile(r'^(\d{2})-(legacy|fast)-((?:audit|broad)\d{2})-r(\d)(\.failure)?\.(json|md)$')
SUMMARY_FILES = {'run-manifest.json', 'records.jsonl', 'suite.json', 'suite.sha256'}


def schedule(cases, repeats):
    """Alternating AB/BA by pair, case-major. Deterministic, bounded and recorded before the first request."""
    runs, pair = [], 0
    for case in cases:
        for repeat in range(1, repeats + 1):
            for position, mode in enumerate(MODES if pair % 2 == 0 else MODES[::-1]):
                runs.append(dict(ordinal=len(runs) + 1, pair=pair, case=case, repeat=repeat, mode=mode, position_in_pair=position,
                                 run_id=f'{len(runs) + 1:02}-{mode}-{case}-r{repeat}'))
            pair += 1
    if not runs or len(runs) > MAX_RUNS or len(set(cases)) != len(cases):
        raise ValueError(f'the paired schedule must contain 1..{MAX_RUNS} runs over unique cases (got {len(runs)})')
    return runs


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def stage_seconds(calls):
    """Per-stage model time from the actual call ledger only (nothing inferred for calls that did not happen)."""
    by = collections.defaultdict(list)
    for call in calls:
        by[call['schema']].append(float(call['seconds']))
    critic = by.get('CritiqueWithQuotes', []) + by.get('Critique', [])
    synth = by.get('HandleDraft', []) + by.get('Draft', [])
    return dict(plan_call_seconds=sum(by.get('Plan', [])), critic_call_seconds_total=sum(critic), critic_call_seconds_max=max(critic, default=0.0),
                critic_calls=len(critic), synthesis_call_seconds=sum(synth), synthesis_calls=len(synth),
                turns=[call.get('num_turns') for call in calls])


# ---------------------------------------------------------------- run
async def run(args):
    from agent.provider import ClaudeJSON
    from agent.provenance import ProvenanceRegistry, digest
    from agent.retrieval import HybridClient
    from agent.state_machine import Agent
    import eval_agent
    suite, suite_digest = lib.load_suite(Path(args.suite), args.expected_suite_sha256)
    if suite['corpus_sha256'] != digest(Path(args.corpus)):
        raise lib.SuiteError('suite was frozen against a different corpus')
    cases_by_id = {c['id']: c for c in suite['cases']}
    selected = args.case or list(DEFAULT_CASES)
    if not set(selected) <= set(cases_by_id):
        raise SystemExit(f'--case must be suite ids: {sorted(cases_by_id)}')
    runs = schedule(selected, args.repeats)
    output = Path(args.output)
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f'latency output {output} is not fresh')
    (output / 'runs').mkdir(parents=True, exist_ok=True)
    registry = ProvenanceRegistry(Path(args.corpus), Path(args.provenance))
    try:
        lib.check_expected_scopes(suite, list(registry.sources.values()), lib.registry_sections(registry), compare_counts=False)
        (output / 'suite.json').write_bytes(Path(args.suite).read_bytes())
        (output / 'suite.sha256').write_text(f'{suite_digest}  suite.json\n')
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, cwd=ROOT).stdout.strip()
        write_json(output / 'run-manifest.json', dict(
            started_utc=datetime.now(timezone.utc).isoformat(), kind='paired-latency', suite_id=suite['suite_id'], suite_sha256=suite_digest,
            corpus_sha256=suite['corpus_sha256'], model_requested=args.model, backend=args.backend, python=platform.python_version(),
            cases=selected, repeats=args.repeats, primary_supported_cases=[c for c in PRIMARY_CASES if c in selected], modes=list(MODES), schedule=runs, git_head=head,
            code={rel: lib.sha256_file(ROOT / rel) for rel in lib.code_files(ROOT)},
            limits=dict(max_runs=MAX_RUNS, audits_active=1, model_concurrency=2, agent_concurrency=3, subqueries_max=3, retries_max=2, contexts_max=5, retained_max=2),
            controls='Prompts are the frozen suite prompts, unchanged. One shared ClaudeJSON (2 slots) and one shared HybridClient serve both modes; '
                     'no result cache; no per-mode tuning; every run is kept, failures included.',
            note='Written before the first request.'))
        model = ClaudeJSON(model=args.model)
        async with HybridClient(args.backend) as backend:
            agents = {mode: Agent(model, backend, registry, mode=mode) for mode in MODES}
            for run_info in runs:
                case = dict(cases_by_id[run_info['case']], require_citations=False)
                record = await execute(agents[run_info['mode']], registry, case, run_info, output / 'runs', eval_agent)
                with (output / 'records.jsonl').open('a') as stream:
                    stream.write(json.dumps(record) + '\n')
                print(json.dumps(record), flush=True)
    finally:
        registry.close()


async def execute(agent, registry, case, run_info, directory, eval_agent):
    """One audit to completion. Failure is a record, never an early exit. Wall time is measured around agent.run only."""
    base = dict(run_info, started_utc=datetime.now(timezone.utc).isoformat())
    start = time.perf_counter()
    try:
        result = await agent.run(lib.agent_view(case)['prompt'])
    except Exception as exc:
        wall = time.perf_counter() - start
        failure = lib.failure_record(case['id'], 'agent_run', exc)
        write_json(directory / f"{run_info['run_id']}.failure.json", failure)
        return dict(base, status='fail', category=failure['category'], error=failure['error'][:500], wall_seconds=wall)
    wall = time.perf_counter() - start
    (directory / f"{run_info['run_id']}.json").write_text(result.model_dump_json(indent=2))
    (directory / f"{run_info['run_id']}.md").write_text(result.markdown + '\n')
    try:
        checked = eval_agent.verify(result, case, registry)  # in-process structural check; the offline verifier repeats it independently
        status, extra = 'pass', dict(evidence_outcome=checked['evidence_outcome'])
    except Exception as exc:
        status, extra = 'fail', dict(category='structural', error=f'{type(exc).__name__}: {exc}'[:500])
    return dict(base, status=status, wall_seconds=wall, agent_seconds=result.elapsed_seconds, agent_mode=result.execution_mode,
                synthesis_route=result.synthesis_route, model_calls=len(result.model_calls), **extra)


# ---------------------------------------------------------------- verify
def median(values):
    return statistics.median(values) if values else None


def primary_gate(rows, primary_cases, repeats):
    """The preregistered latency gate: 1 - median(fast seconds) / median(legacy seconds) over the primary supported runs of each mode.

    Fails closed: every expected primary run must be present, independently verified, and 'supported' in BOTH modes. A primary run is never
    filtered out (for example for poor coverage); it makes the gate fail with a stated problem instead.
    """
    expected = [(case, repeat) for case in primary_cases for repeat in range(1, repeats + 1)]
    problems = []
    seconds = {mode: [] for mode in MODES}
    for case, repeat in expected:
        for mode in MODES:
            row = rows.get((case, repeat, mode))
            if row is None:
                problems.append(f'{case} r{repeat} {mode}: primary run missing or not independently verified')
            elif row['evidence_outcome'] != 'supported':
                problems.append(f"{case} r{repeat} {mode}: outcome {row['evidence_outcome']!r}, not 'supported'")
            else:
                seconds[mode].append(row['seconds'])
    if not expected:
        problems.append('no primary supported cases were scheduled')
    medians = {mode: median(values) for mode, values in seconds.items()}
    reduction = None
    if not problems and medians['legacy']:
        reduction = 1 - medians['fast'] / medians['legacy']
    return dict(primary_cases=list(primary_cases), expected_runs_per_mode=len(expected), runs_used=dict((m, len(v)) for m, v in seconds.items()),
                median_seconds=medians, reduction=reduction, problems=problems,
                latency_target_met=reduction is not None and reduction >= LATENCY_TARGET)


def compare(rows, primary_cases=PRIMARY_CASES, repeats=2):
    """Comparison computed ONLY from independently re-verified records. rows: dict[(case, repeat, mode)] -> verified record.

    The release-relevant latency statistic is `primary` (reduction of median seconds). Per-pair fractional improvements and their median are
    additional DESCRIPTIVE fields and do not decide the gate.
    """
    pairs = []
    for case, repeat in sorted({(c, r) for c, r, _ in rows}):
        legacy, fast = rows.get((case, repeat, 'legacy')), rows.get((case, repeat, 'fast'))
        if legacy and fast:
            pairs.append(dict(case=case, repeat=repeat, legacy=legacy, fast=fast, improvement=1 - fast['seconds'] / legacy['seconds'] if legacy['seconds'] else None,
                              both_supported=legacy['evidence_outcome'] == 'supported' == fast['evidence_outcome']))
    supported = [p for p in pairs if p['both_supported']]
    mode_stats = {}
    for mode in MODES:
        mine = [r for (c, rep, m), r in rows.items() if m == mode]
        available = sum(r['available_subqueries'] for r in mine)
        mode_stats[mode] = dict(runs=len(mine), median_seconds=median([r['seconds'] for r in mine]),
                                model_calls=sum(r['model_calls'] for r in mine), retries=sum(r['retries'] for r in mine),
                                retrieval_calls=sum(r['retrieval_calls'] for r in mine),
                                cited_available_subqueries=sum(r['cited_available_subqueries'] for r in mine), available_subqueries=available,
                                routes=dict(collections.Counter(r['synthesis_route'] for r in mine)),
                                outcomes=dict(collections.Counter(r['evidence_outcome'] for r in mine)))
    order = {}
    for first in MODES:
        sample = [p['improvement'] for p in pairs if p['improvement'] is not None and rows_first(p) == first]
        order[f'{first}_first'] = dict(pairs=len(sample), median_improvement=median(sample))
    primary = primary_gate(rows, primary_cases, repeats)
    return dict(
        pairs=[dict(case=p['case'], repeat=p['repeat'], legacy_seconds=p['legacy']['seconds'], fast_seconds=p['fast']['seconds'], improvement=p['improvement'],
                    legacy_calls=p['legacy']['model_calls'], fast_calls=p['fast']['model_calls'], fast_route=p['fast']['synthesis_route'],
                    legacy_outcome=p['legacy']['evidence_outcome'], fast_outcome=p['fast']['evidence_outcome'],
                    legacy_cited=p['legacy']['cited_available_subqueries'], fast_cited=p['fast']['cited_available_subqueries'], both_supported=p['both_supported']) for p in pairs],
        per_mode=mode_stats, order_effect=order, primary_supported=primary,
        descriptive=dict(supported_pairs=len(supported), median_paired_improvement_supported_pairs=median([p['improvement'] for p in supported if p['improvement'] is not None]),
                         median_paired_improvement_all_pairs=median([p['improvement'] for p in pairs if p['improvement'] is not None]),
                         note='Descriptive only: a median of per-pair fractions is a different statistic from the gate (reduction of median seconds).'),
        preregistered=dict(
            latency_target=LATENCY_TARGET, latency_target_met=primary['latency_target_met'],
            model_calls_not_increased_in_every_pair=bool(pairs) and all(p['fast']['model_calls'] <= p['legacy']['model_calls'] for p in pairs),
            coverage_not_reduced_in_every_pair=bool(pairs) and all(p['fast']['cited_available_subqueries'] >= p['legacy']['cited_available_subqueries'] for p in pairs),
            expected_absent_withheld_in_every_run=all(r['unavailable_withheld'] for r in rows.values()),
            note='Computed from re-verified traces. The release decision and the full 30-case candidate check remain with the reviewer.'))


def rows_first(pair):
    return 'legacy' if pair['legacy']['position_in_pair'] == 0 else 'fast'


def audit_run_environment(directory, suite_digest, suite, corpus, original_provenance, ingest_report, root, strict_code, vb):
    """Everything the supervisor wrote around the paired run: artifact/code hashes (strict), lineage, run status and 4x2 service health."""
    problems, report = [], {}
    parent = directory.parent

    def load(name):
        path = parent / name
        if not path.is_file():
            problems.append(f'{name} missing next to the latency directory')
            return {}
        return json.loads(path.read_text())
    hashes = load('artifact-hashes.json')
    if hashes:
        report['artifacts'], found = vb.verify_artifacts(hashes, root, suite_digest, suite['corpus_sha256'], ingest_report, original_provenance, strict_code)
        problems += found
        lineage, lineage_problems = lib.check_report_lineage(ingest_report, ROOT)
        problems += lineage_problems
        recorded_previous = hashes.get('previous_ingest_report_sha256')
        if (lineage.get('previous_report_sha256') is not None or recorded_previous is not None) and recorded_previous != lineage.get('previous_report_sha256'):
            problems.append('artifact-hashes previous_ingest_report_sha256 differs from the report lineage')
        manifest = json.loads((directory / 'run-manifest.json').read_text())
        if manifest.get('code') != hashes.get('code'):
            problems.append('run-manifest code hashes differ from the supervisor artifact-hashes code hashes')
        report['ingest_lineage'] = lineage
    status = load('run-status.json')
    if not (status.get('status') in ('completed', 'failed') and status.get('owned_processes_stopped') is True and status.get('mode') == 'paired-latency'):
        problems.append('run-status.json missing, not a finished paired-latency run, or owned processes not confirmed stopped')
    report['run_status'] = status.get('status')
    health = {'before': load('health-before.json'), 'after': load('health-after.json')}
    for moment, body in health.items():
        sidecar, bridge = body.get('sidecar', {}), body.get('bridge', {})
        if not (sidecar.get('status') == 'ready' and bridge.get('status') == 'ready'):
            problems.append(f'{moment}: sidecar/bridge health not ready')
        for name, service in (('sidecar', sidecar), ('bridge', bridge)):
            if service.get('corpus_sha256') != suite['corpus_sha256']:
                problems.append(f'{moment}: {name} corpus fingerprint differs from the suite corpus')
        if (sidecar.get('inference_workers'), sidecar.get('threads')) != (4, 2):
            problems.append(f"{moment}: sidecar is not the 4x2 profile (workers={sidecar.get('inference_workers')}, threads={sidecar.get('threads')})")
        if (sidecar.get('admission') or {}).get('rejected', 0) != 0:
            problems.append(f'{moment}: sidecar admission rejected requests')
    report['health_profile'] = dict(workers=(health['before'].get('sidecar') or {}).get('inference_workers'), threads=(health['before'].get('sidecar') or {}).get('threads'))
    return problems, report


def audit_records(directory, scheduled, results, failures):
    """records.jsonl (the runner's own ledger) versus the schedule and the raw saved results: no extra, missing, duplicated or altered row."""
    problems = []
    path = directory / 'records.jsonl'
    if not path.is_file():
        return ['records.jsonl missing']
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except ValueError as exc:
        return [f'records.jsonl unreadable: {exc}']
    ids = [row.get('run_id') for row in rows]
    if sorted(map(str, ids)) != sorted(scheduled):
        problems.append(f'records.jsonl run IDs differ from the schedule (missing {sorted(set(scheduled) - set(ids))}, extra/duplicate {sorted(i for i in set(map(str, ids)) - set(scheduled) or [i for i in ids if ids.count(i) > 1])})')
    if ids != [r for r in scheduled if r in ids] and len(set(ids)) == len(ids):
        problems.append('records.jsonl is not in schedule order')
    for row in rows:
        run_id, info = row.get('run_id'), scheduled.get(row.get('run_id'))
        if info is None:
            continue
        for key in ('ordinal', 'pair', 'case', 'repeat', 'mode', 'position_in_pair'):
            if row.get(key) != info[key]:
                problems.append(f'{run_id}: records.jsonl {key}={row.get(key)!r} differs from the schedule {info[key]!r}')
        wall = row.get('wall_seconds')
        if not isinstance(wall, (int, float)) or wall <= 0:
            problems.append(f'{run_id}: missing or invalid wall_seconds')
            continue
        if run_id in results:
            raw = results[run_id]
            agent_seconds = row.get('agent_seconds')
            if not isinstance(agent_seconds, (int, float)) or abs(agent_seconds - raw['elapsed_seconds']) > 1e-9:
                problems.append(f'{run_id}: records.jsonl agent_seconds differs from the raw result elapsed_seconds')
            elif not 0 <= wall - agent_seconds <= MAX_WALL_OVERHEAD:
                problems.append(f'{run_id}: wall_seconds {wall:.3f} inconsistent with agent elapsed {agent_seconds:.3f}')
            if (row.get('agent_mode'), row.get('synthesis_route'), row.get('model_calls')) != (raw.get('execution_mode', 'legacy'), raw.get('synthesis_route', 'legacy'), len(raw['model_calls'])):
                problems.append(f'{run_id}: records.jsonl mode/route/model_calls differ from the raw result')
        elif run_id in failures:
            if row.get('status') != 'fail' or row.get('category') != failures[run_id].get('category'):
                problems.append(f'{run_id}: records.jsonl failure row differs from the saved failure record')
        else:
            problems.append(f'{run_id}: records.jsonl row without a saved result or failure')
    return problems


def verify(directory, suite_path, pin, corpus, original_provenance, ingest_report, root=ROOT, strict_code=True):
    import verify_broad_agent as vb
    directory = Path(directory)
    problems = []
    suite, suite_digest = lib.load_suite(suite_path, pin)
    if lib.sha256_file(corpus) != suite['corpus_sha256']:
        problems.append('corpus bytes differ from the corpus the suite was frozen against')
    manifest = json.loads((directory / 'run-manifest.json').read_text())
    for name, ok in (('primary supported cases are the preregistered ones', manifest.get('primary_supported_cases') == [c for c in PRIMARY_CASES if c in manifest.get('cases', [])]),
                     ('manifest names the pinned suite', manifest.get('suite_sha256') == suite_digest),
                     ('manifest names the suite corpus', manifest.get('corpus_sha256') == suite['corpus_sha256']),
                     ('saved suite is the pinned suite', (directory / 'suite.json').is_file() and lib.sha256_file(directory / 'suite.json') == suite_digest),
                     ('schedule within the 24-run cap and alternating', manifest.get('schedule') == schedule(manifest.get('cases', []), manifest.get('repeats', 0))),
                     ('limits unchanged', manifest.get('limits', {}).get('audits_active') == 1 and manifest['limits'].get('model_concurrency') == 2
                      and manifest['limits'].get('agent_concurrency') == 3)):
        if not ok:
            problems.append(f'manifest check failed: {name}')
    trusted = lib.build_trusted_sources(original_provenance, ingest_report)
    enrolled_path = directory.parent / 'agent-provenance.json'
    if enrolled_path.is_file() and json.loads(enrolled_path.read_text())['sources'] != trusted:
        problems.append('run enrollment differs from the independently rebuilt trusted source list')
    scheduled = {r['run_id']: r for r in manifest.get('schedule', [])}
    files = collections.defaultdict(set)
    for path in sorted((directory / 'runs').iterdir()) if (directory / 'runs').is_dir() else []:
        found = RUN_FILE.match(path.name)
        stem = path.name.split('.')[0]
        if not found or stem not in scheduled:
            problems.append(f'unexpected file runs/{path.name}')
        else:
            files[stem].add(('failure.' if found[5] else '') + found[6])
    cases = {c['id']: c for c in suite['cases']}
    results, failures = {}, {}
    for run_id in scheduled:
        present = files.get(run_id, set())
        if 'failure.json' in present:
            failures[run_id] = json.loads((directory / 'runs' / f'{run_id}.failure.json').read_text())
        elif {'json', 'md'} <= present:
            results[run_id] = json.loads((directory / 'runs' / f'{run_id}.json').read_text())
        else:
            problems.append(f'{run_id}: no complete result or failure record')
    needed = {e['id'] for r in results.values() for h in r['hops'] for e in h['evidence']}
    _, originals = lib.scan_corpus(corpus, {s['accession'] for s in trusted}, needed)
    if set(originals) != needed:
        problems.append('evidence passages missing from the corpus')
    by_scope = {(s['ticker'], s['filing_year']): s for s in trusted}
    records, rows = [], {}
    for run_id, info in scheduled.items():
        record = dict(run_id=run_id, case=info['case'], repeat=info['repeat'], mode=info['mode'], position_in_pair=info['position_in_pair'], problems=[])
        if run_id in failures:
            record.update(verdict='run_failed', failure_category=failures[run_id].get('category'), failure_error=str(failures[run_id].get('error'))[:300])
        elif run_id in results:
            checks = vb.Checks()
            markdown = (directory / 'runs' / f'{run_id}.md').read_text()
            try:
                audited = vb.audit_case(cases[info['case']], results[run_id], markdown, by_scope, originals, checks)
            except (KeyError, TypeError, IndexError) as exc:
                checks.req(False, f'{run_id}: malformed result JSON ({type(exc).__name__}: {exc})')
                audited = {}
            checks.req(results[run_id].get('execution_mode', 'legacy') == info['mode'], f'{run_id}: executed mode differs from the scheduled mode')
            record.update(audited)
            record['problems'] = checks.problems
            record['verdict'] = 'verified' if not checks.problems else 'verification_failed'
            if not checks.problems:
                record.update(stage_seconds(results[run_id]['model_calls']))
                rows[(info['case'], info['repeat'], info['mode'])] = dict(record, position_in_pair=info['position_in_pair'])
        else:
            record['verdict'] = 'omitted'
        records.append(record)
    environment_problems, environment = audit_run_environment(directory, suite_digest, suite, corpus, original_provenance, ingest_report, root, strict_code, vb)
    problems += environment_problems
    problems += audit_records(directory, scheduled, results, failures)
    summary = dict(
        directory=str(directory), environment=environment, suite_id=suite['suite_id'], suite_sha256=suite_digest, scheduled_runs=len(scheduled),
        verified=sum(r['verdict'] == 'verified' for r in records),
        run_failed=[dict(run_id=r['run_id'], category=r.get('failure_category')) for r in records if r['verdict'] == 'run_failed'],
        verification_failed=[r['run_id'] for r in records if r['verdict'] == 'verification_failed'], omitted=[r['run_id'] for r in records if r['verdict'] == 'omitted'],
        global_problems=problems, runs=records, comparison=compare(rows, tuple(manifest.get('primary_supported_cases', ())), manifest.get('repeats', 0)),
        scope='Wall-clock of live paired runs on a small frozen subset after independent structural re-verification. Not a throughput, tail-latency or answer-accuracy result.')
    summary['passed'] = not problems and summary['verified'] == len(scheduled)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    r = sub.add_parser('run')
    r.add_argument('--backend', required=True)
    r.add_argument('--model', default='haiku')
    r.add_argument('--corpus', required=True)
    r.add_argument('--provenance', required=True)
    r.add_argument('--suite', required=True)
    r.add_argument('--expected-suite-sha256')
    r.add_argument('--case', action='append', help=f'suite case id (repeatable); default {list(DEFAULT_CASES)}')
    r.add_argument('--repeats', type=int, choices=(1, 2, 3), default=2)
    r.add_argument('--output', required=True)
    v = sub.add_parser('verify')
    v.add_argument('--directory', type=Path, required=True, help='the latency/ directory written by `run`')
    v.add_argument('--suite', type=Path, default=ROOT / 'bench_kit/suites/broad-validation-v2.json')
    v.add_argument('--expected-suite-sha256')
    v.add_argument('--corpus', type=Path, default=ROOT / 'data/sec-scale-v3-evaluation-20261101/chunks.jsonl')
    v.add_argument('--original-provenance', type=Path, default=ROOT / 'data/sec/provenance.json')
    v.add_argument('--ingest-report', type=Path, default=ROOT / 'indexes/sec-scale-v3-20261101/report.json')
    v.add_argument('--root', type=Path, default=ROOT, help='repository root that recorded artifact paths are relative to')
    v.add_argument('--no-strict-code', action='store_true', help='report (do not fail on) code files changed since the run; default is strict')
    v.add_argument('--output', type=Path, help='default: <directory>/paired-latency-verification.json (never overwritten)')
    args = parser.parse_args(argv)
    if args.command == 'run':
        asyncio.run(run(args))
        return 0
    pin = args.expected_suite_sha256 or args.suite.with_suffix('.sha256').read_text().split()[0]
    result = verify(args.directory, args.suite, pin, args.corpus, args.original_provenance, args.ingest_report, args.root, not args.no_strict_code)
    with (args.output or args.directory / 'paired-latency-verification.json').open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'runs'}, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
