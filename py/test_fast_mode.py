"""Fast execution mode contracts. Registry, backend and model are scripted TEST DOUBLES: these check controller/host behaviour
(binding, thresholds, fallback, ledger, cancellation), not model quality and not latency."""
import asyncio
import json
import unittest
from pydantic import ValidationError
from agent.critic import QUOTE_RULES, build_instruction, critique, critique_with_quotes
from agent.fast import MAX_FINAL_CITATIONS, merge_citations
from agent.filters import FilingFilter
from agent.models import Citation, Critique, CritiqueWithQuotes, CriticClaim, Evidence, HandleClaim, HandleDraft, Plan, SubQuery
from agent.state_machine import Agent
from audit_web.errors import ProjectionError
from audit_web.projection import project_result

ACC = '000000000000000001'
FILTERS = FilingFilter(ticker='TST', year=2024, accession=ACC)
PROMPT = 'Audit TST filing year 2024: retrieve alpha disclosures and beta disclosures.'
TEXT = {
    1: 'Alpha disclosures state that supplier concentration is high and three vendors provide most critical components.',
    2: 'Beta disclosures state that export controls could restrict shipments of the products to certain regions of the world.',
    3: 'Alpha addendum: the company also depends on one foundry for advanced packaging capacity in the coming years.',
}
QUOTE = {1: TEXT[1][:90], 2: TEXT[2][:90], 3: TEXT[3][:90]}


def pid(n):
    return f'{ACC}#passage-{n:06}'


class Registry:
    def verify(self, hit, filters):
        n = int(hit['id'][-6:])
        return Evidence(id=hit['id'], text=TEXT[n], metadata={}, accession='0000000000-00-000001', url='https://www.sec.gov/Archives/x', tag=f'TST-p{n}')

    def catalog(self):
        return []

    def resolve_filter(self, f):
        return f


class Backend:
    def __init__(self, hits=None):
        self.hits = hits or {'alpha': [1], 'beta': [2]}

    async def search(self, query, filters):
        return [dict(id=pid(n)) for n in self.hits['alpha' if 'alpha' in query else 'beta']]


class Model:
    """Plan -> alpha/beta. CritiqueWithQuotes -> `script[topic]` = (score, [(ref-or-'@N', quote)]); '@N' resolves to the handle of passage N."""
    def __init__(self, script, draft=None, delay=0.0, retain_refs=()):
        self.script, self.draft, self.delay, self.retain_refs = script, draft, delay, retain_refs
        self.seen, self.payloads, self.instructions = [], [], []

    async def generate(self, schema, instruction, payload):
        self.seen.append(schema.__name__)
        self.payloads.append((schema, payload))
        self.instructions.append(instruction)
        if self.delay:
            await asyncio.sleep(self.delay)
        if schema is Plan:
            return Plan(subqueries=[SubQuery(query='alpha query', goal='alpha disclosures', filters=FILTERS), SubQuery(query='beta query', goal='beta disclosures', filters=FILTERS)])
        if schema is HandleDraft:
            return HandleDraft(claims=[HandleClaim(ref=r, quote=q) for r, q in (self.draft or [])])
        topic = 'alpha' if 'alpha' in payload['requested_goal'] else 'beta'
        entry = self.script[topic]
        if isinstance(entry, list):  # one scripted entry per successive call for this topic
            entry = entry.pop(0)
        score, claims, *more = entry
        retain = more[0] if more else self.retain_refs
        # fast payloads carry handles only (no canonical ids); '@N' finds passage N by its text
        by_text = {row['text']: row.get('ref', 'E00') for row in payload['evidence']}
        built = [CriticClaim(ref=by_text.get(TEXT[int(ref[1:])], 'E99') if ref.startswith('@') else ref, quote=quote) for ref, quote in claims]
        if schema is CritiqueWithQuotes:
            return CritiqueWithQuotes(score=score, rationale='fixture', rewritten_query='another ' + topic + ' query', retain_refs=list(retain), claims=built)
        return Critique(score=score, rationale='fixture', rewritten_query='another ' + topic + ' query', retain_ids=[])


