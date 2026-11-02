"""Result projection: citation/evidence binding, SEC link trust, size bound. Fixtures are synthetic TEST DATA."""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_web import limits
from audit_web.errors import ProjectionError, ResultTooLarge
from audit_web.projection import is_sec_archive_url, project_result, quote_span
from audit_web_testkit import QUOTE_A, TEXT_A, URL, agent_result, citation, evidence, hop


class SourceUrlTrust(unittest.TestCase):
    def test_only_real_sec_archive_https_urls(self):
        self.assertTrue(is_sec_archive_url(URL))
        for bad in ['http://www.sec.gov/Archives/edgar/data/1/2/x.htm', 'https://sec.gov/Archives/edgar/data/1/2/x.htm', 'https://www.sec.gov.evil.com/Archives/x/y/z.htm',
                    'https://evil.com/www.sec.gov/Archives/x/y/z.htm', 'https://user:pw@www.sec.gov/Archives/edgar/data/1/2/x.htm', 'https://www.sec.gov:444/Archives/edgar/data/1/2/x.htm',
                    'https://www.sec.gov/Archives/edgar/data/1/2/x.htm?next=//evil', 'https://www.sec.gov/Archives/edgar/data/1/2/x.htm#frag', 'https://www.sec.gov/other/edgar/x.htm',
                    'https://www.sec.gov/Archives/../admin/x.htm', 'javascript:alert(1)', 'data:text/html,<script>', 'https://www.sec.gov/Archives/edgar/data/1/2/x.htm" onclick="x',
                    'https://www.sec.gov\\@evil.com/Archives/edgar/data/1/2/x.htm', ' https://www.sec.gov/Archives/edgar/data/1/2/x.htm', '', None, 5]:
            self.assertFalse(is_sec_archive_url(bad), bad)

    def test_untrusted_link_in_evidence_fails_closed(self):
        bad = evidence(1, TEXT_A).model_copy(update={'url': 'https://evil.example/Archives/x'})
        with self.assertRaises(ProjectionError):
            project_result(agent_result(hops=[hop(0, 0, [bad])], citations=[]))


class Binding(unittest.TestCase):
    def test_excerpts_bind_to_evidence_in_the_same_result(self):
        document = json.loads(project_result(agent_result()))
        evidence_ids = {row['id'] for row in document['evidence']}
        self.assertEqual(len(document['excerpts']), 2)
        for excerpt in document['excerpts']:
            self.assertIn(excerpt['chunk_id'], evidence_ids)
            row = next(r for r in document['evidence'] if r['id'] == excerpt['chunk_id'])
            start, end = excerpt['quote_span']
            self.assertEqual(' '.join(row['text'][start:end].split()), excerpt['quote'])  # highlight range == the quoted words
            self.assertEqual((excerpt['accession'], excerpt['url'], excerpt['tag']), (row['accession'], row['url'], row['tag']))
            self.assertEqual((excerpt['company'], excerpt['filing_year'], excerpt['form']), ('TST', 2024, '10-K'))

    def test_citation_to_unknown_or_unaccepted_evidence_is_rejected(self):
        stranger = evidence(9, 'A passage that no hop ever retrieved for this fixture result, for contract testing only.')
        with self.assertRaises(ProjectionError):
            project_result(agent_result(citations=[citation(stranger, 'A passage that no hop ever retrieved for this fixture')]))
        a = evidence(1, TEXT_A)
        with self.assertRaises(ProjectionError):  # retrieved but the hop was not accepted
            project_result(agent_result(hops=[hop(0, 0, [a], accepted=False, score=2)], citations=[citation(a, QUOTE_A)]))

    def test_citation_identity_mismatch_and_non_verbatim_quote_are_rejected(self):
        a = evidence(1, TEXT_A)
        wrong = citation(a, QUOTE_A).model_copy(update={'accession': '0000000000-00-000008'})
        with self.assertRaises(ProjectionError):
            project_result(agent_result(hops=[hop(0, 0, [a])], citations=[wrong]))
        with self.assertRaises(ProjectionError):
            project_result(agent_result(hops=[hop(0, 0, [a])], citations=[citation(a, 'a sentence that does not occur in the passage text anywhere at all')]))

    def test_same_passage_in_many_hops_is_stored_once_and_origin_is_recorded(self):
        a = evidence(1, TEXT_A)
        result = agent_result(hops=[hop(0, 0, [a], accepted=False, score=2), hop(0, 1, [a], retained=[a.id])], citations=[citation(a, QUOTE_A)])
        document = json.loads(project_result(result))
        self.assertEqual(len(document['evidence']), 1)
        self.assertEqual([h['context'] for h in document['hops']], [[dict(id=a.id, origin='fresh')], [dict(id=a.id, origin='carried')]])

    def test_conflicting_content_for_one_passage_id_is_rejected(self):
        with self.assertRaises(ProjectionError):
            project_result(agent_result(hops=[hop(0, 0, [evidence(1, TEXT_A)]), hop(1, 0, [evidence(1, TEXT_A + ' altered')])], citations=[]))

    def test_no_evidence_result_keeps_real_flags_and_invents_nothing(self):
        flags = ['Subquery 0 has no evidence meeting threshold after 2 retries.', 'No verified answer; evidence unavailable or below threshold.']
        document = json.loads(project_result(agent_result(hops=[hop(0, 0, [], accepted=False, score=1)], citations=[], flags=flags)))
        self.assertEqual((document['excerpts'], document['evidence']), ([], []))
        self.assertEqual([g['text'] for g in document['gaps']], flags)
        self.assertEqual([g['subquery'] for g in document['gaps']], [0, None])
        self.assertFalse(any('markdown' in key for key in document))  # model-authored Markdown is never forwarded

    def test_hostile_strings_pass_through_as_inert_data(self):
        payload = '<img src=x onerror=alert(1)> [x](javascript:alert(1)) </script><script>alert(2)</script>'
        a = evidence(1, 'Prefix ' + payload + ' end of the fixture passage, long enough for a quote.')
        hostile = hop(0, 0, [a]).model_copy(update={'rationale': payload})
        document = json.loads(project_result(agent_result(hops=[hostile], citations=[citation(a, payload + ' end of the fixture passage, long')], flags=[payload])))
        self.assertEqual(document['hops'][0]['rationale'], payload)
        self.assertEqual(document['gaps'][0]['text'], payload)
        self.assertEqual(document['excerpts'][0]['quote'], payload + ' end of the fixture passage, long')

    def test_result_size_bound_raises_explicitly(self):
        hops = [hop(i % 3, i // 3, [evidence(1000 + i, 'filler ' * 4000)]) for i in range(60)]  # 60 distinct ~28 KB passages > 1 MiB
        with self.assertRaises(ResultTooLarge):
            project_result(agent_result(hops=hops, citations=[]))
        self.assertGreater(limits.MAX_RESULT_BYTES, 100_000)


class QuoteSpans(unittest.TestCase):
    def test_utf16_offsets_for_astral_characters_and_irregular_whitespace(self):
        text = '😀😀 Revenue\tgrew\n\n  strongly in the period 😀'
        start, end = quote_span(text, 'Revenue grew strongly in the period')
        units = text.encode('utf-16-le')
        self.assertEqual(units[start * 2:end * 2].decode('utf-16-le'), 'Revenue\tgrew\n\n  strongly in the period')
        self.assertEqual(start, 5)  # two surrogate pairs + one space
        self.assertIsNone(quote_span(text, 'not present'))


if __name__ == '__main__':
    unittest.main()
