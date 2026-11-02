"""Goal/query separation contracts. Models and registries are scripted TEST DOUBLES; these are not model-quality results."""
import asyncio
import json
import unittest
from agent.critic import critique
from agent.filters import FilingFilter
from agent.models import Critique, Evidence, Hop, Plan, SubQuery
from agent.planner import plan_queries, validate_goal
from agent.state_machine import Agent

PROMPT = 'Audit ACME filing year 2024: separately retrieve liquidity  and cash-flow disclosures and operating lease obligations. Present excerpts.'
F1 = FilingFilter(ticker='ACME', year=2024, accession='000000000000000001')
F2 = FilingFilter(ticker='OTHR', year=2024, accession='000000000000000002')


class Registry:
    def resolve_filter(self, f):
        return f

    def catalog(self):
        return []

    def verify(self, hit, filters):
        return Evidence(id=hit['id'], text=f"text {hit['id']}", metadata={}, accession='a', url='https://www.sec.gov/x', tag='t')


class PlanModel:
    def __init__(self, goals, queries=('liquidity cash flow', 'lease obligations future minimum lease payments maturities')):
        self.goals, self.queries = goals, queries

    async def generate(self, schema, instruction, payload):
        self.instruction = instruction
        return Plan(subqueries=[SubQuery(query=q, goal=g, filters=F1) for q, g in zip(self.queries, self.goals)])


def plan(goals, queries=None):
    model = PlanModel(goals) if queries is None else PlanModel(goals, queries)
    return asyncio.run(plan_queries(model, PROMPT, Registry())), model


class Recorder:
    def __init__(self, score=4):
        self.payloads, self.score = [], score

    async def generate(self, schema, instruction, payload):
        self.payloads.append((instruction, payload))
        return Critique(score=self.score, rationale='fixture', rewritten_query='fixture rewrite', retain_ids=[])


def ev(n):
    return Evidence(id=f'000000000000000001#passage-{n:06}', text=f'text {n}', metadata={}, accession='a', url='https://www.sec.gov/x', tag='t')


class GoalSpan(unittest.TestCase):
    def test_goal_must_be_normalized_contiguous_nonempty_bounded_prompt_span(self):
        self.assertEqual(validate_goal(PROMPT, 'LIQUIDITY and cash-flow\ndisclosures'), 'LIQUIDITY and cash-flow disclosures')
        for bad in ['', '   ', 'liquidity disclosures', 'operating lease maturities by year', 'liquidity and cash-flow disclosures. Present excerpts. extra', 'x' * 401]:
            with self.assertRaises(ValueError, msg=bad):
                validate_goal(PROMPT, bad)
        with self.assertRaises(ValueError):  # bound applies even if the span is genuinely contiguous
            validate_goal('word ' * 200, 'word ' * 90)

    def test_planner_keeps_expanded_query_but_pins_goal_to_prompt(self):
        resolved, _ = plan(['liquidity  and cash-flow disclosures', 'operating lease obligations'])
        self.assertEqual([q.goal for q in resolved.subqueries], ['liquidity and cash-flow disclosures', 'operating lease obligations'])
        self.assertIn('maturities', resolved.subqueries[1].query)
        self.assertNotIn('maturities', resolved.subqueries[1].goal)

    def test_planner_rejects_invented_missing_or_cross_clause_goals_and_keeps_filters(self):
        for goals in (['liquidity and cash-flow disclosures', 'operating lease obligations by maturity year'], ['liquidity and cash-flow disclosures', ''],
                      ['liquidity', 'lease obligations extra words']):
            with self.assertRaises(ValueError):
                plan(goals)
        resolved, _ = plan(['liquidity and cash-flow disclosures', 'operating lease obligations'])
        self.assertTrue(all(q.filters == F1 for q in resolved.subqueries))