def run(model, backend=None, mode='fast', retries=0):
    agent = Agent(model, backend or Backend(), Registry(), max_retries=retries, mode=mode)
    return asyncio.run(agent.run(PROMPT))


GOOD = {'alpha': (4, [('@1', QUOTE[1])]), 'beta': (5, [('@2', QUOTE[2])])}


class HostQuotes(unittest.TestCase):
    def test_every_accepted_subquery_with_valid_quotes_needs_no_final_model_call(self):
        model = Model(GOOD)
        result = run(model)
        self.assertEqual(model.seen, ['Plan', 'CritiqueWithQuotes', 'CritiqueWithQuotes'])
        self.assertEqual((result.execution_mode, result.synthesis_route), ('fast', 'host_quotes'))
        self.assertEqual([(c.chunk_id, c.quote) for c in result.citations], [(pid(1), QUOTE[1]), (pid(2), QUOTE[2])])
        self.assertTrue(all(f'[{c.tag}](https://www.sec.gov/Archives/x)' in result.markdown and f'`{c.chunk_id}`' in result.markdown for c in result.citations))
        self.assertEqual([[(q.chunk_id, q.quote) for q in h.quote_selections] for h in result.hops], [[(pid(1), QUOTE[1])], [(pid(2), QUOTE[2])]])
        self.assertEqual(result.flags, [])

    def test_legacy_mode_still_runs_the_separate_synthesis_call(self):
        model = Model({'alpha': (4, []), 'beta': (5, [])}, draft=[('E01', QUOTE[1]), ('E02', QUOTE[2])])
        result = run(model, mode='legacy')
        self.assertEqual(model.seen, ['Plan', 'Critique', 'Critique', 'HandleDraft'])
        self.assertEqual((result.execution_mode, result.synthesis_route, len(result.citations)), ('legacy', 'legacy', 2))
        self.assertTrue(all(not h.quote_selections and not h.quote_flags for h in result.hops))

    def test_default_mode_is_fast_and_unknown_modes_are_refused(self):
        self.assertEqual(Agent(None, None, Registry()).mode, 'fast')
        with self.assertRaises(ValueError):
            Agent(None, None, Registry(), mode='turbo')

    def test_below_threshold_quote_proposals_are_never_published_or_recorded(self):
        model = Model({'alpha': (4, [('@1', QUOTE[1])]), 'beta': (3, [('@2', QUOTE[2])])})
        result = run(model)
        self.assertEqual([c.chunk_id for c in result.citations], [pid(1)])
        beta = [h for h in result.hops if h.subquery == 1]
        self.assertTrue(all(not h.accepted and not h.quote_selections and not h.quote_flags for h in beta))
        self.assertNotIn(QUOTE[2], result.markdown)
        self.assertIn('Subquery 1 has no evidence meeting threshold after 0 retries.', result.flags)
        self.assertEqual(model.seen.count('HandleDraft'), 0)  # a rejected goal is a gap, not a reason to fall back

    def test_no_accepted_evidence_makes_no_synthesis_call_and_publishes_nothing(self):
        model = Model({'alpha': (2, [('@1', QUOTE[1])]), 'beta': (1, [])})
        result = run(model)
        self.assertEqual((result.synthesis_route, result.citations, model.seen.count('HandleDraft')), ('none', [], 0))
        self.assertIn('No verified answer; evidence unavailable or below threshold.', result.flags)


