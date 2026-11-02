"""Tests for the parser-v3 corpus adoption: migration, lineage, suite derivation, parameterization and gates.

Three kinds of evidence, kept apart:
* SYNTHETIC FIXTURES (class `SyntheticMigration`, `GateArithmetic`): tiny generated corpora/reports/metrics that exist only to
  exercise the tools' contracts and failure paths. They say nothing about retrieval quality, compression or latency.
* REAL ARTIFACTS (`RealV3Artifacts`, `VerifierContractsV3`): the actual v3 corpus, ingestion report, derived suite and the verified
  Morgan Stanley replay files. Skipped when those local, git-ignored artifacts are absent.
* No test starts a model, a retrieval service or the network.
"""
import contextlib
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bench_kit'))
sys.path.insert(0, str(ROOT / 'py'))
import adopt_parser_v3 as adopt  # noqa: E402
import agent_suite as lib  # noqa: E402
import build_broad_suite as builder  # noqa: E402
import compare_v3_gates as gates  # noqa: E402
import test_broad_agent_suite as tb  # noqa: E402
import verify_broad_agent as verifier  # noqa: E402

TARGET = adopt.TARGET_ACCESSION
OTHER_A, OTHER_B = '000032019325000079', '000078901425000010'
V3_DATA = ROOT / 'data/sec-scale-v3-evaluation-20261101'
V3_SEC = ROOT / 'indexes/sec-scale-v3-20261101'
V2_DATA = ROOT / 'data/sec-scale-v2-evaluation-20261031'
V2_SEC = ROOT / 'indexes/sec-scale-v2-20261031'
SUITE_V1 = ROOT / 'bench_kit/suites/broad-validation-v1.json'
SUITE_V2 = ROOT / 'bench_kit/suites/broad-validation-v2.json'
REPLAY = ROOT / 'indexes/parser-boundaries-20261101-codex'
RAW = ROOT / adopt.DEFAULTS['raw']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def passage(accession, section, number, text, ticker='MS'):
    name = {'Item 1A': 'item1A', 'Item 7': 'item7', 'Item 8': 'item8'}[section]
    return (json.dumps(dict(ID=f'{accession}#{name}#passage-{number:06d}', Ticker=ticker, CIK='1', FormType='10-K', FilingYear=2026,
                            SectionItem=section, Text=text), separators=(',', ':')) + '\n').encode()


def filing_entry(ticker, accession, parser, raw_sha, raw_bytes, chunks):
    sections = {'Item 1A': dict(words=10, chunks=chunks[0]), 'Item 7': dict(words=20, chunks=chunks[1]), 'Item 8': dict(words=30, chunks=chunks[2])}
    return dict(ref=dict(CIK='1', Ticker=ticker, Accession=accession, Form='10-K', FilingDate='2026-02-19T00:00:00Z', ReportDate='2025-12-31', PrimaryDocument='x.htm'),
                shard='2026_Q1', download=dict(url=f'https://www.sec.gov/Archives/edgar/data/1/{accession}/x.htm', bytes=raw_bytes, sha256=raw_sha),
                parse=dict(raw_bytes=raw_bytes, raw_sha256=raw_sha, visible_words=100, candidate_headings=5, chunks=sum(chunks), sections=sections, parser=parser, heading_mode='titled'))


