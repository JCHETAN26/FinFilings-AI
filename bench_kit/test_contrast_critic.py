"""Contrast-diagnostic plumbing contracts with a scripted critic. TEST DOUBLES only; real grades come from the live diagnostic."""
import asyncio
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contrast_critic as cc  # noqa: E402
from agent.models import Critique  # noqa: E402

ACC = '000000000000000001'
PROMPT = 'Audit ACME filing year 2024: retrieve liquidity disclosures and operating lease obligations. Present excerpts.'
FILTERS = dict(ticker='ACME', year=2024, section='', accession=ACC)


def evidence(n, text='fixture'):
    ident = f'{ACC}#passage-{n:06}'
    return dict(id=ident, text=text, metadata={'ID': ident}, accession='a', url='https://www.sec.gov/x', tag=f't{n}')


class Registry:
    def verify(self, hit, filters):
        from agent.models import Evidence
        return Evidence(**evidence(int(hit['id'][-6:]), hit['text']))

    def catalog(self):
        return []


class Scripted:
    def __init__(self, scores):
        self.scores, self.payloads, self.calls = list(scores), [], []

    async def generate(self, schema, instruction, payload):
        self.payloads.append(payload)
        self.calls.append(dict(schema='Critique'))
        return Critique(score=self.scores.pop(0), rationale='fixture', rewritten_query='fixture rewrite', retain_ids=[])


def saved():
    hop = lambda attempt: dict(subquery=0, attempt=attempt, query=f'expanded phrase {attempt}', filters=FILTERS, score=3, rationale='r',
                               evidence=[evidence(1, 'aggregate text'), evidence(2, 'more text')], accepted=False, retained_evidence_ids=[], elapsed_seconds=0.0)
    return dict(prompt=PROMPT, plan=dict(subqueries=[dict(query='lease obligations maturities schedule', filters=FILTERS, goal='operating lease obligations')]), hops=[hop(0), hop(1)])


class Contrast(unittest.TestCase):
    def run_it(self, scores, **kw):
        model = Scripted(scores)
        args = dict(subquery=0, hop_index=-1, goal_a='operating lease obligations', goal_b='year-by-year operating lease maturity schedule')
        args.update(kw)
        return model, asyncio.run(cc.run_contrast(model, Registry(), saved(), **args))

    def test_same_evidence_and_phrase_with_two_goals_sequentially(self):
        model, out = self.run_it([4, 3])
        self.assertEqual(len(model.payloads), 2)
        a, b = model.payloads
        self.assertEqual((a['search_query'], a['initial_search_query']), (b['search_query'], b['initial_search_query']))
        self.assertEqual(a['search_query'], 'expanded phrase 1')
        self.assertEqual(a['evidence'], b['evidence'])
        self.assertEqual((a['requested_goal'], b['requested_goal']), ('operating lease obligations', 'year-by-year operating lease maturity schedule'))
        self.assertEqual(a['audit_prompt'], PROMPT)
        self.assertIn('year-by-year operating lease maturity schedule', b['audit_prompt'])
        self.assertNotIn('operating lease obligations', b['audit_prompt'])
        self.assertTrue(out['contract_met'])
        self.assertEqual([out['variants'][k]['score'] for k in 'AB'], [4, 3])

    def test_grades_are_recorded_not_forced(self):
        _, out = self.run_it([3, 5])
        self.assertFalse(out['contract_met'])
        self.assertEqual([out['variants'][k]['score'] for k in 'AB'], [3, 5])

    def test_goal_must_be_prompt_span_and_evidence_is_reverified(self):
        with self.assertRaises(ValueError):
            self.run_it([4, 3], goal_a='not part of the prompt')
        tampered = saved()
        tampered['hops'][1]['evidence'][0]['text'] = 'altered'
        class Strict(Registry):
            def verify(self, hit, filters):
                if hit['text'] != 'aggregate text' and hit['id'].endswith('1'):
                    raise ValueError('retrieved content differs from verified SEC source')
                return super().verify(hit, filters)
        with self.assertRaises(ValueError):
            asyncio.run(cc.run_contrast(Scripted([4, 3]), Strict(), tampered, 0, -1, 'operating lease obligations', 'maturity schedule by year'))

    def test_explicit_prompt_override(self):
        model, _ = self.run_it([4, 3], prompt_b=PROMPT + ' Include the maturity schedule by year.', goal_b='maturity schedule by year')
        self.assertTrue(model.payloads[1]['audit_prompt'].endswith('by year.'))

    def test_invalid_indices_goals_and_oversized_context_fail_before_any_model_call(self):
        cases = [dict(subquery=3), dict(hop_index=5), dict(goal_b=' '), dict(goal_b='x' * 401)]
        for kw in cases:
            model = Scripted([4, 3])
            with self.assertRaises(ValueError, msg=str(kw)):
                args = dict(subquery=0, hop_index=-1, goal_a='operating lease obligations', goal_b='maturity schedule by year')
                args.update(kw)
                asyncio.run(cc.run_contrast(model, Registry(), saved(), **args))
            self.assertEqual(model.payloads, [])
        big = saved()
        big['hops'][1]['evidence'] = [evidence(n) for n in range(1, 7)]
        model = Scripted([4, 3])
        with self.assertRaises(ValueError):
            asyncio.run(cc.run_contrast(model, Registry(), big, 0, -1, 'operating lease obligations', 'maturity schedule by year'))
        self.assertEqual(model.payloads, [])

    def test_existing_output_is_refused_and_preserved_before_any_work(self):
        import tempfile
        import diagnose_ranks as dr
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'prior.json'
            out.write_text('prior evidence')
            common = ['--output', str(out), '--corpus', 'c', '--provenance', 'p']
            with self.assertRaises(SystemExit):
                cc.main(['--case', 'missing.json', '--subquery', '0', '--goal-a', 'a', '--goal-b', 'b'] + common)
            with self.assertRaises(SystemExit):
                dr.main(['--backend', 'http://127.0.0.1:9', '--replay', 'missing.json'] + common)
            self.assertEqual(out.read_text(), 'prior evidence')


if __name__ == '__main__':
    unittest.main()
