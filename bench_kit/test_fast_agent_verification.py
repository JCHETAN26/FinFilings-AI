"""Fast-mode contracts of the independent verifier and the paired-latency tooling.

REAL: the frozen suite, the expanded corpus and the saved historical LEGACY traces (live model + retrieval, from a past run).
TEST FIXTURE: the "fast" traces below are derived from one saved legacy trace (its real passages, quotes and citations) by
relabeling it as a fast run, only to exercise the verifier's rules and mutation rejection. They are NOT model output, were not
produced by the fast path, and nothing here is a latency, quality or benchmark result.
"""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bench_kit'))
sys.path.insert(0, str(ROOT / 'py'))
import agent_suite as lib  # noqa: E402
import eval_agent_latency as latency  # noqa: E402
import verify_broad_agent as verifier  # noqa: E402
from test_broad_agent_suite import CORPUS, HISTORY, ORIGINAL, REPORT, SUITE, require_artifacts  # noqa: E402


def to_fast_fixture(result):
    """Relabel a saved legacy trace as a fast host_quotes trace (TEST FIXTURE ONLY)."""
    fast = copy.deepcopy(result)
    fast['execution_mode'], fast['synthesis_route'] = 'fast', 'host_quotes'
    for hop in fast['hops']:
        hop['quote_selections'], hop['quote_flags'] = [], []
        if hop['accepted']:
            ids = {e['id'] for e in hop['evidence']}
            seen = []
            for citation in fast['citations']:
                pair = dict(chunk_id=citation['chunk_id'], quote=citation['quote'])
                if citation['chunk_id'] in ids and pair not in seen:
                    seen.append(pair)
            hop['quote_selections'] = seen[:4]
    plan = [c for c in fast['model_calls'] if c['schema'] == 'Plan']
    critics = [dict(c, schema='CritiqueWithQuotes') for c in fast['model_calls'] if c['schema'] == 'Critique']
    fast['model_calls'] = plan + critics
    return fast


class FastTraceVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_artifacts()
        if not (HISTORY / 'agent-provenance.json').exists():
            raise unittest.SkipTest('requires saved expanded-corpus traces')
        cls.suite, _ = lib.load_suite(SUITE, SUITE.with_suffix('.sha256').read_text().split()[0])
        sources = lib.build_trusted_sources(ORIGINAL, REPORT)
        cls.by_scope = {(s['ticker'], s['filing_year']): s for s in sources}
        cls.results = {i: json.loads((HISTORY / 'evaluation' / f'{i}.json').read_text()) for i in lib.REGRESSION_IDS}
        cls.markdown = {i: (HISTORY / 'evaluation' / f'{i}.md').read_text() for i in lib.REGRESSION_IDS}
        needed = {e['id'] for r in cls.results.values() for h in r['hops'] for e in h['evidence']}
        _, cls.originals = lib.scan_corpus(CORPUS, {s['accession'] for s in sources}, needed)
        cls.cases = {c['id']: c for c in cls.suite['cases']}
        for cid in lib.REGRESSION_IDS:  # a saved case where every subquery was accepted and cited
            result = cls.results[cid]
            finals = {}
            for hop in result['hops']:
                finals[hop['subquery']] = hop
            cited = {c['chunk_id'] for c in result['citations']}
            if finals and all(h['accepted'] and cited & {e['id'] for e in h['evidence']} for h in finals.values()) and len(finals) == len(result['plan']['subqueries']):
                cls.cid = cid
                return
        raise unittest.SkipTest('no fully-cited saved legacy case to derive the fixture from')

    def audit(self, result, markdown=None):
        checks = verifier.Checks()
        metrics = verifier.audit_case(self.cases[self.cid], result, self.markdown[self.cid] if markdown is None else markdown, self.by_scope, self.originals, checks)
        return metrics, checks.problems

    def has(self, problems, text):
        self.assertTrue(any(text in p for p in problems), f'{text!r} not in {problems}')

    def test_saved_legacy_trace_keeps_its_historic_meaning_and_has_no_fast_keys(self):
        result = self.results[self.cid]
        self.assertNotIn('execution_mode', result)
        metrics, problems = self.audit(result)
        self.assertEqual((problems, metrics['agent_mode'], metrics['synthesis_route']), ([], 'legacy', 'legacy'))

    def test_legacy_trace_with_fast_artifacts_is_rejected(self):
        for mutate, expected in ((lambda r: r['hops'][0].update(quote_selections=[dict(chunk_id=r['citations'][0]['chunk_id'], quote=r['citations'][0]['quote'])]), 'legacy hop'),
                                 (lambda r: r.update(synthesis_route='host_quotes'), 'legacy synthesis route')):
            result = copy.deepcopy(self.results[self.cid])
            mutate(result)
            self.has(self.audit(result)[1], expected)

    def test_valid_fast_trace_verifies_independently(self):
        fast = to_fast_fixture(self.results[self.cid])
        metrics, problems = self.audit(fast)
        self.assertEqual(problems, [])
        self.assertEqual((metrics['agent_mode'], metrics['synthesis_route'], metrics['model_calls']), ('fast', 'host_quotes', 1 + len(fast['hops'])))
        self.assertEqual(metrics['model_calls'], self.audit(self.results[self.cid])[0]['model_calls'] - 1)  # exactly the legacy synthesis call is gone

    def mutate(self, fn, expected):
        fast = to_fast_fixture(self.results[self.cid])
        fn(fast)
        self.has(self.audit(fast)[1], expected)

    def test_altered_or_fabricated_quote_selection_is_rejected(self):
        self.mutate(lambda r: r['hops'][0]['quote_selections'][0].update(quote='A fabricated quotation that does not occur anywhere in the saved SEC passage text.'), 'verbatim excerpt')

    def test_selection_bound_to_another_hops_evidence_is_rejected(self):
        def wrong(result):
            first, other = result['hops'][0], result['hops'][-1]
            foreign = next(e['id'] for e in other['evidence'] if e['id'] not in {x['id'] for x in first['evidence']})
            first['quote_selections'][0]['chunk_id'] = foreign
        self.mutate(wrong, 'verbatim excerpt')

    def test_below_threshold_hop_may_not_carry_or_publish_quotes(self):
        def leak(result):
            hop = result['hops'][0]
            hop.update(score=3, accepted=False)
        self.mutate(leak, 'non-accepted hop')

    def test_quote_flags_are_bounded_and_never_carry_quote_text(self):
        for flag in ('Critic quote (subquery 0): Dropped claim with unknown evidence handle ' + 'x' * 300,
                     'Critic quote (subquery 0): the critic wrote: ' + self.results[self.cid]['citations'][0]['quote'],
                     'Critic quote (subquery 9): quote length outside 40..2000 characters'):
            def add(result, flag=flag):
                result['hops'][0]['quote_flags'] = [flag]
                result['flags'] = result['flags'] + [flag]
            self.mutate(add, 'quote flag')
        def unpublished(result):
            result['hops'][0]['quote_flags'] = ['Critic quote (subquery 0): quote length outside 40..2000 characters']
        self.mutate(unpublished, 'missing from the published flags')

    def test_invalid_mode_route_and_ledger_are_rejected(self):
        self.mutate(lambda r: r.update(execution_mode='turbo'), 'invalid execution mode')
        self.mutate(lambda r: r.update(synthesis_route='fallback'), 'route')
        self.mutate(lambda r: r['model_calls'].append(dict(r['model_calls'][0], schema='HandleDraft')), 'ledger')  # invented synthesis call
        self.mutate(lambda r: r['model_calls'].__setitem__(1, dict(r['model_calls'][1], schema='Critique')), 'ledger')  # legacy critic in a fast trace
        self.mutate(lambda r: r['model_calls'].append(dict(r['model_calls'][0], schema='Draft')), 'unknown model-call schemas')
        self.mutate(lambda r: r.update(synthesis_route='none'), 'route')

    def test_fallback_requires_exactly_one_draft_call_and_a_real_gap(self):
        def gap(result, with_call=1):
            hop = [h for h in result['hops'] if h['accepted']][0]
            hop['quote_selections'] = []  # this goal got no valid critic quote; its published citations are the fallback's
            result['synthesis_route'] = 'fallback'
            result['model_calls'] += [dict(result['model_calls'][0], schema='HandleDraft')] * with_call
        fast = to_fast_fixture(self.results[self.cid])
        gap(fast)
        metrics, problems = self.audit(fast)
        self.assertEqual((problems, metrics['synthesis_route']), ([], 'fallback'))
        for calls in (0, 2):
            self.mutate(lambda r, calls=calls: gap(r, calls), 'ledger')
        self.mutate(lambda r: r.update(synthesis_route='fallback'), 'route')  # fallback claimed without any gap

    def test_a_gap_that_is_not_published_must_be_flagged(self):
        def gap(result):
            hop = [h for h in result['hops'] if h['accepted']][0]
            gone = {e['id'] for e in hop['evidence']}
            result['citations'] = [c for c in result['citations'] if c['chunk_id'] not in gone]
            hop['quote_selections'] = []
            result['synthesis_route'] = 'fallback'
            result['model_calls'].append(dict(result['model_calls'][0], schema='HandleDraft'))
        self.mutate(gap, 'unquoted-evidence flag inconsistent')

    def test_an_accepted_goal_cannot_lose_its_verified_quotes_silently(self):
        def drop(result):
            hop = [h for h in result['hops'] if h['accepted']][0]
            lost = {(q['chunk_id'], q['quote']) for q in hop['quote_selections']}
            result['citations'] = [c for c in result['citations'] if (c['chunk_id'], c['quote']) not in lost]
        self.mutate(drop, 'published quotes differ')

    def paired_directory(self, fast_mode_for_fast_run='fast'):
        """TEST FIXTURE: a complete paired run directory (one case, one repeat) assembled from the saved legacy trace and its relabeled fast twin,
        plus the supervisor-style companions (artifact hashes over tiny fake trees/code under a temporary root, status, 4x2 health, records ledger)."""
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        out, root = base / 'latency', base / 'repo'
        (out / 'runs').mkdir(parents=True)
        shutil.copy(HISTORY / 'agent-provenance.json', base / 'agent-provenance.json')
        digest = SUITE.with_suffix('.sha256').read_text().split()[0]
        shutil.copy(SUITE, out / 'suite.json')
        runs = latency.schedule([self.cid], 1)
        trees, code = {}, {}
        for tree in ('vector_index', 'reranker_model', 'sparse_index'):
            path = root / tree / 'file.bin'
            path.parent.mkdir(parents=True)
            path.write_text(tree)
            trees[tree] = {f'{tree}/file.bin': lib.sha256_file(path)}
        for relative in lib.REQUIRED_CODE:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'# fixture {relative}\n')
            code[relative] = lib.sha256_file(path)
        lineage, _ = lib.check_report_lineage(REPORT, ROOT)
        (base / 'artifact-hashes.json').write_text(json.dumps(dict(suite_sha256=digest, corpus_sha256=self.suite['corpus_sha256'], ingest_report_sha256=lib.sha256_file(REPORT),
            original_provenance_sha256=lib.sha256_file(ORIGINAL), previous_ingest_report_sha256=lineage.get('previous_report_sha256'), code=code, **trees)))
        (out / 'run-manifest.json').write_text(json.dumps(dict(suite_sha256=digest, corpus_sha256=self.suite['corpus_sha256'], cases=[self.cid], repeats=1, schedule=runs, code=code,
            primary_supported_cases=[c for c in latency.PRIMARY_CASES if c == self.cid], limits=dict(audits_active=1, model_concurrency=2, agent_concurrency=3))))
        (base / 'run-status.json').write_text(json.dumps(dict(status='completed', mode='paired-latency', owned_processes_stopped=True)))
        healthy = dict(sidecar=dict(status='ready', corpus_sha256=self.suite['corpus_sha256'], inference_workers=4, threads=2, admission=dict(rejected=0)),
                       bridge=dict(status='ready', corpus_sha256=self.suite['corpus_sha256']))
        for name in ('health-before.json', 'health-after.json'):
            (base / name).write_text(json.dumps(healthy))
        rows = []
        for run in runs:
            result = self.results[self.cid] if run['mode'] == 'legacy' else to_fast_fixture(self.results[self.cid])
            if run['mode'] == 'fast':
                result['execution_mode'] = fast_mode_for_fast_run
            (out / 'runs' / f"{run['run_id']}.json").write_text(json.dumps(result))
            (out / 'runs' / f"{run['run_id']}.md").write_text(self.markdown[self.cid])
            rows.append(dict(run, status='pass', wall_seconds=result['elapsed_seconds'] + 0.4, agent_seconds=result['elapsed_seconds'], agent_mode=result['execution_mode'] if 'execution_mode' in result else 'legacy',
                             synthesis_route=result.get('synthesis_route', 'legacy'), model_calls=len(result['model_calls'])))
        (out / 'records.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        self.root = root
        return out, digest

    def verify_paired(self, out, digest, strict=True):
        return latency.verify(out, SUITE, digest, CORPUS, ORIGINAL, REPORT, self.root, strict)

    def edit_json(self, path, fn):
        value = json.loads(Path(path).read_text())
        fn(value)
        Path(path).write_text(json.dumps(value))

    def problems(self, out, digest, **kw):
        summary = self.verify_paired(out, digest, **kw)
        self.assertFalse(summary['passed'])
        return summary['global_problems']

    def test_companion_integrity_failures_are_never_masked_by_passing_records(self):
        cases = [
            ('records agent_seconds altered', lambda o, b: (o / 'records.jsonl').write_text(''.join(json.dumps(dict(r, agent_seconds=r['agent_seconds'] + 1)) + '\n' for r in map(json.loads, (o / 'records.jsonl').read_text().splitlines()))), 'agent_seconds differs'),
            ('wall below agent time', lambda o, b: (o / 'records.jsonl').write_text(''.join(json.dumps(dict(r, wall_seconds=r['agent_seconds'] - 1)) + '\n' for r in map(json.loads, (o / 'records.jsonl').read_text().splitlines()))), 'inconsistent with agent elapsed'),
            ('records row missing', lambda o, b: (o / 'records.jsonl').write_text((o / 'records.jsonl').read_text().splitlines()[0] + '\n'), 'differ from the schedule'),
            ('records mode differs', lambda o, b: (o / 'records.jsonl').write_text(''.join(json.dumps(dict(r, mode='fast')) + '\n' for r in map(json.loads, (o / 'records.jsonl').read_text().splitlines()))), 'differs from the schedule'),
            ('health not 4x2', lambda o, b: self.edit_json(b / 'health-before.json', lambda v: v['sidecar'].update(inference_workers=2)), '4x2 profile'),
            ('health wrong corpus', lambda o, b: self.edit_json(b / 'health-after.json', lambda v: v['bridge'].update(corpus_sha256='0' * 64)), 'corpus fingerprint'),
            ('health missing', lambda o, b: (b / 'health-after.json').unlink(), 'health-after.json missing'),
            ('services not stopped', lambda o, b: self.edit_json(b / 'run-status.json', lambda v: v.update(owned_processes_stopped=False)), 'not confirmed stopped'),
            ('artifact hashes missing', lambda o, b: (b / 'artifact-hashes.json').unlink(), 'artifact-hashes.json missing'),
            ('immutable index changed', lambda o, b: (self.root / 'sparse_index' / 'file.bin').write_text('tampered'), 'mismatched'),
            ('code drift under strict check', lambda o, b: (self.root / 'py/agent/state_machine.py').write_text('# edited after the run\n'), 'strict code check'),
            ('manifest code differs from supervisor hashes', lambda o, b: self.edit_json(o / 'run-manifest.json', lambda v: v['code'].update({'go.mod': '0' * 64})), 'code hashes differ'),
        ]
        for label, mutate, expected in cases:
            with self.subTest(label):
                out, digest = self.paired_directory()
                mutate(out, out.parent)
                self.has(self.problems(out, digest), expected)

    def test_code_drift_is_only_reported_when_strictness_is_disabled(self):
        out, digest = self.paired_directory()
        (self.root / 'py/agent/state_machine.py').write_text('# edited after the run\n')
        summary = self.verify_paired(out, digest, strict=False)
        self.assertTrue(summary['passed'], summary['global_problems'])
        self.assertEqual(summary['environment']['artifacts']['code']['changed'], ['py/agent/state_machine.py'])

    def test_paired_verifier_end_to_end_on_a_fixture_directory(self):
        out, digest = self.paired_directory()
        summary = self.verify_paired(out, digest)
        self.assertEqual((summary['global_problems'], summary['verified'], summary['scheduled_runs']), ([], 2, 2))
        self.assertTrue(summary['passed'])
        self.assertEqual(summary['comparison']['per_mode']['fast']['model_calls'] + 1, summary['comparison']['per_mode']['legacy']['model_calls'])
        self.assertEqual(len(summary['comparison']['pairs']), 1)

    def test_paired_verifier_rejects_a_run_executed_in_the_wrong_mode_and_extra_files(self):
        out, digest = self.paired_directory(fast_mode_for_fast_run='legacy')
        summary = self.verify_paired(out, digest)
        self.assertFalse(summary['passed'])
        self.assertTrue(summary['verification_failed'])
        out, digest = self.paired_directory()
        (out / 'runs' / '99-fast-audit01-r1.json').write_text('{}')
        self.assertTrue(any('unexpected file' in p for p in self.verify_paired(out, digest)['global_problems']))

    def test_manifest_mode_must_match_each_result(self):
        fast = to_fast_fixture(self.results[self.cid])
        self.assertEqual(self.audit(fast)[0]['agent_mode'], 'fast')  # per-case mode feeds the manifest cross-check in verify()


class ExecuteRecords(unittest.TestCase):
    """TEST DOUBLES (synthetic fixture corpus, scripted model/backend): the runner's per-run record and raw-file contract only."""

    def test_execute_preserves_raw_results_modes_and_failures(self):
        import asyncio
        import tempfile
        import audit_web_testkit as kit
        import eval_agent
        from agent.provenance import ProvenanceRegistry
        from agent.state_machine import Agent
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            corpus, manifest = kit.write_fixture_corpus(tmp)
            registry = ProvenanceRegistry(corpus, manifest)
            self.addCleanup(registry.close)
            case = dict(id='audit01', prompt=kit.PROMPT, section='', require_citations=False,
                        expected_scopes=[dict(ticker='TST', year=2024, accession=kit.ACC, available=True)])
            (tmp / 'runs').mkdir()
            for mode, schema in (('legacy', 'Critique'), ('fast', 'CritiqueWithQuotes')):
                agent = Agent(kit.ScriptedModel(), kit.ScriptedBackend(), registry, mode=mode)
                info = dict(ordinal=1, pair=0, case='audit01', repeat=1, mode=mode, position_in_pair=0, run_id=f'01-{mode}-audit01-r1')
                record = asyncio.run(latency.execute(agent, registry, case, info, tmp / 'runs', eval_agent))
                self.assertEqual((record['status'], record['agent_mode'], record['evidence_outcome']), ('pass', mode, 'supported'), record)
                saved = json.loads((tmp / 'runs' / f'01-{mode}-audit01-r1.json').read_text())
                self.assertEqual([c['schema'] for c in saved['model_calls']][1:3], [schema, schema])
                self.assertEqual(saved['execution_mode'], mode)
                self.assertGreater(record['wall_seconds'], 0)

            class Boom:
                async def run(self, prompt):
                    raise RuntimeError('Claude generation failed, exit=1')
            info = dict(ordinal=2, pair=0, case='audit01', repeat=1, mode='fast', position_in_pair=1, run_id='02-fast-audit01-r1')
            failed = asyncio.run(latency.execute(Boom(), registry, case, info, tmp / 'runs', eval_agent))
            self.assertEqual(failed['status'], 'fail')
            self.assertIn(failed['category'], lib.FAILURE_CATEGORIES)
            self.assertTrue((tmp / 'runs' / '02-fast-audit01-r1.failure.json').is_file())
            self.assertFalse((tmp / 'runs' / '02-fast-audit01-r1.json').exists())


class PairedSchedule(unittest.TestCase):
    def test_alternates_ab_ba_by_pair_with_a_hard_cap(self):
        runs = latency.schedule(latency.DEFAULT_CASES, 2)
        self.assertEqual(len(runs), 24)
        self.assertEqual([r['mode'] for r in runs[:8]], ['legacy', 'fast', 'fast', 'legacy', 'legacy', 'fast', 'fast', 'legacy'])
        self.assertEqual(len({r['run_id'] for r in runs}), 24)
        for pair in range(12):
            self.assertEqual(sorted(r['mode'] for r in runs if r['pair'] == pair), ['fast', 'legacy'])
        with self.assertRaises(ValueError):
            latency.schedule(latency.DEFAULT_CASES, 3)
        with self.assertRaises(ValueError):
            latency.schedule(['audit04', 'audit04'], 1)

    def test_default_cases_are_in_the_frozen_v2_suite_and_unchanged(self):
        path = ROOT / 'bench_kit/suites/broad-validation-v2.json'
        if not path.exists():
            self.skipTest('requires the frozen v2 suite')
        suite, _ = lib.load_suite(path, path.with_suffix('.sha256').read_text().split()[0])
        ids = {c['id'] for c in suite['cases']}
        self.assertTrue(set(latency.DEFAULT_CASES) <= ids)

    def row(self, case, repeat, mode, seconds, calls, first, outcome='supported', cited=2):
        return dict(seconds=seconds, model_calls=calls, evidence_outcome=outcome, available_subqueries=2, cited_available_subqueries=cited,
                    synthesis_route='host_quotes' if mode == 'fast' else 'legacy', unavailable_withheld=True, retries=0, retrieval_calls=2,
                    position_in_pair=0 if first else 1)

    def primary_rows(self, legacy, fast, outcomes=None, cases=('c1', 'c2'), repeats=2):
        rows, n = {}, 0
        for case in cases:
            for repeat in range(1, repeats + 1):
                for mode, values in (('legacy', legacy), ('fast', fast)):
                    outcome = (outcomes or {}).get((case, repeat, mode), 'supported')
                    rows[(case, repeat, mode)] = self.row(case, repeat, mode, values[n], 7 if mode == 'legacy' else 6, mode == 'legacy', outcome)
                n += 1
        return rows

    def test_gate_is_the_reduction_of_median_seconds_not_the_median_of_pair_fractions(self):
        legacy, fast = [10.0, 20.0, 30.0, 40.0], [5.0, 19.0, 29.0, 10.0]  # pair fractions .5 .05 .033 .75 -> median .275; medians 25 vs 14.5 -> .42
        report = latency.compare(self.primary_rows(legacy, fast), ('c1', 'c2'), 2)
        primary = report['primary_supported']
        self.assertEqual(primary['median_seconds'], {'legacy': 25.0, 'fast': 14.5})
        self.assertAlmostEqual(primary['reduction'], 1 - 14.5 / 25.0)
        self.assertAlmostEqual(report['descriptive']['median_paired_improvement_supported_pairs'], 0.275)
        self.assertNotAlmostEqual(primary['reduction'], report['descriptive']['median_paired_improvement_supported_pairs'])
        self.assertTrue(report['preregistered']['latency_target_met'])

    def test_gate_can_fail_while_the_paired_median_would_pass(self):
        legacy, fast = [10.0, 20.0, 30.0, 40.0], [5.0, 19.0, 29.5, 39.0]  # fractions .5 .05 .017 .025 -> median .0375; try the reverse below
        low = latency.compare(self.primary_rows(legacy, fast), ('c1', 'c2'), 2)
        self.assertFalse(low['preregistered']['latency_target_met'])
        legacy, fast = [10.0, 10.0, 100.0, 100.0], [9.0, 9.0, 80.0, 80.0]  # fractions .1 .1 .2 .2 -> median .15; medians 55 vs 44.5 -> .19
        report = latency.compare(self.primary_rows(legacy, fast), ('c1', 'c2'), 2)
        self.assertAlmostEqual(report['descriptive']['median_paired_improvement_supported_pairs'], 0.15)
        self.assertAlmostEqual(report['primary_supported']['reduction'], 1 - 44.5 / 55.0)
        legacy, fast = [10.0, 10.0, 100.0, 100.0], [4.0, 4.0, 97.0, 97.0]  # fractions .6 .6 .03 .03 -> median .315 passes; medians 55 vs 50.5 -> .082 fails
        report = latency.compare(self.primary_rows(legacy, fast), ('c1', 'c2'), 2)
        self.assertGreaterEqual(report['descriptive']['median_paired_improvement_supported_pairs'], 0.15)
        self.assertLess(report['primary_supported']['reduction'], 0.15)
        self.assertFalse(report['preregistered']['latency_target_met'])

    def test_gate_fails_closed_on_missing_or_unsupported_primary_runs(self):
        legacy, fast = [10.0, 10.0, 10.0, 10.0], [1.0, 1.0, 1.0, 1.0]
        good = self.primary_rows(legacy, fast)
        self.assertTrue(latency.compare(good, ('c1', 'c2'), 2)['preregistered']['latency_target_met'])
        missing = dict(good)
        del missing[('c2', 2, 'fast')]
        report = latency.compare(missing, ('c1', 'c2'), 2)
        self.assertFalse(report['preregistered']['latency_target_met'])
        self.assertTrue(any('missing' in p for p in report['primary_supported']['problems']))
        for mode in ('legacy', 'fast'):  # a poor-coverage primary run is reported, never silently dropped to rescue the median
            gap = self.primary_rows(legacy, fast, {('c1', 1, mode): 'partial_gap'})
            report = latency.compare(gap, ('c1', 'c2'), 2)
            self.assertFalse(report['preregistered']['latency_target_met'])
            self.assertTrue(any('partial_gap' in p for p in report['primary_supported']['problems']))
        self.assertFalse(latency.compare(good, (), 2)['preregistered']['latency_target_met'])

    def test_non_primary_cases_are_descriptive_only_and_other_rules_still_reported(self):
        rows = self.primary_rows([10.0] * 4, [1.0] * 4)
        rows.update({('n', 1, 'legacy'): self.row('n', 1, 'legacy', 20.0, 5, True, 'abstained'), ('n', 1, 'fast'): self.row('n', 1, 'fast', 20.0, 5, False, 'abstained')})
        report = latency.compare(rows, ('c1', 'c2'), 2)
        self.assertTrue(report['preregistered']['latency_target_met'])
        self.assertEqual(report['descriptive']['supported_pairs'], 4)
        rows[('c1', 1, 'fast')] = dict(rows[('c1', 1, 'fast')], cited_available_subqueries=1)
        self.assertFalse(latency.compare(rows, ('c1', 'c2'), 2)['preregistered']['coverage_not_reduced_in_every_pair'])


if __name__ == '__main__':
    unittest.main()