class SyntheticMigration(unittest.TestCase):
    """SYNTHETIC FIXTURE: a tiny corpus whose Morgan-Stanley-like filing loses one passage and changes another."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = b'current raw bytes of the replaced filing'
        self.raw_sha = sha(self.raw)
        (self.root / 'raw.htm').write_bytes(self.raw)
        baseline = [json.dumps(dict(ID=f'finqa:{i}', Text=f'baseline {i}'), separators=(',', ':')).encode() + b'\n' for i in range(3)]
        a = [passage(OTHER_A, 'Item 1A', 0, 'alpha risk', 'AAA'), passage(OTHER_A, 'Item 7', 0, 'alpha mda', 'AAA')]
        b = [passage(OTHER_B, 'Item 8', 0, 'beta statements', 'BBB')]
        self.old_ms = [passage(TARGET, 'Item 1A', 0, 'ms risk'), passage(TARGET, 'Item 7', 0, 'ms mda zero'), passage(TARGET, 'Item 7', 1, 'ms mda one OLD tail runs on'),
                       passage(TARGET, 'Item 7', 2, 'ms risk part wrongly in item 7'), passage(TARGET, 'Item 8', 0, 'ms statements')]
        self.new_ms = [self.old_ms[0], self.old_ms[1], passage(TARGET, 'Item 7', 1, 'ms mda one'), self.old_ms[4]]
        sec = a + self.old_ms + b
        self.write('base/data/chunks.jsonl', b''.join(baseline + sec))
        self.write('base/sec/passages.jsonl', b''.join(sec))
        (self.root / 'base/data/queries.jsonl').write_text('{"id":"q1"}\n')
        (self.root / 'base/data/qrels.tsv').write_text('q1\tfinqa:0\t1\n')
        old_raw_sha = sha(b'previously archived raw')
        entries = [filing_entry('AAA', OTHER_A, adopt.V2_PARSER, sha(b'a'), 100, (1, 1, 0)),
                   filing_entry('MS', TARGET, adopt.V2_PARSER, old_raw_sha, 111, (1, 3, 1)),
                   filing_entry('BBB', OTHER_B, adopt.V2_PARSER, sha(b'b'), 90, (0, 0, 1))]
        report = dict(version=1, parser=adopt.V2_PARSER, requests_per_second=5, filings=entries, skipped=[], chunks=8,
                      shards=[], sections={'Item 1A': 2, 'Item 7': 4, 'Item 8': 2})
        (self.root / 'base/sec/report.json').write_text(json.dumps(report, indent=2) + '\n')
        (self.root / 'base/sec/provenance.jsonl').write_text(''.join(json.dumps(e, separators=(',', ':')) + '\n' for e in entries))
        base = dict(corpus_sha256=sha(b''.join(baseline + sec)), query_sha256=sha(b'{"id":"q1"}\n'), qrels_sha256=sha(b'q1\tfinqa:0\t1\n'),
                    documents=11, base_corpus_sha256=sha(b''.join(baseline)), document_counts=dict(baseline=3, new_sec=8),
                    new_sec_passages_sha256=sha(b''.join(sec)), new_sec_report_sha256=sha((self.root / 'base/sec/report.json').read_bytes()),
                    new_sec_filings=3, new_sec_skipped=0, evaluation_notes=['note'])
        (self.root / 'base/data/provenance.json').write_text(json.dumps(base))
        self.write('v3/passages.jsonl', b''.join(self.new_ms))
        self.write('v3/segments/CORPUS_SHA256', (sha(b''.join(self.new_ms)) + '\n').encode())
        self.write('legacy/passages.jsonl', b''.join(self.old_ms))
        parse = dict(raw_bytes=len(self.raw), raw_sha256=self.raw_sha, visible_words=100, candidate_headings=4, chunks=4,
                     sections={'Item 1A': dict(words=10, chunks=1, start_word=0), 'Item 7': dict(words=18, chunks=2, start_word=5), 'Item 8': dict(words=30, chunks=1, start_word=50)},
                     parser=adopt.V3_PARSER, heading_mode='titled', ignored_titled_headings=1)
        (self.root / 'v3/parse-report.json').write_text(json.dumps(parse, indent=2) + '\n')
        self.summary = dict(source=dict(sha256=self.raw_sha),
                            parser_v3=dict(passages_sha256=sha(b''.join(self.new_ms)), artifact_file_sha256={'parse-report.json': sha((self.root / 'v3/parse-report.json').read_bytes())}),
                            parser_v2_legacy=dict(passages_sha256=sha(b''.join(self.old_ms))))
        self.write_summary()
        original = dict(sources=[dict(ticker='ZZZ', cik='9', filing_year=2023, form='10-K', url='https://www.sec.gov/Archives/edgar/data/9/000000000023000001/z.htm',
                                      accession='000000000023000001', raw_sha256=sha(b'z'), fiscal_period_end='2023-12-31')])
        self.write('data/sec/provenance.json', json.dumps(original).encode())

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def write_summary(self):
        (self.root / 'summary.json').write_text(json.dumps(self.summary))

    def options(self, command='build', **extra):
        argv = [command, '--root', str(self.root), '--base-data', 'base/data', '--base-sec', 'base/sec', '--replacement', 'v3', '--legacy', 'legacy',
                '--replay-summary', 'summary.json', '--raw', 'raw.htm', '--raw-sha256', self.raw_sha, '--out-data', 'out/data', '--out-sec', 'out/sec',
                '--expect-old-passages', '5', '--expect-new-passages', '4', '--expect-unchanged', '3', '--expect-removed', '1', '--expect-changed', '1',
                '--expect-old-total', '11', '--expect-new-total', '10', '--expect-changed-id', f'{TARGET}#item7#passage-000001']
        for key, value in extra.items():
            argv += [f"--{key.replace('_', '-')}", str(value)]
        return adopt.parse_args(argv)

    def build(self, **extra):
        return adopt.build(self.options(**extra))

    def assertNothingPublished(self):
        self.assertFalse((self.root / 'out/data').exists() or (self.root / 'out/sec').exists())
        self.assertEqual([p.name for p in (self.root / 'out').glob('*staging*')] if (self.root / 'out').exists() else [], [])

    def test_replaces_only_the_targeted_filing_and_preserves_every_other_record_byte_for_byte(self):
        result = self.build()
        self.assertEqual(result['documents'], 10)
        old = (self.root / 'base/data/chunks.jsonl').read_bytes().splitlines(keepends=True)
        new = (self.root / 'out/data/chunks.jsonl').read_bytes().splitlines(keepends=True)
        self.assertEqual([l for l in new if TARGET.encode() not in l], [l for l in old if TARGET.encode() not in l])
        self.assertEqual([l for l in new if TARGET.encode() in l], self.new_ms)
        first = next(i for i, l in enumerate(old) if TARGET.encode() in l)
        self.assertEqual(new[first:first + 4], self.new_ms)  # the replacement occupies the old slot
        passages = (self.root / 'out/sec/passages.jsonl').read_bytes()
        self.assertEqual(sha(passages), json.loads((self.root / 'out/data/provenance.json').read_text())['new_sec_passages_sha256'])
        self.assertEqual(adopt.verify_dirs(self.root / 'base/data', self.root / 'base/sec', self.root / 'out/data', self.root / 'out/sec',
                                           self.root / 'v3', self.root / 'legacy', self.root / 'raw.htm', self.options('verify'), self.root), [])

    def test_inputs_and_v2_stay_untouched_and_queries_are_byte_identical(self):
        before = {p: sha(p.read_bytes()) for p in (self.root / 'base').rglob('*') if p.is_file()}
        self.build()
        self.assertEqual({p: sha(p.read_bytes()) for p in (self.root / 'base').rglob('*') if p.is_file()}, before)
        for name in ('queries.jsonl', 'qrels.tsv'):
            self.assertEqual((self.root / 'out/data' / name).read_bytes(), (self.root / 'base/data' / name).read_bytes())

    def test_existing_outputs_are_never_overwritten(self):
        for blocked in ('out/data', 'out/sec'):
            with self.subTest(blocked=blocked):
                (self.root / blocked).mkdir(parents=True, exist_ok=True)
                (self.root / blocked / 'sentinel').write_text('keep')
                with self.assertRaises(FileExistsError):
                    self.build()
                self.assertEqual((self.root / blocked / 'sentinel').read_text(), 'keep')
                self.assertEqual([p.name for p in (self.root / 'out').iterdir() if 'staging' in p.name], [])
                import shutil
                shutil.rmtree(self.root / 'out')

    def test_wrong_raw_hash_is_rejected_before_anything_is_staged(self):
        (self.root / 'raw.htm').write_bytes(self.raw + b'x')
        with self.assertRaisesRegex(adopt.MigrationError, 'raw fixture sha256'):
            self.build()
        self.assertNothingPublished()

    def test_replacement_must_equal_the_verified_replay_summary(self):
        self.write('v3/passages.jsonl', b''.join(self.new_ms[:3]))
        self.write('v3/segments/CORPUS_SHA256', (sha(b''.join(self.new_ms[:3])) + '\n').encode())
        with self.assertRaisesRegex(adopt.MigrationError, 'differ from the verified replay summary'):
            self.build()
        self.assertNothingPublished()

    def test_legacy_replay_must_be_byte_identical_to_the_active_corpus(self):
        altered = list(self.old_ms)
        altered[1] = passage(TARGET, 'Item 7', 0, 'ms mda ZERO altered')
        self.write('legacy/passages.jsonl', b''.join(altered))
        self.summary['parser_v2_legacy']['passages_sha256'] = sha(b''.join(altered))
        self.write_summary()
        with self.assertRaisesRegex(adopt.MigrationError, 'legacy .* not byte-identical'):
            self.build()
        self.assertNothingPublished()

    def test_unexpected_counts_fail_after_staging_and_clean_up(self):
        with self.assertRaisesRegex(adopt.MigrationError, 'migration counts'):
            self.build(expect_removed=2)
        self.assertNothingPublished()

    def test_replacement_adding_unknown_ids_is_rejected(self):
        extra = self.new_ms + [passage(TARGET, 'Item 8', 1, 'brand new id')]
        self.write('v3/passages.jsonl', b''.join(extra))
        self.write('v3/segments/CORPUS_SHA256', (sha(b''.join(extra)) + '\n').encode())
        self.summary['parser_v3']['passages_sha256'] = sha(b''.join(extra))
        self.write_summary()
        parse = json.loads((self.root / 'v3/parse-report.json').read_text())
        parse['chunks'] = 5
        parse['sections']['Item 8']['chunks'] = 2
        (self.root / 'v3/parse-report.json').write_text(json.dumps(parse, indent=2) + '\n')
        self.summary['parser_v3']['artifact_file_sha256']['parse-report.json'] = sha((self.root / 'v3/parse-report.json').read_bytes())
        self.write_summary()
        with self.assertRaises(adopt.MigrationError):
            self.build(expect_new_passages=5, expect_new_total=11)
        self.assertNothingPublished()

    def test_non_contiguous_filing_records_are_rejected(self):
        lines = (self.root / 'base/data/chunks.jsonl').read_bytes().splitlines(keepends=True)
        index = next(i for i, l in enumerate(lines) if b'ms mda zero' in l)
        lines.insert(index + 1, passage(OTHER_B, 'Item 8', 9, 'interloper', 'BBB'))
        self.write('base/data/chunks.jsonl', b''.join(lines))
        base = json.loads((self.root / 'base/data/provenance.json').read_text())
        base['corpus_sha256'] = sha(b''.join(lines))
        (self.root / 'base/data/provenance.json').write_text(json.dumps(base))
        with self.assertRaisesRegex(adopt.MigrationError, 'not contiguous'):
            self.build()
        self.assertNothingPublished()

    def test_duplicate_ids_and_oversized_records_fail_closed(self):
        lines = (self.root / 'base/data/chunks.jsonl').read_bytes().splitlines(keepends=True)
        for label, mutated in (('duplicate', lines + [lines[3]]), ('oversized', lines + [b'{"ID":"finqa:big","Text":"' + b'x' * (adopt.MAX_RECORD_BYTES + 1) + b'"}\n'])):
            with self.subTest(label):
                self.write('base/data/chunks.jsonl', b''.join(mutated))
                base = json.loads((self.root / 'base/data/provenance.json').read_text())
                base['corpus_sha256'] = sha(b''.join(mutated))
                (self.root / 'base/data/provenance.json').write_text(json.dumps(base))
                with self.assertRaises(adopt.MigrationError):
                    self.build(expect_old_total=len(mutated))
                self.assertNothingPublished()

    def test_truncated_final_record_is_rejected(self):
        data = (self.root / 'base/data/chunks.jsonl').read_bytes()
        self.write('base/data/chunks.jsonl', data[:-1])
        base = json.loads((self.root / 'base/data/provenance.json').read_text())
        base['corpus_sha256'] = sha(data[:-1])
        (self.root / 'base/data/provenance.json').write_text(json.dumps(base))
        with self.assertRaisesRegex(adopt.MigrationError, 'not newline terminated'):
            self.build()
        self.assertNothingPublished()

    # --- ingestion report / lineage ---
    def test_report_updates_only_the_targeted_filing_and_retains_previous_raw_identity(self):
        self.build()
        old = json.loads((self.root / 'base/sec/report.json').read_text())
        new = json.loads((self.root / 'out/sec/report.json').read_text())
        for before, after in zip(old['filings'], new['filings']):
            if before['ref']['Accession'] != TARGET:
                self.assertEqual(before, after)
        ms = next(f for f in new['filings'] if f['ref']['Accession'] == TARGET)
        previous = next(f for f in old['filings'] if f['ref']['Accession'] == TARGET)
        self.assertEqual(ms['download']['sha256'], self.raw_sha)
        self.assertEqual(ms['raw_identity_history'][0]['sha256'], previous['download']['sha256'])
        self.assertEqual(ms['raw_identity_history'][0]['bytes'], previous['download']['bytes'])
        self.assertEqual(ms['raw_identity_history'][-1]['sha256'], self.raw_sha)
        self.assertEqual(new['parsers'], {adopt.V2_PARSER: 2, adopt.V3_PARSER: 1})
        self.assertEqual(new['parser'], 'mixed')
        self.assertEqual((new['lineage']['reparsed_filings'], new['lineage']['untouched_filings']), (1, 2))
        self.assertIn('NOT reparsed', new['lineage']['statement'])
        self.assertEqual(new['chunks'], 7)
        self.assertEqual(new['sections'], {'Item 1A': 2, 'Item 7': 3, 'Item 8': 2})
        provenance = json.loads((self.root / 'out/sec/provenance.jsonl').read_text().splitlines()[1])
        self.assertEqual(provenance['download']['sha256'], self.raw_sha)

    def test_trusted_sources_differ_from_v2_only_in_the_replaced_raw_hash(self):
        self.build()
        original = self.root / 'data/sec/provenance.json'
        old = lib.build_trusted_sources(original, self.root / 'base/sec/report.json')
        new = lib.build_trusted_sources(original, self.root / 'out/sec/report.json')
        self.assertEqual([(o['accession'], sorted(k for k in o if o[k] != n[k])) for o, n in zip(old, new) if o != n], [(TARGET, ['raw_sha256'])])
        self.assertEqual(next(n for n in new if n['accession'] == TARGET)['raw_sha256'], self.raw_sha)

    def lineage(self):
        return lib.check_report_lineage(self.root / 'out/sec/report.json', self.root)

    def rewrite_report(self, change):
        path = self.root / 'out/sec/report.json'
        report = json.loads(path.read_text())
        change(report)
        path.write_text(json.dumps(report, indent=2))

    def test_valid_lineage_passes_and_names_the_targeted_replacement(self):
        self.build()
        info, problems = self.lineage()
        self.assertEqual(problems, [])
        self.assertEqual((info['mode'], info['reparsed_filings'], info['untouched_filings']), ('targeted_replacement', 1, 2))

    def test_lineage_detects_tampering(self):
        self.build()

        def drop_history(r):
            next(f for f in r['filings'] if f['ref']['Accession'] == TARGET).pop('raw_identity_history')

        def touch_untouched(r):
            r['filings'][0]['parse']['visible_words'] += 1

        def wrong_previous_hash(r):
            r['lineage']['previous']['report_sha256'] = '0' * 64

        def forget_old_identity(r):
            next(f for f in r['filings'] if f['ref']['Accession'] == TARGET)['raw_identity_history'][0]['sha256'] = '1' * 64

        def claim_all_reparsed(r):
            r['lineage']['reparsed_filings'] = 3

        def wrong_current_hash(r):
            next(f for f in r['filings'] if f['ref']['Accession'] == TARGET)['download']['sha256'] = '2' * 64

        def unsafe_path(r):
            r['lineage']['previous']['report_path'] = '../../etc/passwd'

        cases = dict(drop_history='lacks raw identity history', touch_untouched='untouched filing', wrong_previous_hash='differs from the lineage hash',
                     forget_old_identity='does not retain the previous raw identity', claim_all_reparsed='reparsed_filings', wrong_current_hash='current raw identity is inconsistent',
                     unsafe_path='unsafe or missing')
        for name, fragment in cases.items():
            with self.subTest(name):
                self.build() if not (self.root / 'out/sec/report.json').exists() else None
                original = (self.root / 'out/sec/report.json').read_text()
                self.rewrite_report(locals()[name])
                _, problems = self.lineage()
                self.assertTrue(any(fragment in p for p in problems), problems)
                (self.root / 'out/sec/report.json').write_text(original)

    def test_undeclared_mixed_parsers_are_a_problem(self):
        self.build()
        self.rewrite_report(lambda r: r.pop('lineage'))
        _, problems = self.lineage()
        self.assertTrue(any('without a lineage declaration' in p for p in problems), problems)

    def test_single_parser_v2_report_still_verifies(self):
        info, problems = lib.check_report_lineage(self.root / 'base/sec/report.json', self.root)
        self.assertEqual((info['mode'], problems), ('single_parser', []))

    def test_verify_detects_a_tampered_unrelated_record_and_changed_queries(self):
        self.build()
        corpus = self.root / 'out/data/chunks.jsonl'
        original = corpus.read_bytes()
        corpus.write_bytes(original.replace(b'alpha risk', b'alpha RISK'))
        problems = adopt.verify_dirs(self.root / 'base/data', self.root / 'base/sec', self.root / 'out/data', self.root / 'out/sec',
                                     self.root / 'v3', self.root / 'legacy', self.root / 'raw.htm', self.options('verify'), self.root)
        self.assertTrue(any('not byte-identical' in p for p in problems), problems)
        corpus.write_bytes(original)
        (self.root / 'out/data/queries.jsonl').write_text('{"id":"q2"}\n')
        problems = adopt.verify_dirs(self.root / 'base/data', self.root / 'base/sec', self.root / 'out/data', self.root / 'out/sec',
                                     self.root / 'v3', self.root / 'legacy', self.root / 'raw.htm', self.options('verify'), self.root)
        self.assertTrue(any('queries.jsonl' in p for p in problems), problems)


@unittest.skipUnless(V3_DATA.exists() and V3_SEC.exists() and RAW.exists() and (REPLAY / 'ms-v3').exists(), 'requires the built v3 corpus and the verified MS replay')
class RealV3Artifacts(unittest.TestCase):
    """REAL ARTIFACTS: the migrated corpus, its report/provenance and the derived frozen suite."""

    def test_real_migration_verifies_with_the_documented_counts(self):
        opts = adopt.parse_args(['verify'])
        problems = adopt.verify_dirs(V2_DATA, V2_SEC, V3_DATA, V3_SEC, ROOT / opts.replacement, ROOT / opts.legacy, RAW, opts, ROOT)
        self.assertEqual(problems, [])
        provenance = json.loads((V3_DATA / 'provenance.json').read_text())
        self.assertEqual((provenance['documents'], provenance['document_counts']), (49406, dict(baseline=19511, new_sec=29895)))
        self.assertEqual(provenance['lineage']['previous_corpus_sha256'], json.loads((V2_DATA / 'provenance.json').read_text())['corpus_sha256'])
        self.assertNotEqual(provenance['corpus_sha256'], provenance['lineage']['previous_corpus_sha256'])

    def test_real_report_is_explicitly_mixed_lineage_with_ms_history(self):
        report = json.loads((V3_SEC / 'report.json').read_text())
        ms = next(f for f in report['filings'] if f['ref']['Ticker'] == 'MS')
        self.assertEqual(report['parsers'], {adopt.V2_PARSER: 147, adopt.V3_PARSER: 1})
        self.assertEqual(ms['download']['sha256'], adopt.DEFAULTS['raw_sha256'])
        self.assertEqual([h['sha256'] for h in ms['raw_identity_history']], ['57698077e7a8e943c9b7dbb7521fdb583101cc23579a6f271b9b06d5d61112df', adopt.DEFAULTS['raw_sha256']])
        self.assertEqual((ms['replacement']['unchanged'], ms['replacement']['removed'], ms['replacement']['changed']), (314, 46, [f'{TARGET}#item7#passage-000080']))
        self.assertEqual(report['lineage']['untouched_filings'], 147)

    def test_v2_artifacts_are_intact(self):
        v2 = json.loads((V2_DATA / 'provenance.json').read_text())
        self.assertEqual(lib.sha256_file(V2_DATA / 'chunks.jsonl'), v2['corpus_sha256'])
        self.assertEqual(lib.sha256_file(V2_SEC / 'report.json'), v2['new_sec_report_sha256'])
        self.assertEqual(json.loads((V2_SEC / 'report.json').read_text())['parser'], adopt.V2_PARSER)
        self.assertEqual((SUITE_V1.with_suffix('.sha256').read_text().split()[0], lib.sha256_file(SUITE_V1)), (lib.sha256_file(SUITE_V1),) * 2)

    def test_v2_suite_has_exactly_the_v1_prompts_scopes_and_expectations(self):
        v1, _ = lib.load_suite(SUITE_V1, SUITE_V1.with_suffix('.sha256').read_text().split()[0])
        v2, digest = lib.load_suite(SUITE_V2, SUITE_V2.with_suffix('.sha256').read_text().split()[0])
        self.assertEqual(v2['suite_id'], 'broad-validation-v2')
        self.assertEqual(v2['corpus_sha256'], json.loads((V3_DATA / 'provenance.json').read_text())['corpus_sha256'])
        self.assertEqual(v2['derived_from']['suite_sha256'], lib.sha256_file(SUITE_V1))
        self.assertEqual(len(v2['cases']), 30)
        for old, new in zip(v1['cases'], v2['cases']):
            for field in builder.IDENTICAL_FIELDS:
                self.assertEqual(old.get(field), new.get(field), (old['id'], field))
            self.assertEqual(old['kind'], new['kind'])
        self.assertEqual([c['expected_scopes'] for c in v1['cases'] if c['kind'] in ('negative', 'mixed')],
                         [c['expected_scopes'] for c in v2['cases'] if c['kind'] in ('negative', 'mixed')])
        self.assertNotEqual(v1['corpus_sha256'], v2['corpus_sha256'])
        self.assertEqual(digest, lib.sha256_file(SUITE_V2))

    def test_v2_suite_agent_view_exposes_only_id_and_prompt_never_anchors(self):
        suite, _ = lib.load_suite(SUITE_V2)
        for case in suite['cases']:
            self.assertEqual(set(lib.agent_view(case)), {'id', 'prompt'})
            for anchor in case.get('reviewer_evidence', {}).get('anchors', []):
                self.assertNotIn(anchor['id'], case['prompt'])
                self.assertNotIn(anchor['excerpt'][:40], case['prompt'])

    def test_derivation_refuses_overwrite_changed_availability_and_changed_anchors(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            with self.assertRaises(FileExistsError):
                builder.derive(SUITE_V1, V3_DATA / 'chunks.jsonl', ROOT / 'data/sec/provenance.json', V3_SEC / 'report.json', SUITE_V2, 'broad-validation-v9')
            with self.assertRaisesRegex(ValueError, 'needs a new suite_id'):
                builder.derive(SUITE_V1, V3_DATA / 'chunks.jsonl', ROOT / 'data/sec/provenance.json', V3_SEC / 'report.json', directory / 'x.json', 'broad-validation-v1')
            for label, mutate, fragment in (
                    ('availability', lambda s: s['cases'][12]['expected_scopes'][0].update(passages_in_scope=s['cases'][12]['expected_scopes'][0]['passages_in_scope'] + 1), 'availability on the new corpus'),
                    ('anchor', lambda s: s['cases'][12]['reviewer_evidence']['anchors'][0].update(passage_text_sha256='0' * 64), 'changed on the new corpus')):
                suite = json.loads(SUITE_V1.read_text())
                mutate(suite)
                base = directory / f'{label}.json'
                base.write_text(json.dumps(suite))
                base.with_suffix('.sha256').write_text(f'{lib.sha256_file(base)}  {base.name}\n')
                with self.subTest(label), self.assertRaisesRegex(ValueError, fragment):
                    builder.derive(base, V3_DATA / 'chunks.jsonl', ROOT / 'data/sec/provenance.json', V3_SEC / 'report.json', directory / f'out-{label}.json', 'broad-validation-v9')
                self.assertFalse((directory / f'out-{label}.json').exists())

    def test_suite_derivation_is_deterministic_against_the_real_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'again.json'
            result = builder.derive(SUITE_V1, V3_DATA / 'chunks.jsonl', ROOT / 'data/sec/provenance.json', V3_SEC / 'report.json', output, 'broad-validation-v2')
            self.assertEqual(output.read_bytes(), SUITE_V2.read_bytes())
            self.assertEqual(result['sha256'], lib.sha256_file(SUITE_V2))


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Parameterization(unittest.TestCase):
    V2_LITERALS = dict(corpus_dir='data/sec-scale-v2-evaluation-20261031', ingest_report='indexes/sec-scale-v2-20261031/report.json',
                       original_provenance='data/sec/provenance.json', vector_index='indexes/vector-sec-scale-v2-pq128-20261031',
                       sparse_index='indexes/sparse-sec-scale-v2-20261031', reranker_model='models/reranker-onnx-fp32-optimized-20261031')

    @classmethod
    def setUpClass(cls):
        cls.script = load_script('evaluate_expanded_agent', 'evaluate-expanded-agent.py')

    def test_defaults_are_exactly_the_historical_v2_paths(self):
        args = self.script.build_parser().parse_args(['--out', 'x'])
        for name, literal in self.V2_LITERALS.items():
            self.assertEqual(getattr(args, name), Path(literal), name)

    def test_every_artifact_path_can_be_redirected_to_a_new_version(self):
        v3 = ['--corpus-dir', 'data/sec-scale-v3-evaluation-20261101', '--ingest-report', 'indexes/sec-scale-v3-20261101/report.json',
              '--vector-index', 'indexes/vector-sec-scale-v3-pq128-20261101', '--sparse-index', 'indexes/sparse-sec-scale-v3-20261101']
        args = self.script.build_parser().parse_args(['--out', 'x'] + v3)
        self.assertEqual(args.corpus_dir, Path('data/sec-scale-v3-evaluation-20261101'))
        self.assertEqual(args.vector_index, Path('indexes/vector-sec-scale-v3-pq128-20261101'))
        self.assertEqual(args.sparse_index, Path('indexes/sparse-sec-scale-v3-20261101'))
        self.assertEqual(args.original_provenance, Path(self.V2_LITERALS['original_provenance']))
        self.assertEqual(args.reranker_model, Path(self.V2_LITERALS['reranker_model']))

    def test_missing_v3_inputs_fail_before_any_output_or_service_exists(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            cwd = os.getcwd()
            stack.callback(os.chdir, cwd)
            out = Path(directory) / 'run'
            with self.assertRaises(FileNotFoundError):
                self.script.main(['--out', str(out), '--corpus-dir', str(Path(directory) / 'absent')])
            self.assertFalse(out.exists())

    @unittest.skipUnless(V3_DATA.exists() and V3_SEC.exists(), 'requires the built v3 corpus')
    def test_corpus_hash_mismatch_with_wrong_report_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            cwd = os.getcwd()
            stack.callback(os.chdir, cwd)
            out = Path(directory) / 'run'
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                self.script.main(['--out', str(out), '--corpus-dir', str(V3_DATA), '--ingest-report', str(V2_SEC / 'report.json'), '--suite', str(SUITE_V2)])
            self.assertFalse(out.exists())

    @unittest.skipUnless(V3_DATA.exists() and V3_SEC.exists(), 'requires the built v3 corpus')
    def test_v1_suite_cannot_be_used_against_the_v3_corpus(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            cwd = os.getcwd()
            stack.callback(os.chdir, cwd)
            out = Path(directory) / 'run'
            with self.assertRaisesRegex(ValueError, 'different corpus'):
                self.script.main(['--out', str(out), '--corpus-dir', str(V3_DATA), '--ingest-report', str(V3_SEC / 'report.json'), '--suite', str(SUITE_V1)])
            self.assertFalse(out.exists())


class FixtureRunV3(tb.FixtureRun):
    """FIXTURE: the shared synthetic run builder, plus the lineage artifacts a real v3 run records."""

    def write(self, directory):
        directory = super().write(directory)
        hashes = json.loads((directory / 'artifact-hashes.json').read_text())
        hashes['previous_ingest_report_sha256'] = lib.sha256_file(V2_SEC / 'report.json')
        (directory / 'artifact-hashes.json').write_text(json.dumps(hashes))
        previous = self.root / 'indexes/sec-scale-v2-20261031/report.json'
        previous.parent.mkdir(parents=True, exist_ok=True)
        previous.write_bytes((V2_SEC / 'report.json').read_bytes())
        return directory


@unittest.skipUnless(V3_DATA.exists() and V3_SEC.exists() and SUITE_V2.exists(), 'requires the built v3 corpus and the derived suite')
class VerifierContractsV3(tb.VerifierContracts):
    """Runs the ENTIRE existing verifier contract suite against the v3 corpus, v3 report and broad-validation-v2.

    The inherited tests use fixture run directories assembled from real v3 passages. Passing them shows the verifier's
    citation/source/scope/bounds checks are not weakened by the mixed-lineage report.
    """

    @classmethod
    def setUpClass(cls):
        saved = {name: getattr(tb, name) for name in ('SUITE', 'CORPUS', 'REPORT', 'FixtureRun')}
        tb.SUITE, tb.CORPUS, tb.REPORT, tb.FixtureRun = SUITE_V2, V3_DATA / 'chunks.jsonl', V3_SEC / 'report.json', FixtureRunV3
        cls.addClassCleanup(lambda: [setattr(tb, k, v) for k, v in saved.items()])
        super().setUpClass()

    def test_summary_reports_the_declared_mixed_lineage(self):
        summary = self.verify()
        self.assertEqual(summary['suite_id'], 'broad-validation-v2')
        self.assertEqual(summary['ingest_lineage']['mode'], 'targeted_replacement')
        self.assertEqual(summary['ingest_lineage']['reparsed_filings'], 1)
        self.assertEqual(summary['global_problems'], [])

    def test_undeclared_mixed_report_fails_the_run(self):
        report = json.loads((V3_SEC / 'report.json').read_text())
        report.pop('lineage')
        path = Path(self.temp.name) / 'report-no-lineage.json'
        path.write_text(json.dumps(report))
        summary = verifier.verify(self.run_dir, SUITE_V2, self.pin, V3_DATA / 'chunks.jsonl', tb.ORIGINAL, path, root=self.root)
        self.assertFalse(summary['passed'])
        self.assertTrue(any('lineage declaration' in p for p in summary['global_problems']), summary['global_problems'])

    def test_altered_untouched_filing_in_the_report_fails_the_run(self):
        report = json.loads((V3_SEC / 'report.json').read_text())
        report['filings'][0]['download']['sha256'] = '3' * 64
        path = Path(self.temp.name) / 'report-altered.json'
        path.write_text(json.dumps(report))
        summary = verifier.verify(self.run_dir, SUITE_V2, self.pin, V3_DATA / 'chunks.jsonl', tb.ORIGINAL, path, root=self.root)
        self.assertFalse(summary['passed'])
        self.assertTrue(any('untouched filing' in p for p in summary['global_problems']), summary['global_problems'])

    def test_recorded_previous_report_hash_must_match_lineage(self):
        hashes_path = self.run_dir / 'artifact-hashes.json'
        hashes = json.loads(hashes_path.read_text())
        hashes['previous_ingest_report_sha256'] = '4' * 64
        hashes_path.write_text(json.dumps(hashes))
        summary = self.verify()
        self.assertFalse(summary['passed'])
        self.assertTrue(any('previous_ingest_report_sha256' in p for p in summary['global_problems']), summary['global_problems'])

    def test_a_v1_suite_run_is_rejected_against_the_v3_corpus(self):
        summary = verifier.verify(self.run_dir, SUITE_V1, SUITE_V1.with_suffix('.sha256').read_text().split()[0], V3_DATA / 'chunks.jsonl', tb.ORIGINAL, V3_SEC / 'report.json', root=self.root)
        self.assertFalse(summary['passed'])
        self.assertTrue(any('different corpus' in p or 'differs' in p or 'different' in p for p in summary['global_problems']), summary['global_problems'])


class SuitePublish(unittest.TestCase):
    """SYNTHETIC FIXTURE suites: only the exclusive-create / orphan-pin / cleanup contract of `publish_suite`. No corpus is read."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.out = self.dir / 'suite.json'
        self.pin = self.dir / 'suite.sha256'
        self.suite = dict(suite_id='fixture', cases=[1, 2, 3])

    def test_writes_suite_and_matching_pin(self):
        digest = builder.publish_suite(self.out, self.suite)
        self.assertEqual(lib.sha256_file(self.out), digest)
        self.assertEqual(self.pin.read_text(), f'{digest}  suite.json\n')

    def test_existing_suite_blocks_and_nothing_changes(self):
        self.out.write_text('frozen')
        with self.assertRaises(FileExistsError):
            builder.publish_suite(self.out, self.suite)
        self.assertEqual(self.out.read_text(), 'frozen')
        self.assertFalse(self.pin.exists())

    def test_orphan_pin_alone_blocks_and_is_not_overwritten(self):
        self.pin.write_text('orphan pin of some other frozen suite\n')
        with self.assertRaises(FileExistsError):
            builder.publish_suite(self.out, self.suite)
        self.assertEqual(self.pin.read_text(), 'orphan pin of some other frozen suite\n')
        self.assertFalse(self.out.exists())

    def test_dangling_symlink_pin_also_blocks(self):
        self.pin.symlink_to(self.dir / 'nowhere')
        with self.assertRaises(FileExistsError):
            builder.publish_suite(self.out, self.suite)
        self.assertFalse(self.out.exists())
        self.assertTrue(self.pin.is_symlink())

    def test_failure_after_the_suite_is_written_removes_only_files_this_call_created(self):
        calls = []
        real = os.fsync

        def failing(fd):
            calls.append(fd)
            if len(calls) == 2:  # the pin write
                raise OSError('simulated disk failure')
            return real(fd)

        unrelated = self.dir / 'other.json'
        unrelated.write_text('keep')
        builder.os.fsync = failing
        try:
            with self.assertRaises(OSError):
                builder.publish_suite(self.out, self.suite)
        finally:
            builder.os.fsync = real
        self.assertFalse(self.out.exists() or self.pin.exists())
        self.assertEqual(unrelated.read_text(), 'keep')

    def test_race_created_pin_is_never_deleted_by_cleanup(self):
        real_open = Path.open
        pin = self.pin

        def racing(path, mode='r', *a, **k):
            if path == pin and mode == 'xb':
                pin.write_text('someone else won the race')
            return real_open(path, mode, *a, **k)

        Path.open = racing
        try:
            with self.assertRaises(FileExistsError):
                builder.publish_suite(self.out, self.suite)
        finally:
            Path.open = real_open
        self.assertEqual(pin.read_text(), 'someone else won the race')
        self.assertFalse(self.out.exists())  # the suite this call created is removed

    @unittest.skipUnless(SUITE_V2.exists(), 'requires the frozen v2 suite')
    def test_derive_refuses_existing_outputs_before_reading_anything_and_frozen_bytes_are_unchanged(self):
        before = {p: lib.sha256_file(p) for p in (SUITE_V1, SUITE_V1.with_suffix('.sha256'), SUITE_V2, SUITE_V2.with_suffix('.sha256'))}
        missing = self.dir / 'absent'  # derive must fail on the output check before touching any input
        for output in (SUITE_V2, ):
            with self.assertRaises(FileExistsError):
                builder.derive(SUITE_V1, missing / 'c.jsonl', missing / 'o.json', missing / 'r.json', output, 'broad-validation-v9')
        orphan = self.dir / 'orphan.json'
        orphan.with_suffix('.sha256').write_text('orphan\n')
        with self.assertRaises(FileExistsError):
            builder.derive(SUITE_V1, missing / 'c.jsonl', missing / 'o.json', missing / 'r.json', orphan, 'broad-validation-v9')
        self.assertFalse(orphan.exists())
        self.assertEqual(orphan.with_suffix('.sha256').read_text(), 'orphan\n')
        self.assertEqual({p: lib.sha256_file(p) for p in before}, before)
        self.assertEqual(SUITE_V2.with_suffix('.sha256').read_text().split()[0], lib.sha256_file(SUITE_V2))