class CriticPayload(unittest.IsolatedAsyncioTestCase):
    async def graded(self, sub, query):
        model = Recorder()
        await critique(model, sub, query, [ev(1)], PROMPT, [])
        return model.payloads[0]

    async def test_goal_and_search_phrases_are_separate_and_scope_is_carried(self):
        sub = SubQuery(query='lease obligations future minimum lease payments maturities', goal='operating lease obligations', filters=F2)
        _, payload = await self.graded(sub, 'annual maturities of lease liabilities table')
        self.assertEqual(payload['requested_goal'], 'operating lease obligations')
        self.assertEqual(payload['search_query'], 'annual maturities of lease liabilities table')
        self.assertEqual(payload['initial_search_query'], sub.query)
        self.assertEqual(payload['current_scope'], F2.model_dump())
        self.assertNotIn('filters', payload)
        self.assertEqual(payload['audit_prompt'], PROMPT)
        self.assertNotIn('original', payload)
        self.assertNotIn('maturities', payload['requested_goal'])

    async def test_legacy_subquery_without_goal_falls_back_to_query(self):
        legacy = SubQuery.model_validate({'query': 'old style query', 'filters': F1.model_dump()})
        self.assertEqual(legacy.goal, '')
        _, payload = await self.graded(legacy, legacy.query)
        self.assertEqual(payload['requested_goal'], 'old style query')

    async def test_shared_multi_scope_goal_does_not_change_current_scope(self):
        shared = 'compare each company and each filing year, retrieve the medical care ratio by quarter'
        payloads = []
        for filters in (F1, F2):
            _, payload = await self.graded(SubQuery(query='ratio definition', goal=shared, filters=filters), 'ratio definition')
            payloads.append(payload)
        self.assertEqual([p['current_scope'] for p in payloads], [F1.model_dump(), F2.model_dump()])
        self.assertEqual({p['requested_goal'] for p in payloads}, {shared})  # complete qualifiers preserved verbatim, not trimmed per scope
        self.assertEqual(set(payloads[0]) , set(payloads[1]))
        self.assertEqual(payloads[0]['current_scope']['accession'], F1.accession)

    async def test_current_scope_is_independent_of_goal_and_query_text(self):
        sub = SubQuery(query='mentions OTHR 2023 in the hint', goal='operating lease obligations for OTHR', filters=F1)
        _, payload = await self.graded(sub, 'rewrite naming OTHR')
        self.assertEqual(payload['current_scope'], F1.model_dump())

    async def test_critic_still_cannot_accept_absent_evidence(self):
        with self.assertRaises(ValueError):
            await critique(Recorder(score=5), SubQuery(query='some query', goal='g' * 5, filters=F1), 'some query', [], PROMPT, [])


class HopTrace(unittest.TestCase):
    def test_every_hop_records_the_same_goal_across_rewrites(self):
        class Backend:
            def __init__(self):
                self.rounds = [[1, 2], [3, 4]]

            async def search(self, q, f):
                return [dict(id=f'000000000000000001#passage-{n:06}') for n in self.rounds.pop(0)]

        model = Recorder(score=3)
        agent = Agent(model, Backend(), Registry(), max_retries=1, mode='legacy')
        sub = SubQuery(query='original search phrase', goal='operating lease obligations', filters=F1)
        hops = asyncio.run(agent._retrieve(0, sub, PROMPT))
        self.assertEqual([h.requested_goal for h in hops], ['operating lease obligations'] * 2)
        self.assertEqual([p[1]['requested_goal'] for p in model.payloads], ['operating lease obligations'] * 2)
        self.assertEqual([p[1]['search_query'] for p in model.payloads], ['original search phrase', 'fixture rewrite'])
        self.assertTrue(all(h.score == 3 and not h.accepted for h in hops))  # no auto-accept of partial evidence
        legacy = asyncio.run(Agent(Recorder(score=3), type('B', (), {'rounds': [[1]], 'search': Backend.search})(), Registry(), max_retries=0, mode='legacy')
                             ._retrieve(0, SubQuery(query='legacy query', filters=F1), PROMPT))
        self.assertEqual(legacy[0].requested_goal, '')
        Hop.model_validate({k: v for k, v in hops[0].model_dump().items() if k != 'requested_goal'})  # old saved hops still load


if __name__ == '__main__':
    unittest.main()
