"""Empty-evidence stalled-rewrite stop. Backend, registry and critic are scripted TEST DOUBLES (contracts, not quality)."""
import asyncio
import unittest
from agent.filters import FilingFilter
from agent.models import Critique, Evidence, Plan, SubQuery
from agent.state_machine import Agent

ACC = '000000000000000001'
FILTERS = FilingFilter(ticker='TST', year=2024, accession=ACC)
PROMPT = 'Audit TST filing year 2024: retrieve alpha disclosures and beta disclosures.'


def pid(n):
    return f'{ACC}#passage-{n:06}'


class Registry:
    def verify(self, hit, filters):
        return Evidence(id=hit['id'], text=f"text {hit['id']}", metadata={}, accession='a', url='https://www.sec.gov/x', tag='t')

    def catalog(self):
        return []

    def resolve_filter(self, f):
        return f


class Backend:
    def __init__(self, rounds):
        self.rounds, self.searches = list(rounds), 0

    async def search(self, query, filters):
        self.searches += 1
        return [dict(id=pid(n)) for n in self.rounds.pop(0)]


class Model:
    """Plan -> two subqueries; Critique -> scripted (score, rewrite) per call, in call order across subqueries."""
    def __init__(self, critiques, goals=('alpha disclosures', 'beta disclosures')):
        self.critiques, self.goals, self.seen = list(critiques), goals, []

    async def generate(self, schema, instruction, payload):
        self.seen.append(schema.__name__)
        if schema is Plan:
            return Plan(subqueries=[SubQuery(query='alpha query', goal=self.goals[0], filters=FILTERS), SubQuery(query='beta query', goal=self.goals[1], filters=FILTERS)])
        score, rewrite = self.critiques.pop(0)
        return Critique(score=score, rationale='fixture', rewritten_query=rewrite, retain_ids=[])


def retrieve(rounds, critiques, retries=2):
    model, backend = Model(critiques), Backend(rounds)
    hops = asyncio.run(Agent(model, backend, Registry(), max_retries=retries)._retrieve(0, SubQuery(query='alpha query', goal='alpha disclosures', filters=FILTERS), PROMPT))
    return hops, model, backend


class Stall(unittest.TestCase):
    def test_empty_evidence_unchanged_rewrite_stops_with_typed_terminal_and_records_rewrite(self):
        hops, model, backend = retrieve([[]], [(1, '  ALPHA   query'.replace('   ', ' '))])
        self.assertEqual(len(hops), 1)
        self.assertEqual((hops[0].terminal_reason, hops[0].accepted, hops[0].score, hops[0].evidence), ('stalled_rewrite', False, 1, []))
        self.assertEqual(hops[0].proposed_rewrite, '  ALPHA query')  # the actual critic output, not fabricated
        self.assertEqual((backend.searches, model.seen), (1, ['Critique']))  # one search, one critic call, nothing more

    def test_stall_after_one_real_rewrite_counts_one_retry_and_no_extra_calls(self):
        calls = []

        class Counting(Model):
            async def generate(self, schema, instruction, payload):
                calls.append(schema.__name__)
                return await super().generate(schema, instruction, payload)
        model, backend = Counting([(1, 'a better alpha query'), (1, 'a better alpha query')]), Backend([[], []])
        hops = asyncio.run(Agent(model, backend, Registry())._retrieve(0, SubQuery(query='alpha query', goal='alpha disclosures', filters=FILTERS), PROMPT))
        self.assertEqual([h.terminal_reason for h in hops], [None, 'stalled_rewrite'])
        self.assertEqual([h.query for h in hops], ['alpha query', 'a better alpha query'])
        self.assertEqual(hops[0].proposed_rewrite, hops[1].query)
        self.assertEqual((calls, backend.searches), (['Critique'] * 2, 2))

    def test_final_attempt_unchanged_rewrite_is_exhaustion_not_stall(self):
        hops, _, _ = retrieve([[], [], []], [(1, 'second query'), (1, 'third query'), (1, 'third query')])
        self.assertEqual(len(hops), 3)
        self.assertTrue(all(h.terminal_reason is None for h in hops))
        self.assertEqual(hops[2].proposed_rewrite, 'third query')

    def test_unchanged_rewrite_with_evidence_remains_an_exception(self):
        with self.assertRaises(ValueError):
            retrieve([[1, 2]], [(3, 'alpha query')])

    def test_changed_rewrite_continues_and_accepted_hop_records_no_rewrite(self):
        hops, _, _ = retrieve([[], [4, 5]], [(1, 'changed alpha query'), (5, 'unused rewrite value')])
        self.assertEqual([h.accepted for h in hops], [False, True])
        self.assertEqual((hops[0].proposed_rewrite, hops[1].proposed_rewrite, hops[1].terminal_reason), ('changed alpha query', '', None))

    def test_stalled_subquery_is_a_published_gap_not_evidence_and_adds_true_flags(self):
        model = Model([(1, 'alpha query'), (1, 'beta query')])
        agent = Agent(model, Backend([[], []]), Registry())
        result = asyncio.run(agent.run(PROMPT))
        self.assertEqual(model.seen, ['Plan', 'Critique', 'Critique'])
        self.assertEqual(result.citations, [])
        self.assertFalse(any(h.accepted for h in result.hops))
        for i in (0, 1):
            self.assertIn(f'Subquery {i} has no evidence meeting threshold after 0 retries.', result.flags)
            self.assertIn(f'Subquery {i} stopped early: no verified evidence and the critic proposed no new query (stalled rewrite).', result.flags)
        self.assertIn('Evidence gaps:', result.markdown)

    def test_exhausted_subquery_has_no_stop_flag(self):
        model = Model([(1, 'r1 alpha query'), (1, 'r2 alpha query'), (1, 'r2 alpha query'), (1, 'r1 beta query'), (1, 'r2 beta query'), (1, 'r2 beta query')])
        result = asyncio.run(Agent(model, Backend([[]] * 6), Registry()).run(PROMPT))
        self.assertFalse(any('stopped early' in f for f in result.flags))
        self.assertEqual(len([h for h in result.hops if h.subquery == 0]), 3)


if __name__ == '__main__':
    unittest.main()
