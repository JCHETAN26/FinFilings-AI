"""Opaque-handle synthesis contracts. Evidence and the model are scripted TEST DOUBLES (no model-quality claim)."""
import asyncio
import json
import unittest
from agent.models import Draft, Evidence, HandleClaim, HandleDraft, QuoteClaim
from agent.synthesizer import MAX_HANDLES, bind_handles, ground, ground_handles, synthesize

ACC_A, ACC_B = '000000248826000018', '000000248826000016'
SHARED = 'Revenue recognition requires significant judgment about performance obligations in contracts.'


def item(acc, number, text):
    ident = f'{acc}#item1A#passage-{number:06}'
    return Evidence(id=ident, text=text, metadata={'ID': ident, 'Ticker': 'TST', 'FilingYear': 2026, 'FormType': '10-K', 'SectionItem': 'Item 1A'},
                    accession=f'{acc[:10]}-{acc[10:12]}-{acc[12:]}', url=f'https://www.sec.gov/Archives/edgar/data/1/{acc}/x.htm', tag=f'TST-2026-10K-item1A-p{number}')


class Model:
    def __init__(self, claims):
        self.claims, self.payloads = claims, []

    async def generate(self, schema, instruction, payload):
        self.payloads.append((schema, instruction, json.dumps(payload)))
        return schema(claims=[HandleClaim(ref=r, quote=q) for r, q in self.claims])


class Handles(unittest.TestCase):
    def setUp(self):
        self.a = item(ACC_A, 18, 'Alpha unique sentence about AMD supply risk in foundry capacity expansion plans.  ' + SHARED)
        self.b = item(ACC_B, 16, 'Beta unique sentence regarding export restrictions affecting product shipments abroad. ' + SHARED)

    def test_handles_are_deterministic_short_and_resolve_by_exact_lookup(self):
        handles = bind_handles([self.a, self.b])
        self.assertEqual(list(handles), ['E01', 'E02'])
        self.assertIs(handles['E01'], self.a)
        self.assertEqual(list(bind_handles([self.a, self.b])), list(handles))

    def test_model_payload_carries_no_canonical_identifiers(self):
        model = Model([('E01', 'Alpha unique sentence about AMD supply risk in foundry capacity expansion plans.')])
        asyncio.run(synthesize(model, 'audit prompt', [self.a, self.b]))
        sent = model.payloads[0][2]
        for secret in (ACC_A, ACC_B, self.a.id, self.a.tag, self.a.url, self.a.accession, 'passage-'):
            self.assertNotIn(secret, sent)
        self.assertIn('E01', sent); self.assertIn('E02', sent)

    def test_published_citation_uses_canonical_source_not_model_text(self):
        model = Model([('E02', 'Beta unique sentence regarding export restrictions affecting product shipments abroad.')])
        _, citations, flags = asyncio.run(synthesize(model, 'p', [self.a, self.b]))
        self.assertEqual(len(model.payloads), 1)
        self.assertEqual((citations[0].chunk_id, citations[0].accession, citations[0].tag, citations[0].url), (self.b.id, self.b.accession, self.b.tag, self.b.url))
        self.assertEqual(flags, [])

    def test_forged_unknown_and_near_miss_handles_are_rejected_without_fuzzy_matching(self):
        quote = 'Alpha unique sentence about AMD supply risk in foundry capacity expansion plans.'
        forged = [self.a.id, f'{ACC_A[:-2]}17#item1A#passage-000017', 'e01', 'E1', 'E001', ' E01', 'E03', 'E01\n']
        draft = HandleDraft(claims=[HandleClaim(ref=r, quote=quote) for r in forged])
        citations, flags = ground_handles(draft, bind_handles([self.a, self.b]))
        self.assertEqual(citations, [])
        self.assertEqual(len(flags), len(forged))
        self.assertTrue(all('unknown evidence handle' in f for f in flags))
        self.assertTrue(all(len(f) < 90 for f in flags))  # bounded; rejected quote text is not stored

    def test_quote_from_another_handle_or_altered_quote_is_rejected(self):
        handles = bind_handles([self.a, self.b])
        beta = 'Beta unique sentence regarding export restrictions affecting product shipments abroad.'
        citations, flags = ground_handles(HandleDraft(claims=[
            HandleClaim(ref='E01', quote=beta),  # real text, wrong passage
            HandleClaim(ref='E02', quote=beta.replace('export', 'import')),  # altered
            HandleClaim(ref='E02', quote=beta[:60] + ' 9999 ' + beta[60:])]), handles)
        self.assertEqual(citations, [])
        self.assertEqual(len(flags), 3)
        self.assertNotIn(beta[:30], ' '.join(flags))

    def test_identical_text_in_two_passages_cannot_bypass_binding(self):
        handles = bind_handles([self.a, self.b])
        draft = HandleDraft(claims=[HandleClaim(ref='E01', quote=SHARED), HandleClaim(ref='E02', quote=SHARED), HandleClaim(ref='E02', quote=SHARED)])
        citations, _ = ground_handles(draft, handles)
        self.assertEqual([c.chunk_id for c in citations], [self.a.id, self.b.id])  # per-handle, deduplicated per (passage, quote)

    def test_whitespace_normalization_is_unchanged(self):
        spaced = 'Alpha unique sentence about AMD supply risk   in foundry\ncapacity expansion plans.'
        citations, _ = ground_handles(HandleDraft(claims=[HandleClaim(ref='E01', quote=spaced)]), bind_handles([self.a]))
        self.assertEqual(len(citations), 1)

    def test_bounds(self):
        many = [item(ACC_A, n, f'unique passage number {n} with enough words to be distinct text') for n in range(MAX_HANDLES + 1)]
        self.assertEqual(len(bind_handles(many[:MAX_HANDLES])), MAX_HANDLES)
        with self.assertRaises(ValueError):
            bind_handles(many)
        with self.assertRaises(ValueError):
            bind_handles([self.a, self.a])

    def test_legacy_ground_api_remains_strict(self):
        quote = 'Alpha unique sentence about AMD supply risk in foundry capacity expansion plans.'
        citations, flags = ground(Draft(claims=[QuoteClaim(chunk_id=self.a.id, quote=quote),
            QuoteClaim(chunk_id=self.a.id[:-1] + '9', quote=quote), QuoteClaim(chunk_id=self.a.id, quote=quote.replace('AMD', 'XYZ'))]), [self.a])
        self.assertEqual(len(citations), 1); self.assertEqual(len(flags), 2)


if __name__ == '__main__':
    unittest.main()
