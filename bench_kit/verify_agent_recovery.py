#!/usr/bin/env python3
"""Offline composite verification of an INTERRUPTED full-suite run plus its fresh continuation batch.

Why this exists: `full-run-01` was interrupted by the execution environment after an exact prefix of the frozen 30-case suite was
saved (its run-status stayed `started`); the unfinished suffix was run in a separate fresh batch. Neither directory alone is a
normal, cleanly finished 30-case run, and this tool never claims otherwise. It never edits, repairs or rewrites any raw record, status
file or existing verifier. It calls the existing `verify_broad_agent.verify` in memory (strict code comparison) on each directory and
then fails closed unless all of these hold:

* interrupted batch: its manifest selected the full frozen suite; the saved valid cases are exactly the recorded completed PREFIX of the
  frozen order; the only omitted cases are exactly the recorded uncompleted suffix; no case failures; the ONLY permitted global problem is
  the known "run-status unfinished" one (and run-status must really say `started`); the only permitted evaluator-summary discrepancies are
  the two that a truncated run necessarily has (no report.json, ledger lacking the suffix);
* continuation batch: selected exactly the recorded suffix (in frozen order) before its first request, started after the interruption
  record, finished `completed` with owned processes stopped, and passes the existing verifier strictly and its release gates;
* the two batches are disjoint and their union is exactly the frozen suite; suite/corpus/report/provenance/enrollment/index/model/code
  hashes are identical in both; verifier + helper sources are code-frozen in both runs' recorded hashes;
* every completed case passed the existing raw-source verification; every available subquery is cited, every unavailable scope
  withheld, every mixed case fully supported on its available side.

The result is a separate exclusive-create summary. It does not start services and cannot establish process cleanup (Codex records that
separately). Structural/source checks are not human semantic gold. Timings are descriptive, from two batches, not a controlled comparison.
"""
import argparse
import datetime
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_suite as lib  # noqa: E402
import verify_broad_agent as base  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
KNOWN_UNFINISHED = 'run-status.json missing, unfinished, or owned processes not confirmed stopped'
SAME_IN_BOTH = ('suite_sha256', 'corpus_sha256', 'ingest_report_sha256', 'original_provenance_sha256', 'previous_ingest_report_sha256',
                'ingest_lineage', 'artifact_paths', 'enrolled_source_manifest_sha256', 'enrolled_filings', 'vector_index', 'reranker_model',
                'sparse_index', 'code', 'git_head')
FROZEN_HELPERS = ('bench_kit/verify_broad_agent.py', 'bench_kit/agent_suite.py')
OK_OUTCOMES = ('supported', 'expected_unavailable')


def read_json(path):
    return json.loads(Path(path).read_text())


def load_batch(directory):
    """Raw (never modified) side files of one batch directory. Missing files become {}/[] so checks fail closed."""
    directory = Path(directory)

    def maybe(relative):
        path = directory / relative
        return read_json(path) if path.is_file() else {}
    ledger = directory / 'evaluation' / 'records.jsonl'
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()] if ledger.is_file() else []
    return dict(directory=str(directory), status=maybe('run-status.json'), manifest=maybe('evaluation/run-manifest.json'),
                hashes=maybe('artifact-hashes.json'), provenance=maybe('agent-provenance.json'), ledger=rows)


