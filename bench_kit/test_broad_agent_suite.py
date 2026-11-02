"""Contract tests for the frozen broad suite, the evaluator's per-scope checks and the offline verifier.

REAL artifacts: the frozen suite, the 49,452-passage corpus, the ingestion report and the saved historical audits.
TEST FIXTURES: the synthetic *run directories* below are assembled from real corpus passages only to exercise the
verifier's contracts (omitted cases, tampering, scope leaks, retention). They are not model output, and nothing
here is a reported metric.
"""
import asyncio
import collections
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
import verify_broad_agent as verifier  # noqa: E402

SUITE = ROOT / 'bench_kit/suites/broad-validation-v1.json'
CORPUS = ROOT / 'data/sec-scale-v2-evaluation-20261031/chunks.jsonl'
ORIGINAL = ROOT / 'data/sec/provenance.json'
REPORT = ROOT / 'indexes/sec-scale-v2-20261031/report.json'
HISTORY = ROOT / 'bench_kit/results/phase3-expanded-20261031'
REQUIRED = [SUITE, CORPUS, ORIGINAL, REPORT, SUITE.with_suffix('.sha256')]
CANNED = 'No verified SEC evidence met the relevance threshold. No financial claims were generated.'


def require_artifacts():
    if not all(p.exists() for p in REQUIRED):
        raise unittest.SkipTest('requires the frozen suite and acquired expanded corpus')


class SuiteContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_artifacts()
        cls.pin = SUITE.with_suffix('.sha256').read_text().split()[0]
        cls.suite, cls.digest = lib.load_suite(SUITE, cls.pin)
        cls.sources = lib.build_trusted_sources(ORIGINAL, REPORT)

    def test_exact_membership_counts_and_labels(self):
        cases = self.suite['cases']
        self.assertEqual(len(cases), 30)
        self.assertEqual(self.suite['case_ids'], [c['id'] for c in cases])
        self.assertEqual(collections.Counter(c['kind'] for c in cases), {'regression': 10, 'positive': 16, 'negative': 2, 'mixed': 2})
        self.assertIn('not independent human gold', self.suite['label'])
        self.assertEqual(len(self.sources), 152)

    def test_pin_detects_a_modified_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            copy_path = Path(directory) / 'suite.json'
            copy_path.write_text(SUITE.read_text().replace('broad-validation-v1', 'broad-validation-v2', 1))
            with self.assertRaises(lib.SuiteError):
                lib.load_suite(copy_path, self.pin)

    def test_regression_prompts_are_verbatim_historical_prompts(self):
        saved = {i: json.loads((HISTORY / 'evaluation' / f'{i}.json').read_text())['prompt'] for i in lib.REGRESSION_IDS}
        import eval_agent
        self.assertEqual({c['id']: c['prompt'] for c in eval_agent.cases()}, saved)
        self.assertEqual({c['id']: c['prompt'] for c in self.suite['cases'][:10]}, saved)

    def test_required_scenarios_and_company_diversity(self):
        coverage = lib.suite_coverage(self.suite)
        self.assertGreaterEqual(len(coverage['new_companies_beyond_baseline']), 12)
        self.assertEqual(sorted(coverage['exact_sections']), ['Item 1A', 'Item 7', 'Item 8'])
        self.assertTrue({'broad27', 'broad29'} <= set(coverage['absent_filing_year']))
        self.assertIn('broad28', coverage['absent_exact_section'])
        self.assertEqual(coverage['mixed_availability'], ['broad29', 'broad30'])
        self.assertGreaterEqual(len(coverage['filing_years']), 4)
        positives = [c for c in self.suite['cases'] if c['kind'] == 'positive']
        self.assertTrue(all(len({s['ticker'] for s in c['scopes']}) > 1 for c in positives[:2]))

    def test_expected_availability_matches_the_real_corpus(self):
        sections, _ = lib.scan_corpus(CORPUS, {s['accession'] for s in self.sources})
        lib.check_expected_scopes(self.suite, self.sources, sections)  # raises on any drift

    def test_unavailability_is_audited_per_scope_not_any_source(self):
        case = next(c for c in self.suite['cases'] if c['id'] == 'broad29')
        flags = [e['available'] for e in case['expected_scopes']]
        self.assertEqual(flags, [True, False])
        # The historical classifier `not any(...)` over all scopes would call this mixed case supported.
        self.assertFalse(not any(flags))
        self.assertEqual({e['reason'] for e in case['expected_scopes']}, {'available', 'filing_year_absent'})
        mixed = next(c for c in self.suite['cases'] if c['id'] == 'broad30')
        self.assertEqual([e['reason'] for e in mixed['expected_scopes']], ['available', 'exact_section_absent'])
        self.assertTrue(mixed['expected_scopes'][1]['source_exists'])

    def test_anchors_are_real_passages_and_hidden_from_the_agent(self):
        anchors = [(c, a) for c in self.suite['cases'] for a in c.get('reviewer_evidence', {}).get('anchors', [])]
        _, found = lib.scan_corpus(CORPUS, set(), {a['id'] for _, a in anchors})
        self.assertEqual(len(found), len({a['id'] for _, a in anchors}))
        for case, anchor in anchors:
            chunk = found[anchor['id']]
            self.assertIn(anchor['excerpt'], lib.normalize(chunk['Text']))
            self.assertEqual(lib.sha256_text(chunk['Text']), anchor['passage_text_sha256'])
            self.assertEqual(lib.agent_view(case), {'id': case['id'], 'prompt': case['prompt']})
            self.assertNotIn(anchor['id'], case['prompt'])
            self.assertNotIn(anchor['excerpt'][:40], case['prompt'])

    def test_validator_rejects_broken_suites(self):
        def mutated(change):
            suite = copy.deepcopy(self.suite)
            change(suite)
            with self.assertRaises(lib.SuiteError):
                lib.validate_suite(suite)
        mutated(lambda s: (s['cases'].pop(), s.update(case_count=29, case_ids=s['case_ids'][:-1])))
        mutated(lambda s: s['cases'].__setitem__(5, dict(s['cases'][5], id='audit01')))
        mutated(lambda s: s['cases'][12].__setitem__('kind', 'mixed'))
        mutated(lambda s: s['cases'][12].__setitem__('prompt', s['cases'][12]['prompt'] + ' ' + s['cases'][12]['reviewer_evidence']['anchors'][0]['id']))
        mutated(lambda s: s['cases'][12]['expected_scopes'].pop())
        mutated(lambda s: s['cases'][28]['expected_scopes'][1].update(available=True))
        mutated(lambda s: s['cases'][10]['reviewer_evidence']['anchors'][0].update(id='000000000000000000#item7#passage-000001'))
        mutated(lambda s: s['cases'][0].__setitem__('reviewer_evidence', {'anchors': [{'x': 1}]}))


def load_passages(sources):
    """First four real passages per (accession, SectionItem) and per accession ('' = any section)."""
    wanted = {s['accession'] for s in sources}
    passages = collections.defaultdict(list)
    with CORPUS.open('rb') as stream:
        for line in stream:
            chunk = json.loads(line)
            accession = chunk['ID'].split('#')[0]
            if accession in wanted and len(passages[(accession, chunk['SectionItem'])]) < 4:
                passages[(accession, chunk['SectionItem'])].append(chunk)
                if len(passages[(accession, '')]) < 4:
                    passages[(accession, '')].append(chunk)
    return passages