class Fallback(unittest.TestCase):
    def test_wrong_ref_wrong_quote_and_short_quote_are_dropped_with_bounded_flags_then_one_fallback(self):
        forged = 'A fabricated sentence claiming revenue grew ninety nine percent in the filing year.'
        model = Model({'alpha': (4, [('E99', QUOTE[1]), ('@1', forged), ('@1', 'too short')]), 'beta': (5, [('@2', QUOTE[2])])}, draft=[('E01', QUOTE[1])])
        result = run(model)
        self.assertEqual(model.seen, ['Plan', 'CritiqueWithQuotes', 'CritiqueWithQuotes', 'HandleDraft'])
        self.assertEqual(result.synthesis_route, 'fallback')
        self.assertEqual(sorted((c.chunk_id, c.quote) for c in result.citations), sorted([(pid(1), QUOTE[1]), (pid(2), QUOTE[2])]))  # beta's valid quote kept
        alpha = result.hops[0] if result.hops[0].subquery == 0 else result.hops[1]
        self.assertEqual(len(alpha.quote_flags), 3)
        self.assertTrue(all(len(f) < 120 and f in result.flags for f in alpha.quote_flags))
        self.assertNotIn('fabricated', ' '.join(result.flags) + result.markdown)
        self.assertFalse(any('too short' in f for f in result.flags))
        schema, payload = [p for p in model.payloads if p[0] is HandleDraft][0]
        self.assertEqual([row['text'] for row in payload['evidence']], [TEXT[1]])  # only the gap subquery's accepted context

    def test_quote_copied_from_a_different_handle_is_rejected(self):
        backend = Backend({'alpha': [1, 3], 'beta': [2]})
        model = Model({'alpha': (4, [('@3', QUOTE[1])]), 'beta': (4, [('@2', QUOTE[2])])}, draft=[('E01', QUOTE[1])])
        result = run(model, backend)
        alpha = next(h for h in result.hops if h.subquery == 0)
        self.assertEqual(alpha.quote_selections, [])
        self.assertTrue(any('not found verbatim' in f for f in alpha.quote_flags))
        self.assertEqual(result.synthesis_route, 'fallback')

    def test_fallback_that_publishes_nothing_leaves_an_honest_gap_and_keeps_valid_quotes(self):
        model = Model({'alpha': (4, []), 'beta': (5, [('@2', QUOTE[2])])}, draft=[])
        result = run(model)
        self.assertEqual([c.chunk_id for c in result.citations], [pid(2)])
        self.assertIn('Subquery 0 has retrieved evidence but no published supporting quote.', result.flags)
        self.assertIn('Subquery 0 has retrieved evidence but no published supporting quote.', result.markdown.replace('\\', ''))
        self.assertEqual(model.seen.count('HandleDraft'), 1)  # one bounded fallback, never a second

    def test_fallback_citations_cannot_leave_the_gap_contexts(self):
        model = Model({'alpha': (4, []), 'beta': (5, [('@2', QUOTE[2])])}, draft=[('E01', QUOTE[1]), ('E02', QUOTE[2])])
        result = run(model)
        self.assertEqual(sorted(c.chunk_id for c in result.citations), [pid(1), pid(2)])  # E02 does not exist in the fallback context: dropped
        self.assertTrue(any('unknown evidence handle' in f for f in result.flags))

    def test_fallback_transport_failure_is_not_swallowed(self):
        class Failing(Model):
            async def generate(self, schema, instruction, payload):
                if schema is HandleDraft:
                    raise RuntimeError('Claude generation failed, exit=1')
                return await super().generate(schema, instruction, payload)
        with self.assertRaises(RuntimeError):
            run(Failing({'alpha': (4, []), 'beta': (5, [('@2', QUOTE[2])])}))