def parse_time(text):
    try:
        return datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def check_interrupted(first, batch, interruption, frozen_ids):
    """Problems for the interrupted batch. `first` is the existing verifier's in-memory summary of that directory."""
    problems = []
    need = problems.append
    completed, pending = interruption.get('completed'), interruption.get('pending')
    if not isinstance(completed, list) or not isinstance(pending, list) or interruption.get('failed') != []:
        return ['interruption record must list completed and pending cases and no failed cases']
    if not completed or not pending:
        need('interruption record must have a non-empty completed prefix and a non-empty pending suffix')
    if completed + pending != frozen_ids:
        need('interruption record completed+pending is not exactly the frozen suite order (prefix + suffix)')
    if batch['manifest'].get('selected') != frozen_ids or batch['manifest'].get('full_suite') is not True:
        need('interrupted batch manifest did not select the full frozen suite')
    if first['expected_case_ids'] != frozen_ids or not first['full_suite']:
        need('existing verifier did not treat the interrupted batch as the full frozen suite')
    ok = [r['id'] for r in first['cases'] if r['verdict'] == 'verified']
    if ok != completed:
        need(f'saved valid cases {ok} are not exactly the recorded completed prefix {completed}')
    if sorted(first['omitted']) != sorted(pending):
        need(f'omitted cases {first["omitted"]} are not exactly the recorded uncompleted suffix {pending}')
    for record in first['cases']:
        if record['id'] in pending and record['state'] != 'omitted':
            need(f'{record["id"]}: has saved files but the interruption record says it was not completed')
    if first['run_failed'] or first['verification_failed'] != len(pending) or any(first['failures_by_category'].values()):
        need('interrupted batch has case failures or verification failures beyond the omitted suffix')
    if first['global_problems'] != [KNOWN_UNFINISHED]:
        need(f'unexpected global problems (only the known unfinished-status problem is permitted): {first["global_problems"]}')
    if batch['status'].get('status') != 'started' or 'finished_utc' in batch['status']:
        need('interrupted batch run-status is not the untouched unfinished "started" record')
    allowed = {'evaluation/report.json missing',
               f'records.jsonl: case IDs differ from the frozen selection (missing {sorted(pending)}, extra [])'}
    if set(first['evaluator_summary_discrepancies']) != allowed or len(first['evaluator_summary_discrepancies']) != len(allowed):
        need(f'unexpected evaluator-summary discrepancies: {first["evaluator_summary_discrepancies"]}')
    ledger_ids = [row.get('id') for row in batch['ledger']]
    if ledger_ids != completed or any(row.get('status') != 'pass' for row in batch['ledger']):
        need('interrupted batch records.jsonl is not exactly the completed prefix with pass rows')
    return problems


def check_continuation(second, batch, interruption, frozen_ids):
    problems = []
    need = problems.append
    pending = interruption.get('pending') or []
    if batch['manifest'].get('selected') != pending or second['expected_case_ids'] != pending:
        need('continuation did not select exactly the recorded uncompleted suffix (in frozen order)')
    if batch['manifest'].get('full_suite') is not False or second['full_suite']:
        need('continuation is not marked as a partial selection')
    if not second['passed'] or second['global_problems'] or second['evaluator_summary_discrepancies']:
        need(f'continuation did not pass the existing strict verifier: {second["global_problems"]} {second["evaluator_summary_discrepancies"]}')
    if second['verified'] != len(pending) or second['run_failed'] or second['verification_failed'] or second['omitted']:
        need('continuation does not have a clean completed result for every selected case')
    if not second['release_gates']['all_checked_gates_met']:
        need('continuation release gates not met')
    status = batch['status']
    if status.get('status') != 'completed' or status.get('owned_processes_stopped') is not True or status.get('evaluator_exit_code') != 0:
        need('continuation run-status is not a clean completed record')
    started, checked = parse_time(status.get('started_utc')), parse_time(interruption.get('checked_utc'))
    if started is None or checked is None or started <= checked:
        need('continuation did not start after the interruption record was written')
    return problems


