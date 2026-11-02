#!/usr/bin/env python3
"""Bounded REAL-model critic contrast: same search phrase, same source-verified evidence, two explicit user goals.

Replays one saved hop's accumulated context (re-verified against the SEC corpus) through the PRODUCTION `critique`
function twice, strictly sequentially: goal A (a span of the original audit prompt) and goal B (the same request with
an explicit extra requirement). It makes exactly two authenticated Claude LLM calls (the CLI is online) and nothing else: no
retrieval service, no external data fetch, no agent loop. It is a two-call model regression check, NOT a gold-accuracy
measurement. Grades are never forced; the expected contract (A >= 4, B < 4) is recorded next to what the model returned.
Inputs are bounded and validated before any model call; the output file is created exclusively and never overwritten.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))
sys.path.insert(0, str(ROOT / 'bench_kit'))
from agent.critic import critique  # noqa: E402
from agent.filters import FilingFilter  # noqa: E402
from agent.models import SubQuery  # noqa: E402
from agent.planner import validate_goal  # noqa: E402
from agent.provenance import ProvenanceRegistry, digest  # noqa: E402
from diagnose_ranks import reverify  # noqa: E402

THRESHOLD = 4
MAX_CASE_BYTES = 32 << 20
MAX_EVIDENCE = 5
MAX_GOAL = 400


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def variant_prompt(prompt: str, goal_a: str, goal_b: str, prompt_b: str | None) -> str:
    """Modified audit prompt for B: explicit override, or the original with goal A's span replaced by goal B."""
    if prompt_b is not None:
        return prompt_b
    validate_goal(prompt, goal_a)
    squashed = ' '.join(prompt.split())
    start = squashed.casefold().index(' '.join(goal_a.split()).casefold())
    return squashed[:start] + ' '.join(goal_b.split()) + squashed[start + len(' '.join(goal_a.split())):]


async def run_contrast(model, registry, saved: dict, subquery: int, hop_index: int, goal_a: str, goal_b: str, prompt_b: str | None = None) -> dict:
    subqueries = saved['plan']['subqueries']
    if not 0 <= subquery < len(subqueries):
        raise ValueError('subquery index out of range')
    plan_query = subqueries[subquery]
    hops = [h for h in saved['hops'] if h['subquery'] == subquery]
    if not hops or not -len(hops) <= hop_index < len(hops):
        raise ValueError('hop index out of range')
    hop = hops[hop_index]
    if not 1 <= len(hop['evidence']) <= MAX_EVIDENCE:
        raise ValueError(f'selected hop must carry 1..{MAX_EVIDENCE} evidence passages')
    for goal in (goal_a, goal_b):
        if not goal.strip() or len(goal) > MAX_GOAL:
            raise ValueError(f'goals must contain 1..{MAX_GOAL} characters')
    filters = FilingFilter(**plan_query['filters'])
    evidence = reverify(registry, hop['evidence'], filters)
    prompt_a, prompt_b = saved['prompt'], variant_prompt(saved['prompt'], goal_a, goal_b, prompt_b)
    variants = {}
    for name, goal, prompt in (('A', goal_a, prompt_a), ('B', goal_b, prompt_b)):  # sequential: one model call in flight
        planned = SubQuery(query=plan_query['query'], goal=validate_goal(prompt, goal), filters=filters)
        before = len(getattr(model, 'calls', []))
        verdict = await critique(model, planned, hop['query'], evidence, prompt, registry.catalog())
        calls = list(getattr(model, 'calls', []))[before:]
        variants[name] = dict(goal=planned.goal, audit_prompt=prompt, score=verdict.score, rationale=verdict.rationale,
            rewritten_query=verdict.rewritten_query, retain_ids=verdict.retain_ids, model_calls=calls)
    met = variants['A']['score'] >= THRESHOLD and variants['B']['score'] < THRESHOLD
    return dict(
        note='Two real critic calls on identical evidence and search phrase; regression contrast only, not gold accuracy. Grades are as returned by the model.',
        expected=dict(A=f'score >= {THRESHOLD} (goal does not ask for the extra element)', B=f'score < {THRESHOLD} (goal explicitly asks for an element absent from the evidence)'),
        contract_met=met, subquery=subquery, hop_index=hop_index, search_query=hop['query'], initial_search_query=plan_query['query'],
        filters=plan_query['filters'], evidence=[dict(id=e.id, text_sha256=sha(e.text), text=e.text) for e in evidence],
        variants=variants)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case', type=Path, required=True, help='saved raw agent case JSON')
    p.add_argument('--subquery', type=int, required=True)
    p.add_argument('--hop-index', type=int, default=-1, help='index among that subquery\'s hops (default: last)')
    p.add_argument('--goal-a', required=True, help='span of the original audit prompt')
    p.add_argument('--goal-b', required=True, help='modified request with an explicit extra requirement')
    p.add_argument('--prompt-b', help='override the modified audit prompt (default: original prompt with goal A replaced by goal B)')
    p.add_argument('--corpus', type=Path, required=True)
    p.add_argument('--provenance', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--model', default='haiku')
    args = p.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f'{args.output} exists; outputs are never overwritten')
    from agent.provider import ClaudeJSON
    if args.case.stat().st_size > MAX_CASE_BYTES:
        raise SystemExit('saved case exceeds the diagnostic input bound')
    saved_bytes = args.case.read_bytes()
    saved = json.loads(saved_bytes)
    registry = ProvenanceRegistry(args.corpus, args.provenance)
    try:
        result = asyncio.run(run_contrast(ClaudeJSON(model=args.model, concurrency=1), registry, saved, args.subquery, args.hop_index,
                                          args.goal_a, args.goal_b, args.prompt_b))
    finally:
        registry.close()
    result.update(case_path=str(args.case), case_sha256=hashlib.sha256(saved_bytes).hexdigest(), corpus_sha256=digest(args.corpus), model_requested=args.model)
    with args.output.open('x') as stream:  # exclusive create: never overwrite, even under a race
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({n: dict(goal=v['goal'], score=v['score']) for n, v in result['variants'].items()} | dict(contract_met=result['contract_met'])))
    return 0 if result['contract_met'] else 1


if __name__ == '__main__':
    sys.exit(main())