class Merge(unittest.TestCase):
    def cite(self, n, quote):
        return Citation(tag='t', chunk_id=pid(n), accession='a', url='https://www.sec.gov/Archives/x', quote=quote)

    def test_dedup_cap_and_goal_preservation(self):
        shared = self.cite(1, 'q' * 40)
        groups = [[shared] + [self.cite(1, f'{i}' * 40) for i in range(2, 5)], [shared, self.cite(2, 'b' * 40)], []]
        fallback = [self.cite(3, f'{i}' * 41) for i in range(10)] + [self.cite(3, 'z' * 41)]
        context = [[Evidence(id=pid(1), text='x', metadata={}, accession='a', url='u', tag='t')], [Evidence(id=pid(2), text='x', metadata={}, accession='a', url='u', tag='t')],
                   [Evidence(id=pid(3), text='x', metadata={}, accession='a', url='u', tag='t')]]
        merged = merge_citations(groups, fallback, context)
        self.assertEqual(len(merged), MAX_FINAL_CITATIONS)
        self.assertEqual(len({(c.chunk_id, c.quote) for c in merged}), len(merged))
        self.assertIn(self.cite(2, 'b' * 40), merged)  # subquery 1 keeps its quote
        self.assertTrue(any(c.chunk_id == pid(3) for c in merged))  # the gap subquery keeps a fallback quote even under the cap
        reserved = merge_citations([[], []], [self.cite(3, 'a' * 41)] * 3, [[], [Evidence(id=pid(3), text='x', metadata={}, accession='a', url='u', tag='t')]])
        self.assertEqual(len(reserved), 1)

    def test_existing_critic_quotes_are_never_displaced_by_fallback_when_one_goal_needs_fallback(self):
        ev = lambda n: [Evidence(id=pid(n), text='x', metadata={}, accession='a', url='u', tag='t')]
        critic = [[self.cite(1, f'{i}' * 40) for i in range(4)], [self.cite(2, f'{i}' * 40) for i in range(4)], []]
        fallback = [self.cite(3, f'f{i}' * 21) for i in range(12)]  # a long fallback list for the third goal
        merged = merge_citations(critic, fallback, [ev(1), ev(2), ev(3)])
        self.assertEqual(len(merged), MAX_FINAL_CITATIONS)
        for citation in critic[0] + critic[1]:
            self.assertIn(citation, merged)  # all 8 verified critic quotes survive
        self.assertEqual(sum(c.chunk_id == pid(3) for c in merged), 4)  # the rest of the cap goes to the gap goal's fallback quotes, in order
        self.assertEqual([c for c in merged if c.chunk_id == pid(3)], fallback[:4])
        self.assertEqual(merged[:8], critic[0] + critic[1])  # stable order: critic quotes first
        # a gap goal whose fallback quote duplicates another goal's critic quote is still represented by that published quote
        shared = self.cite(1, 's' * 40)
        merged = merge_citations([[shared], []], [shared], [ev(1), ev(1)])
        self.assertEqual(merged, [shared])


