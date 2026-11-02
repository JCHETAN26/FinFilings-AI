"""Critic context contracts. Tiny TEST-ONLY fixtures and a recording stub; these do not measure model quality."""
import asyncio
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent.critic import critique, review_view
from agent.filters import FilingFilter
from agent.models import Critique, Evidence, SubQuery


def ev(acc, number, section='Item 7', year=2024, ticker='T', text=None):
    ident = f'{acc}#{section.lower().replace(" ", "")}#passage-{number:06d}'
    return Evidence(id=ident, text=text or f'fixture text {number}', metadata=dict(Ticker=ticker, FilingYear=year, SectionItem=section),
                    accession=acc, url='https://www.sec.gov/x', tag=f'tag-{number}')


class Recorder:
    def __init__(self, retain=()):
        self.calls, self.retain = [], list(retain)

    async def generate(self, schema, instruction, payload):
        self.calls.append((instruction, payload))
        return Critique(score=4, rationale='fixture', rewritten_query='fixture query', retain_ids=self.retain)


class ReviewView(unittest.TestCase):
    def test_orders_within_scope_and_marks_only_consecutive(self):
        items = [ev('A', 36), ev('A', 35), ev('A', 42), ev('B', 10), ev('A', 9, section='Item 1A')]
        ordered, pairs = review_view(items)
        self.assertEqual([i.id.split('passage-')[1] for i in ordered], ['000035', '000036', '000042', '000010', '000009'])
        self.assertEqual(pairs, [dict(before=items[1].id, after=items[0].id)])

    def test_never_pairs_across_filing_section_year_or_ticker(self):
        items = [ev('A', 5), ev('B', 6), ev('A', 6, section='Item 8'), ev('A', 7, year=2025), ev('A', 8, ticker='U')]
        self.assertEqual(review_view(items)[1], [])

    def test_non_passage_ids_stay_after_ordered_group_and_empty_input(self):
        odd = ev('A', 1).model_copy(update=dict(id='free-form-id'))
        items = [ev('A', 3), odd, ev('A', 2)]
        ordered, pairs = review_view(items)
        self.assertEqual([i.id for i in ordered], [items[2].id, items[0].id, 'free-form-id'])
        self.assertEqual(pairs, [dict(before=items[2].id, after=items[0].id)])
        self.assertEqual(review_view([]), ([], []))

    def test_duplicate_id_never_pairs_with_itself_and_nothing_is_dropped(self):
        a = ev('A', 5)
        ordered, pairs = review_view([a, a, ev('A', 6)])
        self.assertEqual(len(ordered), 3)
        self.assertIn(dict(before=a.id, after=ev('A', 6).id), pairs)
        self.assertNotIn(dict(before=a.id, after=a.id), pairs)

    def test_gaps_and_malformed_passage_suffixes_are_not_adjacent(self):
        gap = [ev('A', 35), ev('A', 37)]
        self.assertEqual(review_view(gap)[1], [])
        for suffix in ('12a', 'x', '', '-3'):
            bad = ev('A', 12).model_copy(update=dict(id=f'A#item7#passage-{suffix}'))
            ordered, pairs = review_view([ev('A', 11), bad])
            self.assertEqual(pairs, [])
            self.assertEqual([i.id for i in ordered], [ev('A', 11).id, bad.id])

    def test_text_unchanged_and_no_merge(self):
        items = [ev('A', 2, text='and settled $150 million.'), ev('A', 1, text='we repurchased')]
        ordered, _ = review_view(items)
        self.assertEqual({i.id: i.text for i in ordered}, {i.id: i.text for i in items})
        self.assertEqual(len(ordered), 2)


class CritiquePayload(unittest.IsolatedAsyncioTestCase):
    async def run_critique(self, evidence, retain=()):
        model = Recorder(retain)
        sub = SubQuery(query='share repurchase activity', filters=FilingFilter(ticker='T', year=2024))
        result = await critique(model, sub, sub.query, evidence, 'prompt', [])
        return model.calls[0], result

    async def test_payload_has_document_order_and_adjacent_pairs(self):
        a, b = ev('A', 36), ev('A', 35)
        (instruction, payload), _ = await self.run_critique([a, b])
        self.assertEqual([e['id'] for e in payload['evidence']], [b.id, a.id])
        self.assertEqual(payload['adjacent_pairs'], [dict(before=b.id, after=a.id)])

    async def test_empty_evidence_with_accepting_score_is_rejected(self):
        sub = SubQuery(query='share repurchase activity', filters=FilingFilter(ticker='T', year=2024))
        with self.assertRaises(ValueError):  # recording double returns score 4
            await critique(Recorder(), sub, sub.query, [], 'prompt', [])

    async def test_retention_validation_unchanged(self):
        a, b = ev('A', 1), ev('A', 2)
        _, result = await self.run_critique([a, b], retain=[a.id, b.id])
        self.assertEqual(result.retain_ids, [a.id, b.id])
        for bad in ([a.id, a.id], ['ghost']):
            with self.assertRaises(ValueError):
                await self.run_critique([a, b], retain=bad)

    async def test_stub_gets_no_added_calls(self):
        model = Recorder()
        sub = SubQuery(query='share repurchase activity', filters=FilingFilter(ticker='T', year=2024))
        await critique(model, sub, sub.query, [ev('A', 1)], 'p', [])
        self.assertEqual(len(model.calls), 1)


if __name__ == '__main__':
    unittest.main()
