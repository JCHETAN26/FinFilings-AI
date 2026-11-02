#!/usr/bin/env python3
"""Release-comparison gates for adopting the parser-v3 corpus, computed from saved run directories only.

The thresholds were set by the manager BEFORE any v3 run and are constants here; they are never read from the candidate and
never adjusted to a result. Nothing is measured or invented by this tool: it compares fields that the existing evaluation,
load and independent-verification tools already wrote, and fails closed on any missing or mismatched input.

Gates
  quality   same 100 queries; reranked nDCG@10 may not drop more than 0.005 absolute from the v2 baseline; PQ recall@10
            vs exact float32 may not drop more than 0.02; native payload reduction >= 90%
  load      matched 50-client run: identical configuration and query set, zero failures on both endpoints in BOTH the baseline
            and the candidate (a baseline with failures is not a valid control), hybrid p99
            (all attempts) regression <= 10%
  agent     independent verification of the 30-case broad-validation-v2 run: every case verified, no transport/model/
            structural failures or global problems, release gates met (>= 80% available-subquery citation coverage,
            unavailable scopes withheld, mixed available sides fully supported)

Inputs must be finite: NaN/Infinity literals, 1e999-style overflow, booleans or strings where a number is required make the tool
exit 2 without writing a result.

Not checked here (run and read separately): Go/Python test suites, race, vet and source-integrity hashing.
"""
import argparse
import json
import math
from pathlib import Path
import sys

NDCG_MAX_DROP = 0.005
RECALL_MAX_DROP = 0.02
REDUCTION_MIN_PERCENT = 90.0
P99_MAX_REGRESSION = 0.10
MATCHED_PARAMETERS = ('inference_workers', 'threads_per_job', 'configured_intraop_thread_budget', 'load_workers', 'requests_per_endpoint',
                      'queries', 'candidate_limit', 'max_tokens', 'reranker_backend', 'rerank_batch_size', 'dense_batch_size',
                      'dense_batch_wait_ms', 'dense_batch_trigger')
REQUIRED_LOAD = dict(requests=200, workers=50)
BASELINE_NDCG = 0.5537183500706869
BASELINE_RECALL = 0.838
EPSILON = 1e-12


class GateInputError(ValueError):
    """An input report is malformed or holds a non-finite number; the comparison fails closed."""


def _reject_constant(name):
    raise GateInputError(f'non-finite JSON constant {name}')