class Critic(unittest.TestCase):
    def evidence(self, order=(1, 2)):
        return [Evidence(id=pid(n), text=TEXT[n], metadata={}, accession='a', url='https://www.sec.gov/Archives/x', tag=f't{n}') for n in order]

    class Spy:
        def __init__(self, **fields):
            self.seen, self.fields = [], fields

        async def generate(self, schema, instruction, payload):
            self.seen.append((schema, instruction, payload))
            if schema is CritiqueWithQuotes:
                return CritiqueWithQuotes(score=4, rationale='r', rewritten_query='alpha query', **self.fields)
            return schema(score=4, rationale='r', rewritten_query='alpha query', retain_ids=[])

    sq = SubQuery(query='alpha query', goal='alpha disclosures', filters=FILTERS)

    def fast(self, evidence, **fields):
        spy = self.Spy(**fields)
        verdict, handles = asyncio.run(critique_with_quotes(spy, self.sq, 'alpha query', evidence, PROMPT, []))
        return spy, verdict, handles

    def test_legacy_critic_prompt_payload_and_schema_are_unchanged(self):
        spy, evidence = self.Spy(), self.evidence()
        asyncio.run(critique(spy, self.sq, 'alpha query', evidence, PROMPT, []))
        schema, instruction, payload = spy.seen[0]
        self.assertIs(schema, Critique)
        self.assertEqual(instruction, build_instruction(evidence))
        self.assertIn('set retain_ids to at most two supplied passage IDs', instruction)
        self.assertNotIn('retain_refs', instruction)
        self.assertEqual([set(r) for r in payload['evidence']], [{'id', 'text'}] * 2)
        self.assertEqual(payload['adjacent_pairs'], [dict(before=pid(1), after=pid(2))])  # legacy still speaks canonical IDs
        self.assertEqual(list(Critique.model_json_schema()['properties']), ['score', 'rationale', 'rewritten_query', 'retain_ids'])

    def test_fast_wire_schema_requests_retain_refs_and_never_canonical_ids(self):
        properties = CritiqueWithQuotes.model_json_schema()['properties']
        self.assertIn('retain_refs', properties)
        self.assertNotIn('retain_ids', properties)
        with self.assertRaises(ValidationError):
            CritiqueWithQuotes(score=4, rationale='r', rewritten_query='alpha query', retain_ids=[pid(1)])
        with self.assertRaises(ValidationError):
            CritiqueWithQuotes(score=4, rationale='r', rewritten_query='alpha query', retain_refs=['E01', 'E02', 'E03'])

    def test_fast_prompt_shares_the_rules_and_the_model_is_never_shown_a_source_id(self):
        evidence = self.evidence((2, 1))  # retrieval rank order differs from document order
        spy, verdict, handles = self.fast(evidence)
        schema, instruction, payload = spy.seen[0]
        self.assertIs(schema, CritiqueWithQuotes)
        self.assertEqual(instruction, build_instruction(evidence, refs=True) + QUOTE_RULES)
        for rule in ('SCOPE CONTRACT', 'REQUIREMENTS', 'SCORING', 'DATES', 'RETENTION AND REWRITE'):
            self.assertIn(rule, instruction)
        self.assertIn('set retain_refs to at most two distinct supplied passage refs', instruction)
        self.assertNotIn('retain_ids', instruction)
        self.assertIn('may not rescue a score', QUOTE_RULES)
        self.assertEqual(payload['evidence'], [dict(ref='E01', text=TEXT[1]), dict(ref='E02', text=TEXT[2])])
        self.assertEqual(payload['adjacent_pairs'], [dict(before='E01', after='E02')])  # split passages: handles, in document order
        self.assertNotIn(ACC, json.dumps(payload['adjacent_pairs'] + payload['evidence']))
        legacy_payload = {k: v for k, v in payload.items() if k not in ('evidence', 'adjacent_pairs')}
        self.assertEqual(set(legacy_payload), {'audit_prompt', 'filing_catalog', 'requested_goal', 'search_query', 'initial_search_query', 'current_scope'})

    def test_known_handles_resolve_to_exact_canonical_ids(self):
        evidence = self.evidence((2, 1))
        for refs, expected in ((['E01'], [pid(1)]), (['E02'], [pid(2)]), (['E02', 'E01'], [pid(2), pid(1)]), ([], [])):
            _, verdict, handles = self.fast(evidence, retain_refs=refs)
            self.assertEqual(verdict.retain_ids, expected)
            self.assertEqual(list(handles), ['E01', 'E02'])

    def test_unknown_and_duplicate_handles_are_rejected_fail_closed_with_counts_only(self):
        evidence = self.evidence()
        cases = ((['E09'], '1 unknown, 0 duplicate'), (['e01'], '1 unknown, 0 duplicate'), ([pid(1)], '1 unknown, 0 duplicate'),
                 (['E01', 'E01'], '0 unknown, 1 duplicate'), (['E01', 'E09'], '1 unknown, 0 duplicate'), (['E09', 'E09'], '2 unknown, 1 duplicate'))
        for refs, counts in cases:
            with self.subTest(refs=refs):
                with self.assertRaises(ValueError) as caught:
                    self.fast(evidence, retain_refs=refs)
                self.assertIn(counts, str(caught.exception))
                self.assertNotIn(refs[0], str(caught.exception).replace('E01', '') if refs[0] != 'E01' else '')
        with self.assertRaises(ValueError):  # no evidence: nothing can be retained
            self.fast([], retain_refs=['E01'])

    def test_absent_evidence_cannot_be_accepted_and_claim_count_is_bounded(self):
        class Accepting:
            async def generate(self, schema, instruction, payload):
                return schema(score=4, rationale='r', rewritten_query='alpha query')
        with self.assertRaises(ValueError):
            asyncio.run(critique_with_quotes(Accepting(), self.sq, 'alpha query', [], PROMPT, []))
        with self.assertRaises(ValidationError):
            CritiqueWithQuotes(score=4, rationale='r', rewritten_query='alpha query', claims=[CriticClaim(ref='E01', quote='x' * 40)] * 5)