def check_union(first, second, a, b, interruption, frozen_ids, suite_case_count):
    problems = []
    need = problems.append
    done_a = [r['id'] for r in first['cases'] if r['verdict'] == 'verified']
    done_b = [r['id'] for r in second['cases'] if r['verdict'] == 'verified']
    if set(done_a) & set(done_b):
        need(f'batches overlap: {sorted(set(done_a) & set(done_b))}')
    if len(done_a) != len(set(done_a)) or len(done_b) != len(set(done_b)):
        need('duplicate completed case ids inside a batch')
    if done_a + done_b != frozen_ids or len(frozen_ids) != suite_case_count:
        need('union of completed cases is not exactly the frozen suite in order')
    for key in SAME_IN_BOTH:
        if a['hashes'].get(key) is None or a['hashes'].get(key) != b['hashes'].get(key):
            need(f'artifact-hashes {key} differs between the batches or is missing')
    if not a['provenance'] or a['provenance'].get('sources') != b['provenance'].get('sources') or a['provenance'].get('corpus_sha256') != b['provenance'].get('corpus_sha256'):
        need('batch enrollments differ')
    for name, batch in (('interrupted', a), ('continuation', b)):
        for helper in FROZEN_HELPERS:
            if helper not in batch['hashes'].get('code', {}):
                need(f'{name} batch did not record {helper} in its code hashes')
    for name, summary in (('interrupted', first), ('continuation', second)):
        code = summary['artifact_verification'].get('code', {})
        if code.get('strict') is not True or code.get('changed') or code.get('missing') or code.get('required_but_unrecorded'):
            need(f'{name} batch code hashes are not strictly unchanged')
    for summary in (first, second):
        for record in summary['cases']:
            if record['verdict'] != 'verified':
                continue
            if record.get('problems') or record.get('evidence_outcome') not in OK_OUTCOMES:
                need(f'{record["id"]}: source verification not clean or outcome {record.get("evidence_outcome")!r}')
            if record.get('cited_available_subqueries') != record.get('available_subqueries'):
                need(f'{record["id"]}: not every available subquery is cited')
            if record.get('unavailable_subqueries') and not record.get('unavailable_withheld'):
                need(f'{record["id"]}: an unavailable scope was not withheld')
            if record.get('mixed_availability') and record.get('mixed_result') != 'mixed_fully_supported':
                need(f'{record["id"]}: mixed case not fully supported on its available side')
    return problems


def case_rows(first, second, a, b):
    rows = []
    for tag, summary, batch in (('interrupted-prefix', first, a), ('continuation-suffix', second, b)):
        for r in summary['cases']:
            if r['verdict'] != 'verified':
                continue
            rows.append(dict(id=r['id'], batch=tag, source_batch=batch['directory'], kind=r['kind'], outcome=r['evidence_outcome'],
                             agent_mode=r['agent_mode'], synthesis_route=r['synthesis_route'], citations=r['citations'], retries=r['retries'],
                             retrieval_calls=r['retrieval_calls'], cli_invocations=r['model_calls'], available_subqueries=r['available_subqueries'],
                             cited_available_subqueries=r['cited_available_subqueries'], unavailable_subqueries=r['unavailable_subqueries'],
                             mixed_availability=r['mixed_availability'], flags=r['flags'], seconds=r['seconds']))
    return rows


def source_hashes(paths):
    return {str(p): lib.sha256_file(p) for p in paths}