def _check_finite(value, where='$'):
    if isinstance(value, float) and not math.isfinite(value):
        raise GateInputError(f'non-finite number at {where}')
    if isinstance(value, dict):
        for key, item in value.items():
            _check_finite(item, f'{where}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_finite(item, f'{where}[{index}]')
    return value


def read(path):
    """Parse JSON refusing NaN/Infinity literals and out-of-range numbers such as 1e999 (which Python reads as infinity)."""
    return _check_finite(json.loads(Path(path).read_text(), parse_constant=_reject_constant), str(path))


def num(value, label):
    """A real, finite, non-boolean number or GateInputError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GateInputError(f'{label} is not a finite number: {value!r}')
    return value


def query_ids(directory):
    path = Path(directory) / 'evaluation' / 'per-query.jsonl'
    return [json.loads(line)['id'] for line in path.read_text().splitlines() if line.strip()]


def gate(name, passed, **detail):
    return dict(name=name, passed=bool(passed), **detail)


def quality_gates(baseline, candidate, base_ids, cand_ids, expected_ndcg, expected_recall):
    b, c = num(baseline['ndcg_at_10']['reranked'], 'baseline nDCG'), num(candidate['ndcg_at_10']['reranked'], 'candidate nDCG')
    br = num(baseline['pq_recall_at_10_vs_exact_float32'], 'baseline PQ recall')
    cr = num(candidate['pq_recall_at_10_vs_exact_float32'], 'candidate PQ recall')
    reduction = num(candidate['vector_memory']['exact_native_payload_reduction_percent'], 'candidate payload reduction')
    num(expected_ndcg, 'stated nDCG')
    num(expected_recall, 'stated recall')
    return [
        gate('baseline_matches_stated_v2_values', abs(b - expected_ndcg) < EPSILON and abs(br - expected_recall) < EPSILON,
             baseline_ndcg=b, stated_ndcg=expected_ndcg, baseline_recall=br, stated_recall=expected_recall),
        gate('same_100_development_queries', baseline['evaluated_queries'] == candidate['evaluated_queries'] == 100 and base_ids == cand_ids and len(set(cand_ids)) == 100,
             baseline_queries=baseline['evaluated_queries'], candidate_queries=candidate['evaluated_queries'], identical_ids=base_ids == cand_ids),
        gate('hybrid_rerank_ndcg_drop_within_0.005', b - c <= NDCG_MAX_DROP + EPSILON, baseline=b, candidate=c, drop=b - c, max_drop=NDCG_MAX_DROP),
        gate('pq_recall_at_10_drop_within_0.02', br - cr <= RECALL_MAX_DROP + EPSILON, baseline=br, candidate=cr, drop=br - cr, max_drop=RECALL_MAX_DROP),
        gate('payload_reduction_at_least_90_percent', reduction >= REDUCTION_MIN_PERCENT, candidate_percent=reduction, minimum=REDUCTION_MIN_PERCENT),
    ]


def load_gates(base_load, cand_load, base_params, cand_params):
    gates = []
    for endpoint in ('hybrid', 'dense'):
        for label, report in (('baseline', base_load), ('candidate', cand_load)):
            section = report[endpoint]
            gates.append(gate(f'{label}_{endpoint}_shape', section['requests'] == REQUIRED_LOAD['requests'] and section['workers'] == REQUIRED_LOAD['workers'],
                              requests=section['requests'], workers=section['workers']))
    mismatched = {k: (base_params.get(k), cand_params.get(k)) for k in MATCHED_PARAMETERS if base_params.get(k) != cand_params.get(k)}
    gates.append(gate('matched_configuration', not mismatched and base_load['query_sha256'] == cand_load['query_sha256']
                      and base_load['warmup_per_endpoint'] == cand_load['warmup_per_endpoint'] and base_load['load_model'] == cand_load['load_model'],
                      mismatched=mismatched, query_sha256_equal=base_load['query_sha256'] == cand_load['query_sha256']))
    for endpoint in ('hybrid', 'dense'):
        for label, report in (('baseline', base_load), ('candidate', cand_load)):
            section = report[endpoint]
            failures, successes, requests = (num(section[k], f'{label} {endpoint} {k}') for k in ('failures', 'successes', 'requests'))
            gates.append(gate(f'{label}_{endpoint}_zero_failures', failures == 0 and successes == requests and set(section['statuses']) == {'200'},
                              failures=failures, successes=successes, statuses=section['statuses']))
    base_p99 = num(base_load['hybrid']['all_attempt_latency']['p99_ns'], 'baseline hybrid p99')
    cand_p99 = num(cand_load['hybrid']['all_attempt_latency']['p99_ns'], 'candidate hybrid p99')
    if base_p99 <= 0 or cand_p99 <= 0:
        raise GateInputError('hybrid p99 must be positive')
    regression = cand_p99 / base_p99 - 1
    gates.append(gate('hybrid_p99_regression_within_10_percent', regression <= P99_MAX_REGRESSION + EPSILON,
                      baseline_p99_seconds=base_p99 / 1e9, candidate_p99_seconds=cand_p99 / 1e9, regression=regression, max_regression=P99_MAX_REGRESSION,
                      note='A failure here is reported as is; a rerun is justified only for an identified, unresolved source of noise, and every attempt is preserved.'))
    dense_base = num(base_load['dense']['all_attempt_latency']['p99_ns'], 'baseline dense p99')
    dense_cand = num(cand_load['dense']['all_attempt_latency']['p99_ns'], 'candidate dense p99')
    gates.append(gate('dense_p99_reported_not_gated', True, baseline_p99_seconds=dense_base / 1e9, candidate_p99_seconds=dense_cand / 1e9, informational=True))
    return gates


def agent_gates(verification, expected_corpus_sha256, expected_suite_id):
    summary = verification
    gates_block = summary.get('release_gates', {})
    categories = summary.get('failures_by_category', {})
    coverage = gates_block.get('citation_coverage_of_available_subqueries', {})
    return [
        gate('suite_and_corpus_bound', summary.get('suite_id') == expected_suite_id and summary.get('corpus_sha256') == expected_corpus_sha256 and summary.get('full_suite') is True,
             suite_id=summary.get('suite_id'), corpus_sha256=summary.get('corpus_sha256'), full_suite=summary.get('full_suite')),
        gate('all_30_cases_verified', summary.get('frozen_case_count') == 30 and summary.get('verified') == 30 and summary.get('run_failed') == 0
             and summary.get('verification_failed') == 0 and not summary.get('omitted'),
             verified=summary.get('verified'), run_failed=summary.get('run_failed'), verification_failed=summary.get('verification_failed'), omitted=summary.get('omitted')),
        gate('no_transport_model_or_structural_failures', all(categories.get(k) == 0 for k in ('transport', 'model', 'structural')), failures_by_category=categories),
        gate('no_global_problems_or_summary_discrepancies', summary.get('passed') is True and not summary.get('global_problems') and not summary.get('evaluator_summary_discrepancies'),
             verifier_passed=summary.get('passed'), global_problems=summary.get('global_problems'), discrepancies=summary.get('evaluator_summary_discrepancies')),
        gate('unavailable_scopes_withheld', gates_block.get('unavailable_scopes_publish_zero_claims') is True),
        gate('mixed_available_sides_fully_supported', gates_block.get('mixed_cases_preserve_unavailable_and_fully_support_available') is True, mixed_cases=summary.get('mixed_cases')),
        gate('available_subquery_citation_coverage_at_least_80_percent', coverage.get('met') is True and coverage.get('threshold') == 0.8, coverage=coverage),
        gate('verifier_release_gates_all_met', gates_block.get('all_checked_gates_met') is True),
    ]


def compare(baseline_run, candidate_run, verification=None, candidate_provenance=None, suite_id='broad-validation-v2',
            expected_ndcg=BASELINE_NDCG, expected_recall=BASELINE_RECALL):
    base_dir, cand_dir = Path(baseline_run), Path(candidate_run)
    sections = dict(quality=quality_gates(read(base_dir / 'evaluation/report.json'), read(cand_dir / 'evaluation/report.json'),
                                          query_ids(base_dir), query_ids(cand_dir), expected_ndcg, expected_recall),
                    load=load_gates(read(base_dir / 'load/report.json'), read(cand_dir / 'load/report.json'),
                                    read(base_dir / 'parameters.json'), read(cand_dir / 'parameters.json')))
    if verification is not None and candidate_provenance is not None:
        sections['agent'] = agent_gates(read(verification), read(candidate_provenance)['corpus_sha256'], suite_id)
    evaluated = {name: all(g['passed'] for g in gates) for name, gates in sections.items()}
    required = ('quality', 'load', 'agent')
    return dict(thresholds=dict(ndcg_max_drop=NDCG_MAX_DROP, pq_recall_max_drop=RECALL_MAX_DROP, payload_reduction_min_percent=REDUCTION_MIN_PERCENT,
                                hybrid_p99_max_regression=P99_MAX_REGRESSION),
                sections=sections, section_passed=evaluated, sections_not_evaluated=[s for s in required if s not in sections],
                passed=all(s in sections for s in required) and all(evaluated.values()),
                not_checked_by_this_tool=['Go and Python test suites', 'go test -race', 'go vet', 'source-integrity hashing', 'public-snapshot secret/artifact scan'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--baseline-run', type=Path, required=True, help='v2 run directory with evaluation/, load/ and parameters.json')
    parser.add_argument('--candidate-run', type=Path, required=True, help='v3 run directory with the same layout')
    parser.add_argument('--agent-verification', type=Path, help='independent-broad-verification.json of the broad-validation-v2 run')
    parser.add_argument('--candidate-provenance', type=Path, help='v3 data provenance.json (binds the agent run to the v3 corpus hash)')
    parser.add_argument('--suite-id', default='broad-validation-v2')
    parser.add_argument('--output', type=Path, required=True, help='new file; never overwritten')
    args = parser.parse_args(argv)
    try:
        result = compare(args.baseline_run, args.candidate_run, args.agent_verification, args.candidate_provenance, args.suite_id)
        text = json.dumps(result, indent=2, allow_nan=False) + '\n'  # never emits NaN/Infinity
    except (GateInputError, ValueError, KeyError, TypeError, OSError) as exc:
        print(f'INVALID INPUT, no result written: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    with args.output.open('x') as stream:
        stream.write(text)
    for name, gates in result['sections'].items():
        for g in gates:
            print(f"{'PASS' if g['passed'] else 'FAIL'}  {name}: {g['name']}")
    print('not evaluated:', result['sections_not_evaluated'] or 'none')
    print('OVERALL', 'PASS' if result['passed'] else 'NOT PASSED')
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