class GateArithmetic(unittest.TestCase):
    """SYNTHETIC FIXTURE METRICS: only exercise the pass/fail arithmetic of the pre-registered thresholds."""

    def evaluation(self, ndcg=gates.BASELINE_NDCG, recall=0.838, reduction=91.1, queries=100):
        return dict(ndcg_at_10=dict(reranked=ndcg), pq_recall_at_10_vs_exact_float32=recall, evaluated_queries=queries,
                    vector_memory=dict(exact_native_payload_reduction_percent=reduction))

    def load(self, hybrid_p99=40.0, failures=0, requests=200):
        section = lambda p99, f: dict(requests=requests, workers=50, successes=requests - f, failures=f, statuses={'200': requests - f},  # noqa: E731
                                      all_attempt_latency=dict(p99_ns=int(p99 * 1e9) if math.isfinite(p99) else p99))
        return dict(hybrid=section(hybrid_p99, failures), dense=section(0.9, 0), query_sha256='q', warmup_per_endpoint=5, load_model='closed')

    def params(self, **change):
        base = dict(inference_workers=4, threads_per_job=2, configured_intraop_thread_budget=8, load_workers=50, requests_per_endpoint=200, queries=100,
                    candidate_limit=50, max_tokens=512, reranker_backend='onnx-fp32', rerank_batch_size=1, dense_batch_size=8, dense_batch_wait_ms=2.0, dense_batch_trigger=8)
        return dict(base, **change)

    def verdict(self, section, name):
        return next(g['passed'] for g in section if g['name'] == name)

    def quality(self, **kw):
        ids = [f'q{i}' for i in range(100)]
        return gates.quality_gates(self.evaluation(), self.evaluation(**kw), ids, ids, gates.BASELINE_NDCG, 0.838)

    def test_ndcg_boundary_is_inclusive_at_exactly_0_005(self):
        self.assertTrue(self.verdict(self.quality(ndcg=gates.BASELINE_NDCG - 0.005), 'hybrid_rerank_ndcg_drop_within_0.005'))
        self.assertFalse(self.verdict(self.quality(ndcg=gates.BASELINE_NDCG - 0.0051), 'hybrid_rerank_ndcg_drop_within_0.005'))
        self.assertTrue(self.verdict(self.quality(ndcg=gates.BASELINE_NDCG + 0.1), 'hybrid_rerank_ndcg_drop_within_0.005'))

    def test_recall_and_payload_thresholds(self):
        self.assertTrue(self.verdict(self.quality(recall=0.818), 'pq_recall_at_10_drop_within_0.02'))
        self.assertFalse(self.verdict(self.quality(recall=0.817), 'pq_recall_at_10_drop_within_0.02'))
        self.assertFalse(self.verdict(self.quality(reduction=89.9), 'payload_reduction_at_least_90_percent'))
        self.assertTrue(self.verdict(self.quality(reduction=90.0), 'payload_reduction_at_least_90_percent'))

    def test_different_query_sets_or_a_changed_baseline_fail(self):
        ids = [f'q{i}' for i in range(100)]
        swapped = ids[:-1] + ['other']
        self.assertFalse(self.verdict(gates.quality_gates(self.evaluation(), self.evaluation(), ids, swapped, gates.BASELINE_NDCG, 0.838), 'same_100_development_queries'))
        self.assertFalse(self.verdict(gates.quality_gates(self.evaluation(ndcg=0.5), self.evaluation(), ids, ids, gates.BASELINE_NDCG, 0.838), 'baseline_matches_stated_v2_values'))

    def test_hybrid_p99_regression_boundary_and_failures(self):
        ok = gates.load_gates(self.load(40.0), self.load(44.0), self.params(), self.params())
        self.assertTrue(self.verdict(ok, 'hybrid_p99_regression_within_10_percent'))
        slow = gates.load_gates(self.load(40.0), self.load(44.1), self.params(), self.params())
        self.assertFalse(self.verdict(slow, 'hybrid_p99_regression_within_10_percent'))
        failed = gates.load_gates(self.load(40.0), self.load(40.0, failures=1), self.params(), self.params())
        self.assertFalse(self.verdict(failed, 'candidate_hybrid_zero_failures'))

    def test_unmatched_configuration_cannot_pass(self):
        mismatched = gates.load_gates(self.load(), self.load(), self.params(), self.params(inference_workers=8))
        self.assertFalse(self.verdict(mismatched, 'matched_configuration'))
        other_queries = dict(self.load(), query_sha256='different')
        self.assertFalse(self.verdict(gates.load_gates(self.load(), other_queries, self.params(), self.params()), 'matched_configuration'))

    def test_baseline_with_failures_is_not_a_valid_control(self):
        for endpoint_change in ('hybrid', 'dense'):
            with self.subTest(endpoint_change):
                bad = self.load()
                bad[endpoint_change].update(failures=1, successes=199, statuses={'200': 199, '503': 1})
                result = gates.load_gates(bad, self.load(), self.params(), self.params())
                self.assertFalse(self.verdict(result, f'baseline_{endpoint_change}_zero_failures'))
                self.assertTrue(self.verdict(result, f'candidate_{endpoint_change}_zero_failures'))

    def test_nonfinite_values_fail_closed(self):
        ids = [f'q{i}' for i in range(100)]
        for bad in (float('nan'), float('inf'), float('-inf'), True, '0.5', None):
            with self.subTest(ndcg=bad), self.assertRaises(gates.GateInputError):
                gates.quality_gates(self.evaluation(), self.evaluation(ndcg=bad), ids, ids, gates.BASELINE_NDCG, 0.838)
        for bad in (float('nan'), float('inf')):
            with self.subTest(p99=bad), self.assertRaises(gates.GateInputError):
                gates.load_gates(self.load(), self.load(hybrid_p99=bad), self.params(), self.params())
        with self.assertRaises(gates.GateInputError):
            gates.load_gates(self.load(hybrid_p99=0), self.load(), self.params(), self.params())

    def test_json_readers_reject_nan_infinity_and_overflow_literals(self):
        with tempfile.TemporaryDirectory() as directory:
            for text in ('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}', '{"x": 1e999}', '{"a": [1, {"b": -1e999}]}'):
                path = Path(directory) / 'r.json'
                path.write_text(text)
                with self.subTest(text), self.assertRaises(gates.GateInputError):
                    gates.read(path)
            path.write_text('{"x": 1.5}')
            self.assertEqual(gates.read(path), {'x': 1.5})

    def test_cli_exits_2_and_writes_nothing_for_nonfinite_input_and_never_emits_nan(self):
        with tempfile.TemporaryDirectory() as directory:
            for run in ('base', 'cand'):
                root = Path(directory) / run
                (root / 'evaluation').mkdir(parents=True)
                (root / 'load').mkdir()
                (root / 'evaluation/per-query.jsonl').write_text(''.join(json.dumps(dict(id=f'q{i}')) + '\n' for i in range(100)))
                (root / 'load/report.json').write_text(json.dumps(self.load()))
                (root / 'parameters.json').write_text(json.dumps(self.params()))
                (root / 'evaluation/report.json').write_text(json.dumps(self.evaluation()))
            (Path(directory) / 'cand/evaluation/report.json').write_text(json.dumps(self.evaluation()).replace('91.1', 'NaN'))
            out = Path(directory) / 'gates.json'
            self.assertEqual(gates.main(['--baseline-run', str(Path(directory) / 'base'), '--candidate-run', str(Path(directory) / 'cand'), '--output', str(out)]), 2)
            self.assertFalse(out.exists())
            (Path(directory) / 'cand/evaluation/report.json').write_text(json.dumps(self.evaluation()))
            self.assertEqual(gates.main(['--baseline-run', str(Path(directory) / 'base'), '--candidate-run', str(Path(directory) / 'cand'), '--output', str(out)]), 1)
            json.loads(out.read_text(), parse_constant=lambda c: self.fail(c))  # strict JSON, no NaN/Infinity

    def test_missing_agent_verification_is_incomplete_not_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            for run in ('base', 'cand'):
                root = Path(directory) / run
                (root / 'evaluation').mkdir(parents=True)
                (root / 'load').mkdir()
                (root / 'evaluation/report.json').write_text(json.dumps(self.evaluation()))
                (root / 'evaluation/per-query.jsonl').write_text(''.join(json.dumps(dict(id=f'q{i}')) + '\n' for i in range(100)))
                (root / 'load/report.json').write_text(json.dumps(self.load()))
                (root / 'parameters.json').write_text(json.dumps(self.params()))
            result = gates.compare(Path(directory) / 'base', Path(directory) / 'cand')
            self.assertEqual(result['sections_not_evaluated'], ['agent'])
            self.assertFalse(result['passed'])
            self.assertTrue(all(result['section_passed'].values()))

    def agent(self, **change):
        summary = dict(suite_id='broad-validation-v2', corpus_sha256='c', full_suite=True, frozen_case_count=30, verified=30, run_failed=0, verification_failed=0,
                       omitted=[], failures_by_category=dict(transport=0, model=0, structural=0), passed=True, global_problems=[], evaluator_summary_discrepancies=[],
                       release_gates=dict(unavailable_scopes_publish_zero_claims=True, mixed_cases_preserve_unavailable_and_fully_support_available=True,
                                          citation_coverage_of_available_subqueries=dict(met=True, threshold=0.8, ratio=0.9), all_checked_gates_met=True), mixed_cases={})
        summary.update(change)
        return summary

    def test_agent_gates_require_everything(self):
        self.assertTrue(all(g['passed'] for g in gates.agent_gates(self.agent(), 'c', 'broad-validation-v2')))
        for change in (dict(corpus_sha256='other'), dict(verified=29), dict(failures_by_category=dict(transport=1, model=0, structural=0)), dict(global_problems=['x']),
                       dict(suite_id='broad-validation-v1')):
            with self.subTest(change=list(change)):
                self.assertFalse(all(g['passed'] for g in gates.agent_gates(self.agent(**change), 'c', 'broad-validation-v2')))
        broken = self.agent()
        broken['release_gates'] = dict(broken['release_gates'], unavailable_scopes_publish_zero_claims=False, all_checked_gates_met=False)
        failing = [g['name'] for g in gates.agent_gates(broken, 'c', 'broad-validation-v2') if not g['passed']]
        self.assertEqual(failing, ['unavailable_scopes_withheld', 'verifier_release_gates_all_met'])


if __name__ == '__main__':
    unittest.main()
