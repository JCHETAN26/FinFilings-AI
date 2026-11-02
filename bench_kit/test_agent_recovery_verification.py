"""Contract and mutation tests for bench_kit/verify_agent_recovery.py.

TEST DOUBLES ONLY: the summaries, side files and the suite below are small hand-built stand-ins shaped like the existing verifier's
output (the existing `verify` is replaced by a stub). They contain no model output and no measurement. These tests prove the composite
checks fail closed; they say nothing about the real 18+12 saved runs, which the tool verifies against the real artifacts.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bench_kit'))
import verify_agent_recovery as rec  # noqa: E402

FROZEN = [f'c{i:02d}' for i in range(1, 7)]
DONE, PENDING = FROZEN[:4], FROZEN[4:]
CODE = {'bench_kit/verify_broad_agent.py': 'h1', 'bench_kit/agent_suite.py': 'h2', 'py/agent/fast.py': 'h3'}
HASHES = {k: f'v-{k}' for k in rec.SAME_IN_BOTH}
HASHES['code'] = CODE
INTERRUPTION = dict(checked_utc='2026-11-02T17:14:45+00:00', completed=DONE, failed=[], pending=PENDING)


def record(cid):
    return dict(id=cid, kind='regression', state='completed', verdict='verified', problems=[], evidence_outcome='supported', agent_mode='fast',
                synthesis_route='host_quotes', citations=3, retries=1, retrieval_calls=2, model_calls=3, available_subqueries=2,
                cited_available_subqueries=2, unavailable_subqueries=[], unavailable_withheld=True, mixed_availability=False, mixed_result=None,
                flags=[], seconds=10.0)


def omitted(cid):
    return dict(id=cid, kind='regression', state='omitted', verdict='verification_failed', problems=[f'{cid}: OMITTED'])


def summaries():
    code = dict(strict=True, changed=[], missing=[], required_but_unrecorded=[])
    first = dict(expected_case_ids=FROZEN, full_suite=True, cases=[record(c) for c in DONE] + [omitted(c) for c in PENDING], omitted=list(PENDING),
                 run_failed=0, verification_failed=len(PENDING), failures_by_category=dict(transport=0, model=0, structural=0),
                 global_problems=[rec.KNOWN_UNFINISHED], artifact_verification=dict(code=code), passed=False, verified=len(DONE),
                 unique_source_passages_checked=5,
                 evaluator_summary_discrepancies=['evaluation/report.json missing',
                                                  f'records.jsonl: case IDs differ from the frozen selection (missing {PENDING}, extra [])'])
    second = dict(expected_case_ids=PENDING, full_suite=False, cases=[record(c) for c in PENDING], omitted=[], run_failed=0, verification_failed=0,
                  failures_by_category=dict(transport=0, model=0, structural=0), global_problems=[], evaluator_summary_discrepancies=[],
                  artifact_verification=dict(code=copy.deepcopy(code)), passed=True, verified=len(PENDING), unique_source_passages_checked=4,
                  release_gates=dict(all_checked_gates_met=True))
    return first, second


def batches():
    a = dict(directory='first', status=dict(status='started'), manifest=dict(selected=FROZEN, full_suite=True), hashes=copy.deepcopy(HASHES),
             provenance=dict(sources=[1], corpus_sha256='c'), ledger=[dict(id=c, status='pass') for c in DONE])
    b = dict(directory='second', status=dict(status='completed', owned_processes_stopped=True, evaluator_exit_code=0, started_utc='2026-11-02T17:14:53+00:00'),
             manifest=dict(selected=PENDING, full_suite=False), hashes=copy.deepcopy(HASHES), provenance=dict(sources=[1], corpus_sha256='c'), ledger=[])
    return a, b


def problems(first, second, a, b, interruption=None):
    interruption = interruption or INTERRUPTION
    return (rec.check_interrupted(first, a, interruption, FROZEN) + rec.check_continuation(second, b, interruption, FROZEN)
            + rec.check_union(first, second, a, b, interruption, FROZEN, len(FROZEN)))


class RecoveryContract(unittest.TestCase):
    def setUp(self):
        self.first, self.second = summaries()
        self.a, self.b = batches()

    def assertFails(self, fragment=''):
        found = problems(self.first, self.second, self.a, self.b)
        self.assertTrue(found and any(fragment in p for p in found), found)

    def test_baseline_double_passes(self):
        self.assertEqual(problems(self.first, self.second, self.a, self.b), [])

    def test_prefix_must_be_exact(self):
        self.first['cases'][1], self.first['cases'][2] = self.first['cases'][2], self.first['cases'][1]
        self.assertFails('completed prefix')

    def test_gap_in_saved_prefix(self):  # a middle case omitted instead of the suffix
        self.first['cases'][1] = omitted('c02')
        self.first['omitted'] = ['c02'] + PENDING
        self.assertFails()

    def test_extra_saved_case_beyond_record(self):
        self.first['cases'][4] = record('c05')
        self.first['omitted'] = ['c06']
        self.assertFails()

    def test_duplicate_across_batches(self):
        self.second['cases'][0] = record('c04')
        self.assertFails('overlap')

    def test_union_missing_a_case(self):
        self.second['cases'].pop()
        self.assertFails('union')

    def test_continuation_selected_wrong_suffix(self):
        self.b['manifest']['selected'] = PENDING[:1]
        self.assertFails('exactly the recorded uncompleted suffix')

    def test_interrupted_global_error_other_than_known_is_rejected(self):
        self.first['global_problems'].append('run enrollment differs from the independently rebuilt trusted source list')
        self.assertFails('unexpected global problems')

    def test_unfinished_error_not_tolerated_without_started_status(self):
        self.a['status'] = dict(status='completed', owned_processes_stopped=True)
        self.assertFails('untouched unfinished')

    def test_known_error_missing_is_also_rejected(self):  # a "clean" first batch contradicts the interruption record
        self.first['global_problems'] = []
        self.assertFails('unexpected global problems')

    def test_unfinished_error_never_tolerated_in_continuation(self):
        self.second['global_problems'] = [rec.KNOWN_UNFINISHED]
        self.second['passed'] = False
        self.assertFails('continuation did not pass')

    def test_case_failure_in_first_batch(self):
        self.first['cases'][0].update(verdict='run_failed', state='run_failed')
        self.first['run_failed'] = 1
        self.assertFails()

    def test_unexpected_summary_discrepancy(self):
        self.first['evaluator_summary_discrepancies'].append('c01: reported seconds differ from the saved trace')
        self.assertFails('discrepancies')

    def test_changed_source_hash_or_code(self):
        self.b['hashes']['corpus_sha256'] = 'other'
        self.assertFails('corpus_sha256')

    def test_runtime_code_changed(self):
        self.second['artifact_verification']['code']['changed'] = ['py/agent/fast.py']
        self.assertFails('strictly unchanged')

    def test_helpers_must_be_code_frozen(self):
        del self.a['hashes']['code']['bench_kit/verify_broad_agent.py']
        self.assertFails('verify_broad_agent.py')

    def test_enrollment_differs(self):
        self.b['provenance']['sources'] = [2]
        self.assertFails('enrollments differ')

    def test_unfinished_continuation(self):
        self.b['status'] = dict(status='started')
        self.assertFails('clean completed')

    def test_continuation_must_start_after_interruption(self):
        self.b['status']['started_utc'] = '2026-11-02T17:00:00+00:00'
        self.assertFails('after the interruption')

    def test_available_subquery_not_cited(self):
        self.second['cases'][0]['cited_available_subqueries'] = 1
        self.assertFails('not every available')

    def test_unavailable_not_withheld_and_mixed_gap(self):
        self.first['cases'][0].update(unavailable_subqueries=[0], unavailable_withheld=False)
        self.assertFails('not withheld')
        self.first, _ = summaries()
        self.first['cases'][0].update(mixed_availability=True, mixed_result='mixed_available_side_gap')
        self.assertFails('mixed')

    def test_source_verification_problem_or_partial_gap(self):
        self.second['cases'][0]['problems'] = ['x']
        self.assertFails('source verification')
        self.second, = [summaries()[1]]
        self.second['cases'][1]['evidence_outcome'] = 'partial_gap'
        self.assertFails('outcome')

    def test_interruption_record_must_match_frozen_order(self):
        record_ = dict(INTERRUPTION, completed=DONE[::-1])
        self.assertTrue(rec.check_interrupted(self.first, self.a, record_, FROZEN))

    def test_records_ledger_must_be_prefix(self):
        self.a['ledger'].pop()
        self.assertFails('records.jsonl')


class RecoveryEndToEnd(unittest.TestCase):
    """`recover` with the existing verifier stubbed: output is exclusive-create-safe and labelled, never 'passed' on failure."""
    def test_recover_assembles_labelled_summary_and_never_mutates_inputs(self):
        first, second = summaries()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'first').mkdir(), (tmp / 'second').mkdir()
            (tmp / 'first/run-status.json').write_text(json.dumps(dict(status='started')))
            (tmp / 'interruption.json').write_text(json.dumps(INTERRUPTION))
            suite = tmp / 'suite.json'
            suite.write_text('{}')
            original = rec.lib.load_suite
            rec.lib.load_suite = lambda path, pin: (dict(case_ids=FROZEN, case_count=len(FROZEN), suite_id='double', corpus_sha256='c'), 'digest')
            try:
                calls = []
                def stub(directory, *args):
                    calls.append((Path(directory).name, args[-2:]))
                    return first if Path(directory).name == 'first' else second
                result = rec.recover(tmp / 'first', tmp / 'second', tmp / 'interruption.json', suite, 'digest', suite, suite, suite, ROOT, verify=stub)
            finally:
                rec.lib.load_suite = original
            self.assertEqual(calls, [('first', (ROOT, True)), ('second', (ROOT, True))])  # strict code on both; subset only via the manifest
            self.assertFalse(result['passed'], 'doubles have empty side files, so the union/hash checks must fail closed')
            self.assertIn('NOT one uninterrupted', result['kind'])
            self.assertEqual(len(result['cases']), 6)
            self.assertEqual((tmp / 'first/run-status.json').read_text(), json.dumps(dict(status='started')))
            self.assertIn('not_established', result)


if __name__ == '__main__':
    unittest.main()
