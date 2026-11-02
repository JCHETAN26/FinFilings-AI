"""Contract tests for the rank diagnostic. All data below are TEST FIXTURES, not retrieval-quality evidence."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagnose_ranks as dr  # noqa: E402
from agent.filters import FilingFilter  # noqa: E402
from agent.models import Evidence  # noqa: E402

ACC = '000000000000000001'
FILTERS = dict(ticker='TST', year=2024, section='', accession=ACC)


def pid(n):
    return f'{ACC}#passage-{n:06}'


def evidence(n):
    return dict(id=pid(n), text=f'fixture text {n}', metadata={'ID': pid(n)}, accession='x', url='https://www.sec.gov/x', tag=f't{n}')


class FakeRegistry:
    def __init__(self, unverifiable=()):
        self.unverifiable = {pid(n) for n in unverifiable}

    def verify(self, hit, filters):
        return None if hit['id'] in self.unverifiable else Evidence(**evidence(int(hit['id'][-6:])))


def result(reranked, sparse=(), dense=(), hybrid=()):
    mk = lambda ids: [dict(id=pid(n), score=1.0 / (i + 1)) for i, n in enumerate(ids)]
    return dict(sparse=mk(sparse), dense=mk(dense), hybrid=mk(hybrid), reranked=mk(reranked))


def saved_case(path, hops):
    path.write_text(json.dumps(dict(hops=[dict(subquery=s, attempt=a, query=q, filters=FILTERS, score=3, rationale='r',
        evidence=[evidence(n) for n in ev], accepted=False, retained_evidence_ids=[pid(n) for n in ret], elapsed_seconds=0.0)
        for s, a, q, ev, ret in hops])))


class RankDiagnosticTests(unittest.TestCase):
    def test_replay_extracts_query_filters_and_retained_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'case.json'
            saved_case(path, [(0, 0, 'first query', [1, 2], []), (1, 0, 'second query', [3, 4, 5], []), (1, 1, 'third query', [4, 6], [4])])
            hops = dr.load_replays([path], 1)
        self.assertEqual([h['query'] for h in hops], ['second query', 'third query'])
        self.assertEqual(hops[1]['retained_ids'], [pid(4)])
        self.assertEqual([e['id'] for e in hops[1]['retained_evidence']], [pid(4)])

    def test_query_cap_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'case.json'
            saved_case(path, [(0, i, f'query {i}', [1], []) for i in range(dr.MAX_QUERIES + 1)])
            with self.assertRaises(ValueError):
                dr.load_replays([path], None)

    def test_review_target_never_reaches_request(self):
        hop = dict(query='operating expenses', filters=FILTERS)
        request = dr.build_request(hop)
        self.assertEqual(set(request), {'query', 'rerank', 'filters'})
        self.assertNotIn('passage-000064', json.dumps(request))

    def test_stage_ranks_and_context_displacement(self):
        # Target ranks 6th after reranking: reported as reranked=6 yet absent from the five verified fresh items.
        hop = dict(source='fixture', subquery=0, attempt=1, query='q', filters=FILTERS, retained_ids=[pid(90), pid(91)],
                   recorded_context_ids=[pid(90), pid(91), pid(1), pid(2), pid(3)], recorded_score=3,
                   retained_evidence=[evidence(90), evidence(91)])
        res = result(reranked=[1, 2, 3, 4, 5, 64, 7], sparse=[9, 64], dense=[64, 8], hybrid=[8, 64])
        record = dr.analyse(res, hop, FakeRegistry(), [pid(64)])
        target = record['review_only_targets'][pid(64)]
        self.assertEqual((target['sparse'], target['dense'], target['hybrid_rrf'], target['reranked']), (2, 1, 2, 6))
        self.assertFalse(target['in_fresh_top5'])
        # Budget: fresh top-4 reserved, only ONE nominee fits; the second is reported as slotless, not carried.
        self.assertEqual(record['context']['merged_context_ids'], [pid(90), pid(1), pid(2), pid(3), pid(4)])
        self.assertEqual(record['context']['carried_retained_ids'], [pid(90)])
        self.assertEqual(record['context']['nominees_without_slot'], [pid(91)])
        self.assertEqual(record['context']['displaced_by_retention'], [pid(5)])
        self.assertEqual(record['context']['dropped_verified_beyond_five'], [pid(64), pid(7)])
        self.assertFalse(record['replay_matches_recorded_context'])  # recorded fixture used the old budget

    def test_rank_four_target_survives_two_retained(self):
        hop = dict(source='fixture', subquery=0, attempt=1, query='q', filters=FILTERS, retained_ids=[pid(90), pid(91)],
                   recorded_context_ids=[], recorded_score=3, retained_evidence=[evidence(90), evidence(91)])
        record = dr.analyse(result(reranked=[1, 2, 3, 64, 5]), hop, FakeRegistry(), [pid(64)])
        self.assertTrue(record['review_only_targets'][pid(64)]['in_merged_context'])

    def test_saved_retained_evidence_is_reverified(self):
        tampered = dict(evidence(90), text='altered after saving')
        hop = dict(source='fixture', subquery=0, attempt=1, query='q', filters=FILTERS, retained_ids=[pid(90)],
                   recorded_context_ids=[], recorded_score=3, retained_evidence=[tampered])
        with self.assertRaises(ValueError):
            dr.analyse(result(reranked=[1]), hop, FakeRegistry(), [])

    def test_unverifiable_hits_are_skipped_before_the_five_cut(self):
        hop = dict(source='fixture', subquery=0, attempt=0, query='q', filters=FILTERS, retained_ids=[], recorded_context_ids=[],
                   recorded_score=1, retained_evidence=[])
        record = dr.analyse(result(reranked=[1, 2, 3, 4, 5, 6, 7]), hop, FakeRegistry(unverifiable=[2]), [pid(6)])
        self.assertEqual(record['context']['unverifiable_ids'], [pid(2)])
        self.assertEqual(record['context']['fresh_top5_ids'], [pid(n) for n in (1, 3, 4, 5, 6)])
        self.assertTrue(record['review_only_targets'][pid(6)]['in_fresh_top5'])
        self.assertFalse(record['replay_matches_recorded_context'])

    def test_absent_target_reports_none(self):
        self.assertIsNone(dr.ranks([dict(id='a', score=1)], 'b'))


if __name__ == '__main__':
    unittest.main()