class Retention(unittest.TestCase):
    def test_next_hop_receives_canonical_ids_from_handles_and_trace_is_canonical(self):
        backend = Backend({'alpha': [1, 3], 'beta': [2]})
        model = Model({'alpha': [(3, [], ['E02']), (4, [('@3', QUOTE[3])])], 'beta': (4, [('@2', QUOTE[2])])})
        result = run(model, backend, retries=1)
        first, second = [h for h in result.hops if h.subquery == 0]
        self.assertEqual((first.accepted, first.retained_evidence_ids, first.nominated_retained_ids), (False, [], []))
        self.assertEqual(second.nominated_retained_ids, [pid(3)])  # E02 (document order: p1=E01, p3=E02) -> canonical ID
        self.assertIn(pid(3), second.retained_evidence_ids)
        self.assertTrue(all(i.startswith(ACC) for i in second.nominated_retained_ids + second.retained_evidence_ids))
        self.assertEqual((result.synthesis_route, model.seen.count('CritiqueWithQuotes')), ('host_quotes', 3))

    def test_a_bad_retention_handle_fails_the_run_without_retry_extra_call_or_published_claims(self):
        for refs in (['E09'], ['E01', 'E01']):
            model = Model({'alpha': (3, [('@1', QUOTE[1])], refs), 'beta': (5, [('@2', QUOTE[2])])})
            with self.assertRaises(ValueError):
                run(model, retries=2)
            self.assertEqual(model.seen.count('CritiqueWithQuotes'), 2)  # one call per subquery; the bad one is not retried
            self.assertNotIn('HandleDraft', model.seen)

    def test_failed_quotes_and_below_threshold_proposals_still_cannot_publish_with_valid_retention(self):
        model = Model({'alpha': (3, [('@1', QUOTE[1])], ['E01']), 'beta': (4, [('E99', QUOTE[2])], [])}, draft=[('E01', QUOTE[2])])
        result = run(model, retries=0)
        self.assertNotIn(QUOTE[1], result.markdown)
        self.assertEqual([c.chunk_id for c in result.citations], [pid(2)])  # beta's bad handle -> single fallback over beta's context only
        self.assertEqual((result.synthesis_route, model.seen.count('HandleDraft')), ('fallback', 1))


class Cancellation(unittest.TestCase):
    def test_cancelling_a_fast_run_propagates_and_releases_the_agent(self):
        async def scenario():
            model = Model(GOOD, delay=5)
            agent = Agent(model, Backend(), Registry(), mode='fast')
            task = asyncio.create_task(agent.run(PROMPT))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(agent.running.locked())
            self.assertEqual(model.seen, ['Plan'])
        asyncio.run(scenario())


class Projection(unittest.TestCase):
    def test_projection_reports_the_real_mode_and_rejects_forged_selections(self):
        result = run(Model(GOOD))
        document = json.loads(project_result(result))
        self.assertEqual((document['diagnostics']['execution_mode'], document['diagnostics']['synthesis_route']), ('fast', 'host_quotes'))
        self.assertEqual([h['quote_selections'] for h in document['hops']], [1, 1])
        forged = result.model_copy(deep=True)
        forged.hops[0].quote_selections[0] = forged.hops[0].quote_selections[0].model_copy(update=dict(quote='A fabricated sentence that is not inside the passage text at all.'))
        with self.assertRaises(ProjectionError):
            project_result(forged)
        rejected = run(Model({'alpha': (3, []), 'beta': (5, [('@2', QUOTE[2])])})).model_copy(deep=True)
        leaked = next(h for h in rejected.hops if not h.accepted)
        leaked.quote_selections.append(rejected.hops[-1].quote_selections[0])
        with self.assertRaises(ProjectionError):
            project_result(rejected)


if __name__ == '__main__':
    unittest.main()
