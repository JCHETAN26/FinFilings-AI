"""Agent._retrieve context-budget contracts. Backend, registry and critic are scripted TEST FIXTURES (not quality evidence)."""
import asyncio
import unittest
from agent.filters import FilingFilter
from agent.models import Critique, Evidence, SubQuery
from agent.state_machine import Agent

ACC = '000000000000000001'
FILTERS = FilingFilter(ticker='TST', year=2024, accession=ACC)


def pid(n):
    return f'{ACC}#passage-{n:06}'


class Registry:
    def verify(self, hit, filters):
        n = hit['id']
        return None if hit.get('bad') else Evidence(id=n, text=f'text {n}', metadata={}, accession='a', url='https://www.sec.gov/x', tag=n[-6:])

    def catalog(self):
        return []


class Backend:
    def __init__(self, rounds):
        self.rounds = list(rounds)

    async def search(self, query, filters):
        return [dict(id=pid(n), bad=n < 0) for n in self.rounds.pop(0)]


class Model:
    def __init__(self, nominees):
        self.nominees, self.calls = list(nominees), []

    async def generate(self, schema, instruction, payload):
        retain = [pid(n) for n in (self.nominees.pop(0) if self.nominees else [])]
        return Critique(score=3, rationale='fixture', rewritten_query=f'rewrite {len(self.calls)} text', retain_ids=retain)


def run(rounds, nominees):
    agent = Agent(Model(nominees), Backend(rounds), Registry(), max_retries=len(rounds) - 1, mode='legacy')
    return asyncio.run(agent._retrieve(0, SubQuery(query='original query', filters=FILTERS), 'audit'))


class ContextBudget(unittest.TestCase):
    def test_displaced_nominee_is_not_reported_as_carried_and_prefix_holds(self):
        hops = run([[1, 2, 3, 4, 5, 6], [10, 11, 12, 13, 14, 15]], [[1, 2]])
        self.assertEqual([h.retained_evidence_ids for h in hops], [[], [pid(1)]])
        second = hops[1]
        self.assertEqual(second.nominated_retained_ids, [pid(1), pid(2)])
        ids = [e.id for e in second.evidence]
        self.assertEqual(ids, [pid(n) for n in (1, 10, 11, 12, 13)])
        self.assertEqual(ids[:len(second.retained_evidence_ids)], second.retained_evidence_ids)
        self.assertTrue(set(second.retained_evidence_ids) <= {e.id for e in hops[0].evidence})
        self.assertEqual(second.retrieved_ids, [pid(n) for n in (10, 11, 12, 13, 14, 15)])
        self.assertEqual(second.fresh_verified_ids, [pid(n) for n in (10, 11, 12, 13, 14)])

    def test_unverifiable_hits_do_not_consume_reserved_fresh_slots(self):
        hops = run([[1, 2, 3, 4, 5], [-1, 10, 11, 12, 13, 14]], [[1, 2]])
        self.assertNotIn(pid(-1), [e.id for e in hops[1].evidence])
        self.assertTrue({pid(10), pid(11), pid(12), pid(13)} <= {e.id for e in hops[1].evidence})

    def test_overlap_keeps_both_without_crowding(self):
        hops = run([[1, 2, 3, 4, 5], [2, 1, 10, 11, 12]], [[1, 2]])
        self.assertEqual([e.id for e in hops[1].evidence], [pid(n) for n in (1, 2, 10, 11, 12)])
        self.assertEqual(hops[1].retained_evidence_ids, [pid(1), pid(2)])

    def test_few_fresh_preserves_two(self):
        hops = run([[1, 2, 3, 4, 5], [10, 11]], [[1, 2]])
        self.assertEqual([e.id for e in hops[1].evidence], [pid(n) for n in (1, 2, 10, 11)])

    def test_oversized_or_malformed_backend_response_fails_before_any_model_call(self):
        class Calls(Model):
            async def generate(self, *a):
                self.calls.append(a)
                return await super().generate(*a)
        model = Calls([])
        with self.assertRaises(ValueError):
            asyncio.run(Agent(model, Backend([list(range(1, 12))]), Registry(), max_retries=0, mode='legacy')._retrieve(0, SubQuery(query='original query', filters=FILTERS), 'audit'))
        self.assertEqual(model.calls, [])
        class Bad:
            async def search(self, q, f):
                return {'reranked': []}
        model = Calls([])
        with self.assertRaises(ValueError):
            asyncio.run(Agent(model, Bad(), Registry(), max_retries=0, mode='legacy')._retrieve(0, SubQuery(query='original query', filters=FILTERS), 'audit'))
        self.assertEqual(model.calls, [])

    def test_rank_four_fresh_survives_two_nominees(self):
        hops = run([[1, 2, 3, 4, 5], [10, 11, 12, 13, 14]], [[1, 2]])
        self.assertIn(pid(13), [e.id for e in hops[1].evidence])
        self.assertLessEqual(len(hops[1].evidence), 5)


if __name__ == '__main__':
    unittest.main()