def recover(first_dir, second_dir, interruption_path, suite_path, pin, corpus, original_provenance, ingest_report, root=ROOT,
            expected_corpus_sha256=None, expected_report_sha256=None, verify=None):
    verify = verify or base.verify
    interruption = read_json(interruption_path)
    a, b = load_batch(first_dir), load_batch(second_dir)
    suite, suite_digest = lib.load_suite(suite_path, pin)
    frozen_ids = suite['case_ids']
    pins = []
    if expected_corpus_sha256 and (lib.sha256_file(corpus) != expected_corpus_sha256 or suite['corpus_sha256'] != expected_corpus_sha256):
        pins.append('corpus does not match the pinned hash')
    if expected_report_sha256 and lib.sha256_file(ingest_report) != expected_report_sha256:
        pins.append('ingest report does not match the pinned hash')
    first = verify(first_dir, suite_path, pin, corpus, original_provenance, ingest_report, False, root, True)
    second = verify(second_dir, suite_path, pin, corpus, original_provenance, ingest_report, True, root, True)
    problems = pins + check_interrupted(first, a, interruption, frozen_ids) + check_continuation(second, b, interruption, frozen_ids)
    problems += check_union(first, second, a, b, interruption, frozen_ids, suite['case_count'])
    rows = case_rows(first, second, a, b)
    seconds = [r['seconds'] for r in rows]
    own = Path(__file__).resolve()
    helpers = [Path(base.__file__).resolve(), Path(lib.__file__).resolve()]
    summary = dict(
        passed=not problems, problems=problems,
        kind='composite recovery verification of an interrupted run plus a fresh continuation batch; NOT one uninterrupted normal run',
        suite_id=suite['suite_id'], suite_sha256=suite_digest, corpus_sha256=suite['corpus_sha256'],
        batches=dict(interrupted=dict(directory=a['directory'], status=a['status'].get('status'), completed=interruption.get('completed'), omitted=first['omitted']),
                     continuation=dict(directory=b['directory'], status=b['status'].get('status'), selected=b['manifest'].get('selected'))),
        interruption_record=str(interruption_path), interruption_record_sha256=lib.sha256_file(Path(interruption_path)),
        counts=dict(cases=len(rows), citations=sum(r['citations'] for r in rows), retrieval_calls=sum(r['retrieval_calls'] for r in rows),
                    retries=sum(r['retries'] for r in rows), cli_invocations=sum(r['cli_invocations'] for r in rows),
                    available_subqueries=sum(r['available_subqueries'] for r in rows),
                    cited_available_subqueries=sum(r['cited_available_subqueries'] for r in rows),
                    outcomes={k: sum(r['outcome'] == k for r in rows) for k in OK_OUTCOMES},
                    routes={k: sum(r['synthesis_route'] == k for r in rows) for k in sorted({r['synthesis_route'] for r in rows})}),
        per_batch_unique_source_passages=dict(interrupted=first['unique_source_passages_checked'], continuation=second['unique_source_passages_checked'],
                                              note='Per-batch counts; the cross-batch union is not derivable from the verifier summaries.'),
        timing=dict(median_seconds=statistics.median(seconds) if seconds else None, max_seconds=max(seconds) if seconds else None,
                    note='Descriptive completed-case seconds across two batches; not a controlled comparison and not an SLA, p99 or cost claim. '
                         'In-flight work lost at the interruption is unknown and not counted.'),
        cases=rows,
        verifier_sources=source_hashes([own] + helpers),
        existing_verifier_summaries=dict(
            interrupted=dict(passed=first['passed'], verified=first['verified'], global_problems=first['global_problems'],
                             evaluator_summary_discrepancies=first['evaluator_summary_discrepancies']),
            continuation=dict(passed=second['passed'], verified=second['verified'],
                              gates_met=second['release_gates']['all_checked_gates_met'])),
        not_established=['process/service cleanup (recorded separately by the reviewer)', 'any normal supervisor cleanup or health-after record for a single 30-case run',
                         'human semantic correctness (fixtures are source-informed, not human gold)', 'usage consumed by unsaved in-flight work at the interruption'])
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--interrupted-directory', type=Path, required=True)
    parser.add_argument('--continuation-directory', type=Path, required=True)
    parser.add_argument('--interruption-record', type=Path, required=True)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--expected-suite-sha256', required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--expected-corpus-sha256', required=True)
    parser.add_argument('--original-provenance', type=Path, default=ROOT / 'data/sec/provenance.json')
    parser.add_argument('--ingest-report', type=Path, required=True)
    parser.add_argument('--expected-ingest-report-sha256', required=True)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True, help='Created exclusively; never overwritten')
    args = parser.parse_args()
    result = recover(args.interrupted_directory, args.continuation_directory, args.interruption_record, args.suite, args.expected_suite_sha256,
                     args.corpus, args.original_provenance, args.ingest_report, args.root, args.expected_corpus_sha256, args.expected_ingest_report_sha256)
    with args.output.open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('cases',)}, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