class FixtureRun:
    """Assemble a run directory from real passages so verifier contracts can be exercised."""

    def __init__(self, suite, digest, sources, passages):
        self.suite, self.digest, self.sources, self.passages = suite, digest, sources, passages
        self.by_scope = {(s['ticker'], s['filing_year']): s for s in sources}

    def evidence(self, chunk, source):
        match = lib.PASSAGE_ID.fullmatch(chunk['ID'])
        accession, section, number = match.groups()
        suffix = f'{section}-p{number}' if section else f'p{number}'
        return dict(id=chunk['ID'], text=chunk['Text'], metadata={k: v for k, v in chunk.items() if k not in ('Text', 'Tokens')},
            accession=f'{accession[:10]}-{accession[10:12]}-{accession[12:]}', url=source['url'],
            tag=f"{source['ticker']}-{source['filing_year']}-{source['form'].replace('-', '')}-{suffix}")

    def result(self, case, retry_first=True, weak=()):
        scopes = case['expected_scopes']
        plan = []
        for scope in scopes:
            plan.append((scope, 'topic one'))
        if len(plan) == 1:
            plan.append((scopes[0], 'topic two'))
        subqueries, hops, citations, flags, calls = [], [], [], [], [dict(schema='Plan', models=['fixture-model'])]
        cited_lines, retried = [], False
        for position, (scope, topic) in enumerate(plan):
            filters = dict(ticker=scope['ticker'], year=scope['year'], section=case['section'], accession=scope['accession'])
            query = f"{scope['ticker']} {topic} disclosure"
            subqueries.append(dict(query=query, filters=filters))
            if not scope['available']:
                for attempt in range(3):
                    hops.append(dict(subquery=position, attempt=attempt, query=query if attempt == 0 else f'{query} rewrite {attempt}', filters=filters, score=1,
                        rationale='no evidence', evidence=[], accepted=False, retained_evidence_ids=[], elapsed_seconds=.1))
                    calls.append(dict(schema='Critique', models=['fixture-model']))
                flags.append(f'Subquery {position} has no evidence meeting threshold after 2 retries.')
                continue
            source = self.by_scope[(scope['ticker'], scope['year'])]
            if position in weak:  # available scope, honest retrieval gap: evidence found but never accepted
                weak_evidence = [self.evidence(self.passages[(scope['accession'], case['section'])][0], source)]
                for attempt in range(3):
                    hops.append(dict(subquery=position, attempt=attempt, query=query if attempt == 0 else f'{query} rewrite {attempt}', filters=filters, score=2,
                        rationale='weak', evidence=weak_evidence, accepted=False, retained_evidence_ids=[], elapsed_seconds=.1))
                    calls.append(dict(schema='Critique', models=['fixture-model']))
                flags.append(f'Subquery {position} has no evidence meeting threshold after 2 retries.')
                continue
            pool = self.passages[(scope['accession'], case['section'])]
            first = [self.evidence(c, source) for c in pool[:3]]
            if retry_first and not retried and position == 0 and len(first) >= 3:
                retried = True
                hops.append(dict(subquery=position, attempt=0, query=query, filters=filters, score=3, rationale='partial', evidence=first[:2],
                    accepted=False, retained_evidence_ids=[], elapsed_seconds=.1))
                merged = [first[0]] + [e for e in first[1:] if e['id'] != first[0]['id']]
                hops.append(dict(subquery=position, attempt=1, query=query + ' targeted', filters=filters, score=4, rationale='good', evidence=merged,
                    accepted=True, retained_evidence_ids=[first[0]['id']], elapsed_seconds=.1))
                calls += [dict(schema='Critique', models=['fixture-model'])] * 2
                final = merged
            else:
                hops.append(dict(subquery=position, attempt=0, query=query, filters=filters, score=4, rationale='good', evidence=first,
                    accepted=True, retained_evidence_ids=[], elapsed_seconds=.1))
                calls.append(dict(schema='Critique', models=['fixture-model']))
                final = first
            text = ' '.join(final[0]['text'].split())
            used = {(c['chunk_id'], c['quote']) for c in citations}
            quote = next(q for q in (text[:120].strip(), text[60:180].strip(), text[-120:].strip(), text[:90].strip()) if len(q) >= 40 and (final[0]['id'], q) not in used)
            citations.append(dict(tag=final[0]['tag'], chunk_id=final[0]['id'], accession=final[0]['accession'], url=final[0]['url'], quote=quote))
        accepted_any = any(h['accepted'] for h in hops)
        if accepted_any:
            calls.append(dict(schema='Draft', models=['fixture-model']))
            lines = ['Verified filing excerpts (extractive audit evidence; no derived calculations):', '']
            for c in citations:
                lines.append(f"- {verifier.escape_markdown(c['quote'])} [{c['tag']}]({c['url']}) (chunk: `{c['chunk_id']}`; accession: `{c['accession']}`).")
            markdown = '\n'.join(lines)
        else:
            markdown = CANNED
            flags.append('No verified answer; evidence unavailable or below threshold.')
        if flags:
            markdown += '\n\nEvidence gaps:\n' + '\n'.join('- ' + verifier.escape_markdown(f) for f in flags)
        return dict(prompt=case['prompt'], states=['Plan', 'Retrieve_Evaluate', 'Synthesize'], plan=dict(subqueries=subqueries), hops=hops,
            markdown=markdown, citations=citations, flags=flags, model_calls=calls, elapsed_seconds=1.0)

    def write(self, directory):
        directory = Path(directory)
        evaluation = directory / 'evaluation'
        evaluation.mkdir(parents=True)
        suite_bytes = SUITE.read_bytes()
        for path in (directory / 'suite.json', evaluation / 'suite.json'):
            path.write_bytes(suite_bytes)
        (directory / 'suite.sha256').write_text(f'{self.digest}  suite.json\n')
        (evaluation / 'suite.sha256').write_text(f'{self.digest}  suite.json\n')
        (directory / 'agent-provenance.json').write_text(json.dumps(dict(corpus_sha256=self.suite['corpus_sha256'], sources=self.sources, source_manifests=[])))
        self.root = directory.parent / 'root'  # tiny stand-in tree: contracts only, not the real indexes/models
        recorded = {}
        for tree, names in (('vector_index', ['indexes/vec/index.faiss', 'indexes/vec/manifest.json']), ('reranker_model', ['models/rr/model.onnx']),
                            ('sparse_index', ['indexes/sparse/seg-0.ffseg', 'indexes/sparse/MANIFEST'])):
            recorded[tree] = {}
            for name in names:
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f'fixture bytes for {name}'.encode())
                recorded[tree][name] = lib.sha256_file(path)
        recorded['code'] = {}
        for name in lib.REQUIRED_CODE:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f'fixture code {name}'.encode())
            recorded['code'][name] = lib.sha256_file(path)
        (directory / 'artifact-hashes.json').write_text(json.dumps(dict(suite_sha256=self.digest, corpus_sha256=self.suite['corpus_sha256'],
            ingest_report_sha256=lib.sha256_file(REPORT), original_provenance_sha256=lib.sha256_file(ORIGINAL), **recorded)))
        (directory / 'run-status.json').write_text(json.dumps(dict(status='completed', owned_processes_stopped=True)))
        (evaluation / 'run-manifest.json').write_text(json.dumps(dict(suite_sha256=self.digest, selected=self.suite['case_ids'])))
        for case in self.suite['cases']:
            result = self.result(case)
            (evaluation / f"{case['id']}.json").write_text(json.dumps(result))
            (evaluation / f"{case['id']}.md").write_text(result['markdown'] + '\n')
        return directory

    @staticmethod
    def write_ledgers(directory, summary):
        """Evaluator-format report.json + records.jsonl built from independently recomputed metrics (fixture baseline)."""
        rows = [dict(id=r['id'], status='pass', kind=r['kind'], seconds=r['seconds'], **{f: r[f] for f in verifier.COMPARE_FIELDS})
                for r in summary['cases'] if r['verdict'] == 'verified']
        ok = [r for r in summary['cases'] if r['verdict'] == 'verified']
        report = dict(cases=rows, passed=len(ok), total=len(summary['expected_case_ids']), citations=sum(r['citations'] for r in ok),
            retrieval_calls=sum(r['retrieval_calls'] for r in ok), retries=sum(r['retries'] for r in ok),
            negative_cases=sum(r['negative'] for r in ok), fully_covered_positive_cases=sum(r['complete_coverage'] for r in ok),
            failures_by_category=summary['failures_by_category'], evidence_outcomes=summary['evidence_outcomes'],
            selected=summary['expected_case_ids'], median_seconds=1.0)
        (Path(directory) / 'evaluation' / 'report.json').write_text(json.dumps(report))
        (Path(directory) / 'evaluation' / 'records.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))


class VerifierContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_artifacts()
        cls.pin = SUITE.with_suffix('.sha256').read_text().split()[0]
        cls.suite, cls.digest = lib.load_suite(SUITE, cls.pin)
        cls.sources = lib.build_trusted_sources(ORIGINAL, REPORT)
        passages = cls.passages = load_passages(cls.sources)
        cls.fixture = FixtureRun(cls.suite, cls.digest, cls.sources, passages)
        base = tempfile.TemporaryDirectory()
        cls.addClassCleanup(base.cleanup)
        directory = cls.fixture.write(Path(base.name) / 'run')
        cls.baseline = verifier.verify(directory, SUITE, cls.pin, CORPUS, ORIGINAL, REPORT, root=cls.fixture.root)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = self.fixture.write(Path(self.temp.name) / 'run')
        self.root = self.fixture.root
        self.fixture.write_ledgers(self.run_dir, self.baseline)

    def refresh_ledgers(self):
        (self.run_dir / 'evaluation' / 'report.json').unlink()
        self.fixture.write_ledgers(self.run_dir, self.verify())

    def verify(self, **kwargs):
        kwargs.setdefault('root', self.root)
        return verifier.verify(self.run_dir, SUITE, self.pin, CORPUS, ORIGINAL, REPORT, **kwargs)

    def load(self, case_id):
        return json.loads((self.run_dir / 'evaluation' / f'{case_id}.json').read_text())

    def save(self, case_id, result):
        (self.run_dir / 'evaluation' / f'{case_id}.json').write_text(json.dumps(result))
        (self.run_dir / 'evaluation' / f'{case_id}.md').write_text(result['markdown'] + '\n')

    def problems(self, summary, case_id):
        return next(r for r in summary['cases'] if r['id'] == case_id)['problems']

    def assertDetected(self, case_id, fragment, **kwargs):
        summary = self.verify(**kwargs)
        self.assertFalse(summary['passed'])
        self.assertEqual(next(r for r in summary['cases'] if r['id'] == case_id)['verdict'], 'verification_failed')
        self.assertTrue(any(fragment in p for p in self.problems(summary, case_id)), self.problems(summary, case_id))
        return summary

    def test_valid_fixture_run_verifies_all_thirty_with_mixed_and_unavailable_behaviour(self):
        summary = self.verify()
        self.assertEqual(summary['global_problems'], [])
        self.assertEqual(summary['evaluator_summary_discrepancies'], [])
        self.assertEqual([p for r in summary['cases'] for p in r['problems']], [])
        self.assertTrue(summary['passed'])
        self.assertEqual((summary['verified'], summary['frozen_case_count']), (30, 30))
        self.assertEqual(summary['mixed_availability_cases_verified'], ['broad29', 'broad30'])
        outcomes = {r['id']: r['evidence_outcome'] for r in summary['cases']}
        self.assertEqual(outcomes['broad27'], 'expected_unavailable')
        self.assertEqual(outcomes['broad28'], 'expected_unavailable')
        self.assertEqual(outcomes['broad29'], 'supported')

    def test_omitted_case_is_detected_even_if_evaluator_claims_success(self):
        for suffix in ('json', 'md'):
            (self.run_dir / 'evaluation' / f'broad17.{suffix}').unlink()
        report = json.loads((self.run_dir / 'evaluation' / 'report.json').read_text())
        report['cases'] = [dict(id=c, status='pass') for c in self.suite['case_ids']]
        (self.run_dir / 'evaluation' / 'report.json').write_text(json.dumps(report))
        summary = self.verify()
        self.assertFalse(summary['passed'])
        self.assertEqual(summary['omitted'], ['broad17'])
        self.assertEqual(summary['verified'], 29)
        self.assertTrue(any('broad17' in d and 'independent verdict' in d for d in summary['evaluator_summary_discrepancies']))

    def test_half_written_case_and_unexpected_files_are_detected(self):
        (self.run_dir / 'evaluation' / 'broad20.md').unlink()
        (self.run_dir / 'evaluation' / 'broad31.json').write_text('{}')
        summary = self.verify()
        self.assertEqual(next(r for r in summary['cases'] if r['id'] == 'broad20')['state'], 'incomplete_files')
        self.assertTrue(any('broad31.json' in p for p in summary['global_problems']))
        self.assertFalse(summary['passed'])

    def test_partial_run_needs_an_explicit_allowance(self):
        manifest = self.run_dir / 'evaluation' / 'run-manifest.json'
        manifest.write_text(json.dumps(dict(suite_sha256=self.digest, selected=['audit01', 'audit02'])))
        self.assertFalse(self.verify()['passed'])
        subset = self.verify(allow_subset=True)
        self.assertEqual(subset['expected_case_ids'], ['audit01', 'audit02'])
        self.assertFalse(subset['full_suite'])
        self.assertEqual(subset['verified'], 2)

    def test_saved_suite_must_be_the_pinned_suite(self):
        (self.run_dir / 'suite.json').write_text(SUITE.read_text() + ' ')
        self.assertTrue(any('suite.json differs' in p for p in self.verify()['global_problems']))

    def test_enrollment_url_tampering_is_detected(self):
        path = self.run_dir / 'agent-provenance.json'
        enrolled = json.loads(path.read_text())
        enrolled['sources'][10]['url'] = enrolled['sources'][10]['url'].replace('www.sec.gov', 'example.invalid')
        path.write_text(json.dumps(enrolled))
        self.assertTrue(any('enrollment differs' in p for p in self.verify()['global_problems']))

    def test_tampered_citation_quote_tag_and_markdown_are_detected(self):
        result = self.load('broad14')
        result['citations'][0]['quote'] = 'Fabricated revenue increased by 999 percent during the entire filing period.'
        self.save('broad14', result)
        self.assertDetected('broad14', 'not a verbatim passage excerpt')
        result = self.load('broad14')
        result['citations'][0] = dict(self.fixture.result(self.suite['cases'][13])['citations'][0], tag='TGT-2026-10K-p999999')
        self.save('broad14', result)
        self.assertDetected('broad14', 'differ from the verified evidence')
        result = self.fixture.result(self.suite['cases'][13])
        result['markdown'] = result['markdown'].replace('(chunk:', '(chunk: forged-')
        self.save('broad14', result)
        self.assertDetected('broad14', 'inline citation line missing or altered')

    def test_tampered_evidence_bytes_are_detected(self):
        result = self.load('audit03')
        result['hops'][0]['evidence'][0]['text'] += ' invented sentence'
        self.save('audit03', result)
        self.assertDetected('audit03', 'text differs from original bytes')

    def test_scope_leakage_to_another_company_is_detected(self):
        result = self.load('broad11')
        other = next(s for s in self.sources if s['ticker'] == 'PFE')
        leak = self.fixture.evidence(self.passages[(other['accession'], 'Item 7')][0], other)
        result['hops'][0]['evidence'][0] = leak
        self.save('broad11', result)
        self.assertDetected('broad11', 'SCOPE LEAK')

    def test_exact_section_leakage_is_detected(self):
        result = self.load('broad11')
        source = next(s for s in self.sources if s['ticker'] == 'KO' and s['filing_year'] == 2025)
        result['hops'][0]['evidence'][0] = self.fixture.evidence(self.passages[(source['accession'], 'Item 1A')][0], source)
        self.save('broad11', result)
        self.assertDetected('broad11', 'SCOPE LEAK')

    def test_mixed_comparison_cannot_substitute_another_year_for_the_unavailable_scope(self):
        result = self.load('broad29')
        bac_position = next(i for i, q in enumerate(result['plan']['subqueries']) if q['filters']['ticker'] == 'BAC')
        substitute = next(s for s in self.sources if s['ticker'] == 'BAC' and s['filing_year'] == 2026)
        hop = next(h for h in result['hops'] if h['subquery'] == bac_position)
        hop['evidence'] = [self.fixture.evidence(self.passages[(substitute['accession'], '')][0], substitute)]
        hop['score'], hop['accepted'] = 4, True
        self.save('broad29', result)
        summary = self.assertDetected('broad29', 'unavailable scope produced evidence')
        self.assertTrue(any('no trusted filing' in p for p in self.problems(summary, 'broad29')))

    def test_mixed_comparison_requires_unavailable_scope_to_exhaust_retries_and_be_flagged(self):
        result = self.load('broad30')
        amzn = next(i for i, q in enumerate(result['plan']['subqueries']) if q['filters']['ticker'] == 'AMZN')
        result['hops'] = [h for h in result['hops'] if not (h['subquery'] == amzn and h['attempt'] == 2)]
        self.save('broad30', result)
        self.assertDetected('broad30', 'stopped before accepting, exhausting retries or a valid stalled-rewrite stop')
        result = self.load('broad30')
        result['flags'] = []
        self.save('broad30', result)
        self.assertDetected('broad30', 'evidence-gap flag inconsistent')

    def test_wrong_plan_scope_or_relaxed_section_is_detected(self):
        result = self.load('broad28')
        result['plan']['subqueries'][0]['filters']['section'] = ''
        self.save('broad28', result)
        self.assertDetected('broad28', 'plan changed exact section')
        result = self.load('broad12')
        result['plan']['subqueries'][1]['filters'].update(ticker='PFE', year=2025, accession=result['plan']['subqueries'][0]['filters']['accession'])
        self.save('broad12', result)
        self.assertDetected('broad12', 'plan scopes differ')

    def test_retention_continuity_is_enforced(self):
        result = self.load('broad11')
        retry_hops = [h for h in result['hops'] if h['attempt'] == 1]
        self.assertTrue(retry_hops, 'fixture should exercise a retry with a carried passage')
        retry_hops[0]['retained_evidence_ids'] = ['000000000000000000#passage-000001']
        self.save('broad11', result)
        self.assertDetected('broad11', 'carried evidence not identical')
        result = self.fixture.result(self.suite['cases'][10])
        hop = next(h for h in result['hops'] if h['attempt'] == 1)
        hop['evidence'][0] = dict(hop['evidence'][0], text=hop['evidence'][0]['text'] + ' changed')
        self.save('broad11', result)
        self.assertDetected('broad11', 'carried evidence not identical')
        result = self.fixture.result(self.suite['cases'][10])
        hop = next(h for h in result['hops'] if h['attempt'] == 1)
        hop['retained_evidence_ids'] = [e['id'] for e in hop['evidence'][:3]]
        self.save('broad11', result)
        self.assertDetected('broad11', 'carried passages')
        result = self.fixture.result(self.suite['cases'][10])
        next(h for h in result['hops'] if h['attempt'] == 0)['retained_evidence_ids'] = ['x#y']
        self.save('broad11', result)
        self.assertDetected('broad11', 'carried evidence not identical')

    def traced(self, case_id='broad11'):
        """Fixture result with a self-consistent selection trace added to every hop."""
        result = self.fixture.result(self.suite['cases'][10])
        for hop in result['hops']:
            carried = hop['retained_evidence_ids']
            hop['nominated_retained_ids'] = list(carried)
            hop['fresh_verified_ids'] = [e['id'] for e in hop['evidence'] if e['id'] not in carried]
            hop['retrieved_ids'] = list(hop['fresh_verified_ids'])
        return result

    def test_selection_trace_valid_and_legacy_traces_verify(self):
        result = self.traced()
        self.save('broad11', result)
        self.assertEqual(self.problems(self.verify(), 'broad11'), [])
        legacy = self.fixture.result(self.suite['cases'][10])  # no trace keys at all
        self.save('broad11', legacy)
        self.assertEqual(self.problems(self.verify(), 'broad11'), [])

    def test_selection_trace_mutations_are_detected(self):
        retry = lambda r: next(h for h in r['hops'] if h['attempt'] == 1 and h['retained_evidence_ids'])
        def mutate(change, fragment):
            result = self.traced()
            change(result, retry(result))
            self.save('broad11', result)
            self.assertDetected('broad11', fragment)
        mutate(lambda r, h: h.update(nominated_retained_ids=h['nominated_retained_ids'] + ['a#b', 'c#d']), 'nominees exceed 2')
        mutate(lambda r, h: h.update(nominated_retained_ids=h['nominated_retained_ids'] * 2), 'nominees exceed 2 or repeat')
        mutate(lambda r, h: h.update(fresh_verified_ids=h['fresh_verified_ids'] * 3, retrieved_ids=h['retrieved_ids'] * 3), 'fresh verified IDs exceed 5 or repeat')
        mutate(lambda r, h: h.update(retrieved_ids=[f'0#passage-{i:06}' for i in range(11)] ), 'retrieved IDs exceed 10')
        mutate(lambda r, h: h.update(retrieved_ids=h['retrieved_ids'] + ['0#passage-000099'], fresh_verified_ids=['0#passage-000099'] + h['fresh_verified_ids']), 'ordered subset')
        mutate(lambda r, h: h.update(fresh_verified_ids='not-a-list'), 'lists of IDs')
        mutate(lambda r, h: h.pop('retrieved_ids'), 'partial selection trace')
        # Wrong carried choice: a nominee silently dropped although a slot was free.
        mutate(lambda r, h: h.update(retained_evidence_ids=[]), 'differs from the budget result')
        # Nominee that was not in the previous context.
        mutate(lambda r, h: h.update(nominated_retained_ids=['000000000000000000#passage-000001']), 'nominees are not previous-context')

    def test_displaced_rank_four_is_detected(self):
        result = self.traced()
        hop = next(h for h in result['hops'] if h['attempt'] == 1 and h['retained_evidence_ids'])
        first = self.fixture.passages  # noqa: F841  (keep fixture alive)
        # Claim four fresh hits but publish a context that dropped the fourth.
        fresh = [e['id'] for e in hop['evidence'] if e['id'] not in hop['retained_evidence_ids']]
        hop['fresh_verified_ids'] = fresh + ['0' * 18 + '#passage-000077', '0' * 18 + '#passage-000078'][:max(0, 4 - len(fresh))]
        hop['retrieved_ids'] = list(hop['fresh_verified_ids'])
        self.save('broad11', result)
        self.assertDetected('broad11', 'differs from carried-then-fresh budget')

    def test_model_call_ledger_allowlist_and_exact_counts(self):
        call = lambda name: dict(schema=name, models=['fixture-model'])
        def with_calls(edit):
            result = self.fixture.result(self.suite['cases'][10])
            edit(result['model_calls'])
            self.save('broad11', result)
        def synthesis_index(calls):
            return next(i for i, c in enumerate(calls) if c['schema'] == 'Draft')
        # Legacy Draft ledger (the unmodified fixture) and a HandleDraft ledger both verify.
        with_calls(lambda c: None)
        self.assertEqual(self.problems(self.verify(), 'broad11'), [])
        with_calls(lambda c: c[synthesis_index(c)].update(schema='HandleDraft'))
        self.assertEqual(self.problems(self.verify(), 'broad11'), [])
        for fragment, edit in [
                ('unknown model-call schemas', lambda c: c.append(call('Browse'))),
                ('inconsistent with trace', lambda c: c.append(call('Draft'))),  # double synthesis
                ('inconsistent with trace', lambda c: c.append(call('HandleDraft'))),  # both kinds
                ('inconsistent with trace', lambda c: c.__setitem__(slice(None), [x for x in c if x['schema'] != 'Draft'])),  # missing
                ('inconsistent with trace', lambda c: c.append(call('Plan'))),
                ('inconsistent with trace', lambda c: c.append(call('Critique'))),
                ('unknown model-call schemas', lambda c: c[synthesis_index(c)].update(schema='Handle_Draft'))]:
            with self.subTest(fragment=fragment):
                with_calls(edit)
                self.assertDetected('broad11', fragment)

    def goal_result(self):
        """Fixture result carrying extractive goals (spans of the frozen prompt) in the plan and on every hop."""
        result = self.fixture.result(self.suite['cases'][10])
        prompt = result['prompt']
        for position, query in enumerate(result['plan']['subqueries']):
            query['goal'] = ' '.join(prompt.split())[10 * position:10 * position + 50]
        for hop in result['hops']:
            hop['requested_goal'] = result['plan']['subqueries'][hop['subquery']]['goal']
        return result

    def test_goal_trace_valid_legacy_and_mutations(self):
        self.save('broad11', self.goal_result())
        self.assertEqual(self.problems(self.verify(), 'broad11'), [])
        self.save('broad11', self.fixture.result(self.suite['cases'][10]))  # legacy: no goal anywhere
        self.assertEqual(self.problems(self.verify(), 'broad11'), [])

        def mutate(change, fragment):
            result = self.goal_result()
            change(result)
            self.save('broad11', result)
            self.assertDetected('broad11', fragment)
        mutate(lambda r: r['plan']['subqueries'][0].update(goal='an invented requirement not in the prompt'), 'contiguous span')
        mutate(lambda r: r['plan']['subqueries'][0].update(goal='x' * 401), 'contiguous span')
        mutate(lambda r: r['plan']['subqueries'][0].update(goal=''), 'goals must be present')
        mutate(lambda r: r['plan']['subqueries'][1].pop('goal'), 'goals must be present')
        mutate(lambda r: r['plan']['subqueries'][0].update(goal=7), 'goals must be present')
        mutate(lambda r: r['hops'][0].update(requested_goal='different but still prompt text'), 'graded goal differs')
        mutate(lambda r: r['hops'][-1].pop('requested_goal'), 'graded goal differs')
        def legacy_plan_with_hop_goal(r):
            for q in r['plan']['subqueries']:
                q.pop('goal')
        mutate(legacy_plan_with_hop_goal, 'graded goal differs')

    STOP = 'Subquery {} stopped early: no verified evidence and the critic proposed no new query (stalled rewrite).'

    def case_of(self, case_id):
        return next(c for c in self.suite['cases'] if c['id'] == case_id)

    def with_rewrite_keys(self, result):
        """Exhausted/accepted fixture trace upgraded with a self-consistent rewrite trace (proposed_rewrite == next query)."""
        by_query = {}
        for hop in result['hops']:
            by_query.setdefault(hop['subquery'], []).append(hop)
        for hops in by_query.values():
            for index, hop in enumerate(hops):
                hop['proposed_rewrite'] = '' if hop['accepted'] else (hops[index + 1]['query'] if index + 1 < len(hops) else 'final unused rewrite')
                hop['terminal_reason'] = None
        return result

    def stalled_result(self, case_id='broad27', attempts=1):
        """Unavailable-scope trace in which every subquery stops after `attempts` hops on a marked unchanged rewrite."""
        result = self.with_rewrite_keys(self.fixture.result(self.case_of(case_id)))
        positions = sorted({h['subquery'] for h in result['hops']})
        keep, flags = [], []
        for position in positions:
            hops = [h for h in result['hops'] if h['subquery'] == position][:attempts]
            hops[-1].update(proposed_rewrite=hops[-1]['query'].upper(), terminal_reason='stalled_rewrite')
            for index, hop in enumerate(hops[:-1]):
                hop['proposed_rewrite'] = hops[index + 1]['query']
            keep += hops
            flags += [f'Subquery {position} has no evidence meeting threshold after {attempts - 1} retries.', self.STOP.format(position)]
        flags.append('No verified answer; evidence unavailable or below threshold.')
        result['hops'] = keep
        result['flags'] = flags
        result['model_calls'] = [dict(schema='Plan', models=['fixture-model'])] + [dict(schema='Critique', models=['fixture-model'])] * len(keep)
        result['markdown'] = CANNED + '\n\nEvidence gaps:\n' + '\n'.join('- ' + verifier.escape_markdown(f) for f in flags)
        return result

    def test_valid_stalled_stop_and_legacy_traces_verify(self):
        for attempts in (1, 2):
            self.save('broad27', self.stalled_result(attempts=attempts))
            self.refresh_ledgers()
            self.assertEqual(self.problems(self.verify(), 'broad27'), [], attempts)
        self.save('broad27', self.with_rewrite_keys(self.fixture.result(self.case_of('broad27'))))  # exhausted, new keys
        self.refresh_ledgers()
        self.assertEqual(self.problems(self.verify(), 'broad27'), [])
        self.save('broad27', self.fixture.result(self.case_of('broad27')))  # legacy: 3 hops, no keys
        self.refresh_ledgers()
        self.assertEqual(self.problems(self.verify(), 'broad27'), [])

    def test_invalid_stalled_stops_and_rewrite_trace_mutations_are_rejected(self):
        def stalled(change, fragment, attempts=1):
            result = self.stalled_result(attempts=attempts)
            change(result)
            self.save('broad27', result)
            self.assertDetected('broad27', fragment)
        def exhausted(change, fragment):
            result = self.with_rewrite_keys(self.fixture.result(self.case_of('broad27')))
            change(result)
            self.save('broad27', result)
            self.assertDetected('broad27', fragment)
        first = lambda r: next(h for h in r['hops'] if h['terminal_reason'])
        invalid = 'invalid stalled-rewrite stop'
        stalled(lambda r: first(r).update(score=2), invalid)  # score must be 1
        stalled(lambda r: first(r).update(proposed_rewrite='a genuinely different query'), invalid)  # changed rewrite is not a stall
        stalled(lambda r: first(r).update(accepted=True), invalid)  # accepted cannot stall
        stalled(lambda r: first(r).update(evidence=[dict(id='x', text='t')]), invalid)  # evidence cannot stall
        stalled(lambda r: first(r).update(terminal_reason=None), 'not marked as a stalled stop')  # silent early stop
        stalled(lambda r: r['flags'].remove(self.STOP.format(0)), 'stop-reason flag inconsistent')
        stalled(lambda r: first(r).pop('terminal_reason'), 'partial rewrite trace')
        stalled(lambda r: first(r).update(terminal_reason='gave_up'), 'malformed rewrite trace')
        stalled(lambda r: first(r).update(proposed_rewrite='x'), 'did not record the critic rewrite')
        # Terminal on a non-final hop / on the final attempt of an exhausted run.
        exhausted(lambda r: r['hops'][0].update(terminal_reason='stalled_rewrite', proposed_rewrite=r['hops'][0]['query']), invalid)
        exhausted(lambda r: r['hops'][2].update(terminal_reason='stalled_rewrite', proposed_rewrite=r['hops'][2]['query']), invalid)
        exhausted(lambda r: r['hops'][1].update(proposed_rewrite='not what the next hop ran'), 'next query is not the recorded critic rewrite')
        # Unchanged rewrite that was not marked, followed by a different query.
        exhausted(lambda r: r['hops'][0].update(proposed_rewrite=r['hops'][0]['query']), 'unchanged rewrite not marked')
        # Arbitrary early stop in a legacy (key-less) trace is still rejected.
        result = self.fixture.result(self.case_of('broad27'))
        result['hops'] = [h for h in result['hops'] if h['attempt'] == 0]
        self.save('broad27', result)
        self.assertDetected('broad27', 'stopped before accepting, exhausting retries or a valid stalled-rewrite stop')

    def test_accepted_hop_cannot_carry_a_rewrite_or_terminal_reason(self):
        result = self.with_rewrite_keys(self.fixture.result(self.case_of('broad11')))
        accepted = next(h for h in result['hops'] if h['accepted'])
        accepted['proposed_rewrite'] = 'leftover rewrite'
        self.save('broad11', result)
        self.assertDetected('broad11', 'accepted hop carries a rewrite')

    def test_stalled_available_scope_is_a_quality_hold_not_a_pass(self):
        result = self.with_rewrite_keys(self.fixture.result(self.case_of('broad11')))
        for position in sorted({h['subquery'] for h in result['hops']}):
            hops = [h for h in result['hops'] if h['subquery'] == position]
            if position == 0:  # an AVAILABLE scope that stalls on empty evidence stays an honest, flagged gap
                for hop in hops[1:]:
                    result['hops'].remove(hop)
                hops[0].update(score=1, accepted=False, evidence=[], retained_evidence_ids=[], proposed_rewrite=hops[0]['query'], terminal_reason='stalled_rewrite')
        result['citations'] = [c for c in result['citations'] if False]
        result['flags'] = [f for f in result['flags'] if not f.startswith('Subquery 0')] + [
            'Subquery 0 has no evidence meeting threshold after 0 retries.', self.STOP.format(0)]
        self.save('broad11', result)
        summary = self.verify()
        entry = next(r for r in summary['cases'] if r['id'] == 'broad11')
        self.assertFalse(summary['passed'])  # never counted as supported/complete coverage
        self.assertFalse(entry.get('complete_coverage', False))

    def test_bounds_and_state_order_are_enforced(self):
        result = self.load('audit05')
        result['states'] = ['Plan', 'Synthesize', 'Retrieve_Evaluate']
        self.save('audit05', result)
        self.assertDetected('audit05', 'invalid state order')
        result = self.load('audit05')
        result['hops'][0]['evidence'] = result['hops'][0]['evidence'] * 2
        self.save('audit05', result)
        self.assertDetected('audit05', 'context')

    def test_failures_are_recorded_by_category_and_never_pass(self):
        for suffix in ('json', 'md'):
            (self.run_dir / 'evaluation' / f'broad15.{suffix}').unlink()
        (self.run_dir / 'evaluation' / 'broad15.failure.json').write_text(json.dumps(dict(id='broad15', status='fail', phase='agent_run',
            category='transport', error_type='ConnectError', error='ConnectError: refused', traceback='Traceback ...')))
        summary = self.verify()
        record = next(r for r in summary['cases'] if r['id'] == 'broad15')
        self.assertEqual((record['verdict'], record['failure_category']), ('run_failed', 'transport'))
        self.assertEqual(summary['failures_by_category'], {'transport': 1, 'model': 0, 'structural': 0})
        self.assertEqual(summary['omitted'], [])
        self.assertFalse(summary['passed'])
        self.assertEqual(summary['verified'], 29)

    def test_honest_partial_gap_on_an_available_scope_is_not_a_failure(self):
        case = next(c for c in self.suite['cases'] if c['id'] == 'broad19')
        result = self.fixture.result(case, weak={0})
        self.save('broad19', result)
        self.refresh_ledgers()
        summary = self.verify()
        record = next(r for r in summary['cases'] if r['id'] == 'broad19')
        self.assertEqual((record['verdict'], record['evidence_outcome'], record['problems']), ('verified', 'partial_gap', []))
        self.assertEqual(summary['evidence_outcomes']['partial_gap'], 1)
        self.assertTrue(summary['passed'])


    # ---- review fixes: evaluator summary, artifacts, mixed-case coverage, gates ----
    def edit_report(self, change):
        path = self.run_dir / 'evaluation' / 'report.json'
        report = json.loads(path.read_text())
        change(report)
        path.write_text(json.dumps(report))

    def assertDiscrepancy(self, fragment):
        summary = self.verify()
        self.assertFalse(summary['passed'])
        self.assertTrue(any(fragment in d for d in summary['evaluator_summary_discrepancies']), summary['evaluator_summary_discrepancies'])
        return summary

    def test_reported_per_case_metric_must_match_recomputed_values(self):
        self.edit_report(lambda r: r['cases'][3].update(citations=r['cases'][3]['citations'] + 1))
        self.assertDiscrepancy('reported citations=')

    def test_aggregate_counts_must_match_recomputed_values(self):
        self.edit_report(lambda r: r.update(retries=r['retries'] + 5))
        self.assertDiscrepancy('aggregate retries')
        self.edit_report(lambda r: r.update(passed=30, evidence_outcomes={'supported': 30}))
        self.assertDiscrepancy('aggregate evidence_outcomes')

    def test_duplicate_report_ids_and_ledger_membership_are_detected(self):
        self.edit_report(lambda r: r['cases'].append(dict(r['cases'][0])))
        self.assertDiscrepancy('duplicate case IDs')
        self.refresh_ledgers()
        ledger = self.run_dir / 'evaluation' / 'records.jsonl'
        lines = ledger.read_text().splitlines()
        ledger.write_text('\n'.join(lines[:-1]) + '\n')
        self.assertDiscrepancy('records.jsonl: case IDs differ')
        ledger.write_text('\n'.join(lines + [lines[0]]) + '\n')
        self.assertDiscrepancy('records.jsonl: duplicate case IDs')

    def test_missing_ledgers_fail_the_run(self):
        (self.run_dir / 'evaluation' / 'records.jsonl').unlink()
        self.assertDiscrepancy('records.jsonl missing')
        (self.run_dir / 'evaluation' / 'report.json').unlink()
        self.assertDiscrepancy('report.json missing')

    def test_evaluator_pass_for_a_case_that_fails_independent_checks_is_a_discrepancy(self):
        result = self.load('audit03')
        result['hops'][0]['evidence'][0]['text'] += ' x'
        self.save('audit03', result)
        self.assertDiscrepancy('evaluator says pass, independent verdict verification_failed')

    def test_artifact_hashes_are_recomputed_from_recorded_paths(self):
        summary = self.verify()
        art = summary['artifact_verification']
        self.assertEqual((art['vector_index']['recomputed_verified'], art['reranker_model']['recomputed_verified'], art['sparse_index']['recomputed_verified']), (2, 1, 2))
        self.assertEqual(art['code']['required_but_unrecorded'], [])
        self.assertEqual(art['corpus_sha256'], 'recomputed_verified')

    def test_modified_missing_or_extra_index_and_model_files_fail(self):
        target = self.root / 'models/rr/model.onnx'
        target.write_bytes(b'swapped model')
        summary = self.verify()
        self.assertFalse(summary['passed'])
        self.assertTrue(any('reranker_model: 1 mismatched' in p for p in summary['global_problems']), summary['global_problems'])
        target.unlink()
        self.assertTrue(any('reranker_model: 1 missing' in p for p in self.verify()['global_problems']))
        (self.root / 'indexes/vec/extra.bin').write_bytes(b'x')
        self.assertTrue(any('vector_index: 1 unrecorded' in p for p in self.verify()['global_problems']))

    def test_recorded_presence_without_matching_bytes_is_not_accepted(self):
        path = self.run_dir / 'artifact-hashes.json'
        hashes = json.loads(path.read_text())
        hashes['sparse_index'] = {k: 'deadbeef' for k in hashes['sparse_index']}
        path.write_text(json.dumps(hashes))
        summary = self.verify()
        self.assertTrue(any('sparse_index: 2 mismatched' in p for p in summary['global_problems']))
        self.assertEqual(summary['artifact_verification']['sparse_index']['recomputed_verified'], 0)

    def test_unsafe_recorded_paths_are_rejected(self):
        path = self.run_dir / 'artifact-hashes.json'
        hashes = json.loads(path.read_text())
        hashes['vector_index']['../../etc/hostname'] = 'x'
        path.write_text(json.dumps(hashes))
        self.assertTrue(any('unsafe recorded path' in p for p in self.verify()['global_problems']))

    def test_code_drift_is_reported_for_replay_and_fails_only_when_strict(self):
        (self.root / 'py/service.py').write_text('newer sidecar code')
        summary = self.verify()
        self.assertEqual(summary['artifact_verification']['code']['changed'], ['py/service.py'])
        self.assertTrue(summary['passed'])
        strict = self.verify(strict_code=True)
        self.assertFalse(strict['passed'])
        self.assertTrue(any('strict code check' in p for p in strict['global_problems']))

    def test_code_hashes_must_cover_sidecar_and_go_sources(self):
        path = self.run_dir / 'artifact-hashes.json'
        hashes = json.loads(path.read_text())
        del hashes['code']['py/service.py'], hashes['code']['pkg/hybrid/hybrid.go']
        path.write_text(json.dumps(hashes))
        summary = self.verify()
        self.assertFalse(summary['passed'])
        self.assertEqual(summary['artifact_verification']['code']['required_but_unrecorded'], ['py/service.py', 'pkg/hybrid/hybrid.go'])

    def test_supervisor_code_selection_includes_sidecar_agent_and_go_stack(self):
        recorded = set(lib.code_files(ROOT))
        self.assertTrue(set(lib.REQUIRED_CODE) <= recorded, set(lib.REQUIRED_CODE) - recorded)
        self.assertTrue({'py/rerank.py', 'py/agent/provenance.py', 'pkg/index/bm25.go', 'cmd/finfilings/listener.go'} <= recorded)
        self.assertFalse([f for f in recorded if f.split('/')[-1].startswith('test_') or f.endswith('_test.go')])

    def test_valid_mixed_cases_report_available_side_coverage_and_withholding_separately(self):
        summary = self.verify()
        self.assertEqual(summary['mixed_cases'], {
            'broad29': dict(result='mixed_fully_supported', available_side='1/1', unavailable_withheld=True),
            'broad30': dict(result='mixed_fully_supported', available_side='1/1', unavailable_withheld=True)})
        record = next(r for r in summary['cases'] if r['id'] == 'broad29')
        self.assertEqual((record['available_subqueries'], record['cited_available_subqueries']), (1, 1))
        self.assertEqual(summary['available_subqueries'], sum(r['available_subqueries'] for r in summary['cases']))
        self.assertTrue(summary['release_gates']['mixed_cases_preserve_unavailable_and_fully_support_available'])

    def test_available_side_gap_in_a_mixed_case_is_a_structural_pass_but_not_a_successful_mixed_case(self):
        case = next(c for c in self.suite['cases'] if c['id'] == 'broad29')
        self.save('broad29', self.fixture.result(case, weak={0}))
        self.refresh_ledgers()
        summary = self.verify()
        record = next(r for r in summary['cases'] if r['id'] == 'broad29')
        self.assertEqual(record['verdict'], 'verified')
        self.assertEqual((record['mixed_result'], record['unavailable_withheld']), ('mixed_available_side_gap', True))
        self.assertEqual(summary['mixed_cases']['broad29']['available_side'], '0/1')
        self.assertTrue(summary['passed'])
        gates = summary['release_gates']
        self.assertFalse(gates['mixed_cases_preserve_unavailable_and_fully_support_available'])
        self.assertFalse(gates['all_checked_gates_met'])

    def test_gate_arithmetic_and_unchecked_gates_are_explicit(self):
        gates = self.verify()['release_gates']
        self.assertTrue(gates['all_checked_gates_met'])
        self.assertEqual(gates['citation_coverage_of_available_subqueries']['ratio'], 1.0)
        self.assertIn('Go race/vet', ' '.join(gates['not_checked_by_this_tool']))
        summary = dict(self.baseline, available_subqueries=10, cited_available_subqueries=7)
        low = verifier.release_gates(summary, self.baseline['cases'])['citation_coverage_of_available_subqueries']
        self.assertEqual((low['ratio'], low['met']), (0.7, False))
        edge = verifier.release_gates(dict(summary, cited_available_subqueries=8), self.baseline['cases'])['citation_coverage_of_available_subqueries']
        self.assertTrue(edge['met'])

    def test_unavailable_scope_claims_fail_the_zero_claim_gate(self):
        result = self.load('broad28')
        self.assertEqual(result['citations'], [])
        source = next(s for s in self.sources if s['ticker'] == 'NVDA' and s['filing_year'] == 2024)
        item = self.fixture.evidence(self.passages[(source['accession'], '')][0], source)
        text = ' '.join(item['text'].split())[:100]
        result['citations'] = [dict(tag=item['tag'], chunk_id=item['id'], accession=item['accession'], url=item['url'], quote=text)]
        self.save('broad28', result)
        summary = self.verify()
        self.assertFalse(summary['passed'])
        self.assertFalse(summary['release_gates']['unavailable_scopes_publish_zero_claims'])


class EvaluatorContracts(unittest.TestCase):
    """The evaluator's in-process per-scope verification and failure isolation, with fixture inference only."""

    @classmethod
    def setUpClass(cls):
        require_artifacts()
        provenance = HISTORY / 'agent-provenance.json'
        if not provenance.exists():
            raise unittest.SkipTest('requires enrolled expanded SEC corpus')
        import eval_agent
        from agent.provenance import ProvenanceRegistry
        cls.eval_agent = eval_agent
        cls.registry = ProvenanceRegistry(CORPUS, provenance)
        cls.addClassCleanup(cls.registry.close)
        cls.suite, _ = lib.load_suite(SUITE, SUITE.with_suffix('.sha256').read_text().split()[0])
        cls.sources = lib.build_trusted_sources(ORIGINAL, REPORT)
        cls.passages = load_passages(cls.sources)

    def test_registry_preflight_agrees_with_frozen_expectations_without_counts(self):
        lib.check_expected_scopes(self.suite, list(self.registry.sources.values()), lib.registry_sections(self.registry), compare_counts=False)
        drifted = copy.deepcopy(self.suite)
        drifted['cases'][28]['expected_scopes'][1]['available'] = True
        with self.assertRaises(lib.SuiteError):
            lib.check_expected_scopes(drifted, self.sources, lib.registry_sections(self.registry), compare_counts=False)

    def test_legacy_classifier_is_per_scope(self):
        case = self.eval_agent.cases()[8]
        expanded = self.eval_agent.legacy_expected(case, self.registry)
        self.assertEqual((case['id'], expanded['negative']), ('audit09', False))
        negative = self.eval_agent.legacy_expected(self.eval_agent.cases()[9], self.registry)
        self.assertTrue(negative['negative'])
        mixed = self.eval_agent.legacy_expected(dict(id='m', prompt='x', scopes=[['KO', 2025], ['AMZN', 2024]], negative=False, section='Item 7'), self.registry)
        self.assertEqual([r['available'] for r in mixed['expected_scopes']], [True, False])
        self.assertFalse(mixed['negative'])

    def agent_result(self, case_id):
        from agent.models import AgentResult
        case = next(c for c in self.suite['cases'] if c['id'] == case_id)
        raw = FixtureRun(self.suite, '', self.sources, self.passages).result(case)
        return case, AgentResult.model_validate_json(json.dumps(raw))

    def test_in_process_verify_accepts_mixed_trace_and_rejects_unavailable_scope_evidence(self):
        case, result = self.agent_result('broad29')
        case = dict(case, require_citations=False)
        record = self.eval_agent.verify(result, case, self.registry)
        self.assertEqual((record['status'], record['negative'], record['unavailable_subqueries']), ('pass', False, [1]))
        self.assertEqual(record['evidence_outcome'], 'supported')
        case, positive = self.agent_result('broad14')
        self.assertEqual(self.eval_agent.verify(positive, dict(case, require_citations=False), self.registry)['evidence_outcome'], 'supported')
        case, wrong = self.agent_result('broad29')
        as_negative = copy.deepcopy(case)
        as_negative['expected_scopes'][0]['available'] = False  # claims GS is unavailable but the trace has GS evidence
        with self.assertRaises(AssertionError):
            self.eval_agent.verify(wrong, dict(as_negative, require_citations=False), self.registry)

    def test_run_case_records_failures_with_real_categories_and_continues(self):
        from agent.provider import ClaudeJSON
        from agent.retrieval import HybridClient
        from agent.state_machine import Agent
        from agent.models import Plan, SubQuery, Critique
        from agent.filters import FilingFilter
        case = dict(next(c for c in self.suite['cases'] if c['id'] == 'broad14'), require_citations=False)
        scope = case['expected_scopes'][0]

        class PlanOnly:  # fixture inference for failure-path wiring only
            calls = []

            async def generate(self, schema, instruction, payload):
                self.seen = getattr(self, 'seen', []) + [json.dumps(payload)]
                if schema is Plan:
                    filters = FilingFilter(ticker='TGT', year=2026)
                    span = ' '.join(payload['prompt'].split())[:40]  # planner must now supply a prompt-span goal
                    return Plan(subqueries=[SubQuery(query='inventory shrink estimate', goal=span, filters=filters), SubQuery(query='capital expenditures', goal=span, filters=filters)])
                raise AssertionError('unexpected call')

        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                out = Path(directory)
                async with HybridClient('http://127.0.0.1:9') as backend:  # closed local port: real httpx transport error
                    model = PlanOnly()
                    transport = await self.eval_agent.run_case(Agent(model, backend, self.registry, mode='legacy'), self.registry, case, out, True)
                    self.assertTrue((out / 'broad14.failure.json').exists())
                    leaked = [a['id'] for a in case['reviewer_evidence']['anchors']] + [a['excerpt'][:40] for a in case['reviewer_evidence']['anchors']]
                    self.assertFalse([t for t in leaked if any(t in payload for payload in model.seen)])
                    self.assertIn(json.dumps(case['prompt'])[1:-1], ''.join(model.seen))
                missing_cli = Agent(ClaudeJSON(executable='/nonexistent/finfilings-model'), None, self.registry)
                model_failure = await self.eval_agent.run_case(missing_cli, self.registry, case, out, True)
                return transport, model_failure
        transport, model_failure = asyncio.run(scenario())
        self.assertEqual((transport['status'], transport['category'], transport['phase']), ('fail', 'transport', 'agent_run'))
        self.assertIn('ConnectError', transport['error'])
        self.assertEqual((model_failure['status'], model_failure['category']), ('fail', 'model'))
        self.assertTrue(transport['traceback'] and model_failure['traceback'])
        self.assertEqual(scope['accession'], '000002741926000016')

    def test_structural_failures_are_distinct(self):
        from agent.state_machine import Agent
        try:
            Agent(None, None, self.registry, max_retries=3)
        except ValueError as exc:
            self.assertEqual(lib.classify_failure(exc), 'structural')
        try:
            assert False, 'invalid citation'
        except AssertionError as exc:
            self.assertEqual(lib.classify_failure(exc), 'structural')


class HistoricalTraces(unittest.TestCase):
    """REAL saved ten-audit traces (live model + retrieval) must satisfy both the new verifier and the evaluator."""

    def test_saved_real_regression_traces_pass_both_checkers(self):
        require_artifacts()
        if not (HISTORY / 'agent-provenance.json').exists():
            self.skipTest('requires saved expanded-corpus traces')
        suite, _ = lib.load_suite(SUITE, SUITE.with_suffix('.sha256').read_text().split()[0])
        sources = lib.build_trusted_sources(ORIGINAL, REPORT)
        results = {i: json.loads((HISTORY / 'evaluation' / f'{i}.json').read_text()) for i in lib.REGRESSION_IDS}
        needed = {e['id'] for r in results.values() for h in r['hops'] for e in h['evidence']}
        _, originals = lib.scan_corpus(CORPUS, {s['accession'] for s in sources}, needed)
        by_scope = {(s['ticker'], s['filing_year']): s for s in sources}
        import eval_agent
        from agent.models import AgentResult
        from agent.provenance import ProvenanceRegistry
        registry = ProvenanceRegistry(CORPUS, HISTORY / 'agent-provenance.json')
        self.addCleanup(registry.close)
        citations = 0
        for case in suite['cases'][:10]:
            checks = verifier.Checks()
            metrics = verifier.audit_case(case, results[case['id']], (HISTORY / 'evaluation' / f"{case['id']}.md").read_text(), by_scope, originals, checks)
            self.assertEqual(checks.problems, [], case['id'])
            citations += metrics['citations']
            expected = eval_agent.legacy_expected(next(c for c in eval_agent.cases() if c['id'] == case['id']), registry)
            record = eval_agent.verify(AgentResult.model_validate_json(json.dumps(results[case['id']])), expected, registry)
            self.assertEqual(record['evidence_outcome'], metrics['evidence_outcome'], case['id'])
        self.assertEqual(citations, 61)  # the 61 citations recorded by the historical report


class TrustedEnrollment(unittest.TestCase):
    def test_shared_enrollment_equals_the_saved_historical_enrollment(self):
        require_artifacts()
        saved = json.loads((HISTORY / 'agent-provenance.json').read_text())
        self.assertEqual(lib.build_trusted_sources(ORIGINAL, REPORT), saved['sources'])


if __name__ == '__main__':
    unittest.main()
