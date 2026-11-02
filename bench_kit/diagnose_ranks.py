#!/usr/bin/env python3
"""Bounded, LLM-free rank diagnostic: replay saved agent queries against the REAL hybrid bridge.

The bridge itself runs the real BGE dense encoder and the cross-encoder reranker; only agent/LLM calls and
external data fetches are absent.

For every replayed hop it records the actual sparse, dense, RRF-hybrid and reranked ranks and then runs the
PRODUCTION context-selection code (provenance verify -> first five verified -> merge_evidence with the hop's
recorded retained passages). No agent/LLM calls are made, queries run strictly sequentially, and nothing is
fetched from the network.

`--review-target ID` marks passages whose ranks are REPORTED for human review only. They are never sent to the
backend, never alter the query/filters, and never influence selection (see `build_request`).
"""
import argparse
import json
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))
from agent.filters import FilingFilter  # noqa: E402
from agent.models import Evidence  # noqa: E402
from agent.provenance import ProvenanceRegistry  # noqa: E402
from agent.state_machine import select_context  # noqa: E402

MAX_QUERIES = 12            # hard cap on replayed hops per invocation
MAX_RESPONSE_BYTES = 8 << 20
MAX_SAVED_CASE_BYTES = 64 << 20


def load_replays(paths: list[Path], subquery: int | None) -> list[dict]:
    """Extract (query, filters, retained ids, recorded context) per hop from saved raw agent results."""
    hops = []
    for path in paths:
        if path.stat().st_size > MAX_SAVED_CASE_BYTES:
            raise ValueError(f'{path} is too large')
        saved = json.loads(path.read_text())
        known = {}
        for hop in saved['hops']:
            for item in hop['evidence']:
                known[item['id']] = item
        for hop in saved['hops']:
            if subquery is not None and hop['subquery'] != subquery:
                continue
            retained = hop.get('retained_evidence_ids', [])
            hops.append(dict(source=str(path), subquery=hop['subquery'], attempt=hop['attempt'], query=hop['query'],
                filters=hop['filters'], retained_ids=retained, recorded_context_ids=[e['id'] for e in hop['evidence']],
                recorded_score=hop['score'], retained_evidence=[known[i] for i in retained if i in known]))
    if not hops:
        raise ValueError('no hops selected for replay')
    if len(hops) > MAX_QUERIES:
        raise ValueError(f'{len(hops)} hops selected; the diagnostic is capped at {MAX_QUERIES}')
    return hops


def build_request(hop: dict) -> dict:
    """The ONLY thing sent to the backend. Review targets are structurally unable to reach it."""
    return dict(query=hop['query'], rerank=True, filters=FilingFilter(**hop['filters']).model_dump())


def ranks(hits: list[dict], target: str) -> int | None:
    for index, hit in enumerate(hits, 1):
        if hit['id'] == target:
            return index
    return None


def simulate_context(registry, raw: list[dict], filters: FilingFilter, retained: list[Evidence]) -> dict:
    """Run the same verify/first-five/merge steps as Agent._retrieve and report what each step kept."""
    verified, rejected = [], []
    for position, hit in enumerate(raw, 1):
        item = registry.verify(hit, filters)
        if item is None:
            rejected.append(hit['id'])
            continue
        verified.append((position, item))
    fresh = [item for _, item in verified][:5]
    merged, carried = select_context(retained, fresh)
    return dict(carried_retained_ids=[i.id for i in carried], nominated_retained_ids=[i.id for i in retained], unverifiable_ids=rejected, fresh_top5_ids=[i.id for i in fresh], merged_context_ids=[i.id for i in merged],
        dropped_verified_beyond_five=[i.id for _, i in verified[5:]],
        displaced_by_retention=[i.id for i in fresh if i.id not in {m.id for m in merged}],
        nominees_without_slot=[i.id for i in retained if i.id not in {c.id for c in carried}])


def reverify(registry, saved: list[dict], filters: FilingFilter) -> list[Evidence]:
    """Saved evidence is re-checked against the source corpus before it may be carried."""
    result = []
    for item in saved:
        checked = registry.verify(dict(id=item['id'], text=item['text'], metadata=item['metadata']), filters)
        if checked is None or checked != Evidence(**item):
            raise ValueError(f"saved retained evidence {item['id']} does not match the verified source")
        result.append(checked)
    return result


def analyse(result: dict, hop: dict, registry, targets: list[str]) -> dict:
    raw = result.get('reranked', [])
    filters = FilingFilter(**hop['filters'])
    retained = reverify(registry, hop['retained_evidence'], filters)
    context = simulate_context(registry, raw, filters, retained)
    stage = lambda hits, limit: [dict(rank=i, id=h['id'], score=h['score']) for i, h in enumerate(hits[:limit], 1)]
    review = {t: dict(sparse=ranks(result['sparse'], t), dense=ranks(result['dense'], t), hybrid_rrf=ranks(result['hybrid'], t),
        reranked=ranks(raw, t), in_fresh_top5=t in context['fresh_top5_ids'], in_merged_context=t in context['merged_context_ids'],
        in_recorded_context=t in hop['recorded_context_ids']) for t in targets}
    return dict(source=hop['source'], subquery=hop['subquery'], attempt=hop['attempt'], query=hop['query'], filters=hop['filters'],
        retained_ids=hop['retained_ids'], recorded_context_ids=hop['recorded_context_ids'], recorded_critic_score=hop['recorded_score'],
        counts=dict(sparse=len(result['sparse']), dense=len(result['dense']), hybrid=len(result['hybrid']), reranked=len(raw)),
        sparse_top20=stage(result['sparse'], 20), dense_top20=stage(result['dense'], 20), hybrid_top20=stage(result['hybrid'], 20),
        reranked_top10=stage(raw, 10), context=context,
        replay_matches_recorded_context=context['merged_context_ids'] == hop['recorded_context_ids'],
        review_only_targets=review, timings=result.get('timings'))


def search(backend: str, request: dict) -> dict:
    http = urllib.request.Request(backend.rstrip('/') + '/search', data=json.dumps(request).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(http, timeout=125) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError('backend response exceeds diagnostic bound')
    return json.loads(body)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend', required=True)
    p.add_argument('--corpus', type=Path, required=True)
    p.add_argument('--provenance', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--replay', type=Path, action='append', required=True, help='saved raw agent case JSON (repeatable)')
    p.add_argument('--subquery', type=int, help='restrict to one subquery index')
    p.add_argument('--review-target', action='append', default=[], help='REVIEW-ONLY passage ID whose ranks are reported')
    args = p.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f'{args.output} exists; outputs are never overwritten')  # refuse before any query
    hops = load_replays(args.replay, args.subquery)
    registry = ProvenanceRegistry(args.corpus, args.provenance)
    try:
        records = []
        for hop in hops:  # strictly sequential: one in-flight query
            record = analyse(search(args.backend, build_request(hop)), hop, registry, args.review_target)
            records.append(record)
            print(json.dumps(dict(source=Path(record['source']).parts[-4:-2], subquery=record['subquery'], attempt=record['attempt'],
                query=record['query'], reranked=[h['id'].split('-')[-1] for h in record['reranked_top10']],
                merged=[i.split('-')[-1] for i in record['context']['merged_context_ids']], targets=record['review_only_targets'])), flush=True)
        payload = (json.dumps(dict(note='Review-only diagnostic; targets were never sent to the backend. Not a quality metric.',
            review_targets=args.review_target, records=records), indent=2) + '\n')
        with args.output.open('x') as stream:  # exclusive create
            stream.write(payload)
    finally:
        registry.close()


if __name__ == '__main__':
    main()
