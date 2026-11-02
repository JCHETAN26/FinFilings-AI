"""Plan → bounded parallel Retrieve_Evaluate → strictly grounded Synthesize."""
import argparse
import asyncio
import json
from pathlib import Path
import time
from typing import Protocol, Any
from .critic import critique, critique_with_quotes
from .filters import FilingFilter
from .models import AgentResult, Evidence, Hop, State, SubQuery
from .planner import plan_queries
from .provider import ClaudeJSON, StructuredModel
from .provenance import ProvenanceRegistry
from .retrieval import HybridClient
from .fast import citation_for, ground_critic_claims, merge_citations
from .synthesizer import NO_EVIDENCE, draft_citations, escape_markdown, render_markdown, synthesize


class SearchBackend(Protocol):
    async def search(self, query: str, filters: FilingFilter) -> list[dict[str, Any]]: ...


FRESH_RESERVED = 4  # Fresh top-ranked verified hits that stale carry may never displace.
CONTEXT_LIMIT = 5
STALL_FLAG = 'Subquery {} stopped early: no verified evidence and the critic proposed no new query (stalled rewrite).'
EXECUTION_MODES = ('fast', 'legacy')
MAX_BACKEND_HITS = 10  # Protocol-level bound, independent of HybridClient.


def select_context(retained: list[Evidence], fresh: list[Evidence]) -> tuple[list[Evidence], list[Evidence]]:
    """Bounded context budget: returns (context, actually carried retained passages).

    The best FRESH_RESERVED verified fresh hits are always kept, so earlier carry can no longer push a
    newly relevant top-ranked hit out of a five-slot context. Retained passages (<=2, critic order) then
    use the remaining slots, in the order given (Agent passes nominees in CONTEXT order, not the critic's retain_ids order); one already inside the reserved fresh set costs no slot. Remaining slots are
    filled with further fresh hits in rank order. Carried passages are presented first (existing verifier
    prefix contract); a nominee that finds no slot is dropped and not reported as carried. Purely
    in-memory over already verified items: no lookups, no model calls.
    """
    if len(retained) > 2 or len(fresh) > CONTEXT_LIMIT:
        raise ValueError('evidence context exceeds retention/retrieval bounds')
    if len({i.id for i in retained}) != len(retained):
        raise ValueError('duplicate retained evidence')
    if len({i.id for i in fresh}) != len(fresh):
        raise ValueError('duplicate fresh evidence')
    reserved = {i.id for i in fresh[:FRESH_RESERVED]}
    slots = CONTEXT_LIMIT - len(reserved)
    carried = []
    for item in retained:
        if item.id in reserved:
            carried.append(item)
        elif slots > 0:
            carried.append(item)
            slots -= 1
    context = list(carried)
    present = {i.id for i in context}
    for item in fresh:
        if len(context) == CONTEXT_LIMIT:
            break
        if item.id not in present:
            context.append(item)
            present.add(item.id)
    return context, carried


def merge_evidence(retained: list[Evidence], fresh: list[Evidence]) -> list[Evidence]:
    """Context only; see select_context for the budget and for which retained passages were carried."""
    return select_context(retained, fresh)[0]


class Agent:
    def __init__(self, model: StructuredModel, backend: SearchBackend, registry: ProvenanceRegistry,
                 max_retries: int = 2, threshold: int = 4, concurrency: int = 3, mode: str = 'fast'):
        if not 0 <= max_retries <= 2 or not 1 <= threshold <= 5 or not 1 <= concurrency <= 3:
            raise ValueError('invalid agent limits')
        if mode not in EXECUTION_MODES:
            raise ValueError(f'execution mode must be one of {EXECUTION_MODES}')
        self.mode = mode  # 'fast': critic nominates quotes, host renders; 'legacy': separate HandleDraft synthesis call
        self.model, self.backend, self.registry = model, backend, registry
        self.max_retries, self.threshold = max_retries, threshold
        self.slots = asyncio.Semaphore(concurrency)
        self.running = asyncio.Lock()  # Bound retained results and preserve per-run model traces.

    async def _retrieve(self, position: int, subquery: SubQuery, audit_prompt: str) -> list[Hop]:
        history, query = [], subquery.query
        retained: list[Evidence] = []
        for attempt in range(self.max_retries + 1):
            start = time.perf_counter()
            async with self.slots:
                raw = await self.backend.search(query, subquery.filters)
                if not isinstance(raw, list) or len(raw) > MAX_BACKEND_HITS:
                    raise ValueError(f'backend returned more than {MAX_BACKEND_HITS} hits; refusing before verification or model calls')
                evidence = []
                # Only the best five VERIFIED passages go into the model context.
                for hit in raw:
                    item = self.registry.verify(hit, subquery.filters)
                    if item is not None:
                        evidence.append(item)
                        if len(evidence) == 5:
                            break
                fresh_ids = [item.id for item in evidence]
                evidence, carried = select_context(retained, evidence)
                if self.mode == 'fast':
                    verdict, handles = await critique_with_quotes(self.model, subquery, query, evidence, audit_prompt, self.registry.catalog())
                else:
                    verdict, handles = await critique(self.model, subquery, query, evidence, audit_prompt, self.registry.catalog()), {}
            accepted = bool(evidence) and verdict.score >= self.threshold
            # Fast mode: proposals count only when the hop itself is accepted at the unchanged threshold; otherwise they are discarded unseen.
            quotes, quote_flags = ground_critic_claims(verdict.claims, handles, position) if self.mode == 'fast' and accepted else ([], [])
            unchanged = verdict.rewritten_query.strip().casefold() == query.strip().casefold()
            # Fail-closed early stop, narrowly: no verified evidence at all, not the final attempt, critic proposed no new query.
            stalled = (not accepted and not evidence and attempt < self.max_retries and unchanged)
            history.append(Hop(subquery=position, attempt=attempt, query=query, requested_goal=subquery.goal, filters=subquery.filters,
                score=verdict.score, rationale=verdict.rationale, evidence=evidence,
                accepted=accepted, retained_evidence_ids=[item.id for item in carried], nominated_retained_ids=[item.id for item in retained],
                proposed_rewrite='' if accepted else verdict.rewritten_query, terminal_reason='stalled_rewrite' if stalled else None,
                retrieved_ids=[hit['id'] for hit in raw], fresh_verified_ids=fresh_ids,
                quote_selections=quotes, quote_flags=quote_flags, elapsed_seconds=time.perf_counter()-start))
            if accepted or stalled:
                break
            if attempt < self.max_retries and unchanged:
                raise ValueError('critic failed to rewrite a below-threshold query')  # evidence exists: genuine critic malfunction
            retained = [item for item in evidence if item.id in verdict.retain_ids]  # nominees: <=2, context order
            query = verdict.rewritten_query
        return history

    async def _finish_fast(self, prompt: str, histories: list[list[Hop]], selected: dict[str, Evidence]):
        """Synthesize without a final model call when every accepted subquery has host-verified critic quotes.

        If any accepted subquery has none, ONE existing HandleDraft call runs over just those subqueries' accepted contexts (at most 15
        passages) and its grounded citations are merged with the quotes already verified. Pure host formatting otherwise.
        """
        if not selected:
            text, citations, flags = NO_EVIDENCE
            return text, list(citations), list(flags), 'none'
        finals = [history[-1] for history in histories]
        groups, context, flags = [], [], []
        for hop in finals:
            by_id = {item.id: item for item in hop.evidence}
            groups.append([citation_for(by_id[q.chunk_id], q.quote) for q in hop.quote_selections] if hop.accepted else [])
            context.append(hop.evidence if hop.accepted else [])
            flags.extend(hop.quote_flags)
        gaps = [hop for hop in finals if hop.accepted and not hop.quote_selections]
        fallback = []
        if gaps:
            gap_evidence = {item.id: item for hop in gaps for item in hop.evidence}
            fallback, fallback_flags = await draft_citations(self.model, prompt, list(gap_evidence.values()))
            flags.extend(fallback_flags)
        citations = merge_citations(groups, fallback, context)
        return render_markdown(citations, flags), citations, flags, 'fallback' if gaps else 'host_quotes'

    async def run(self, prompt: str) -> AgentResult:
        if not prompt.strip() or len(prompt.encode()) > 8192:
            raise ValueError('audit prompt must contain 1..8192 bytes')
        async with self.running:
            start = time.perf_counter()
            calls = getattr(self.model, 'calls', None)
            if calls is not None:
                calls.clear()
            states = [State.PLAN]
            plan = await plan_queries(self.model, prompt, self.registry)
            states.append(State.RETRIEVE_EVALUATE)
            tasks = [asyncio.create_task(self._retrieve(i, query, prompt)) for i, query in enumerate(plan.subqueries)]
            try:
                histories = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            hops = [hop for history in histories for hop in history]
            selected: dict[str, Evidence] = {}
            flags = []
            for i, history in enumerate(histories):
                if not history[-1].accepted:
                    flags.append(f'Subquery {i} has no evidence meeting threshold after {len(history)-1} retries.')
                    if history[-1].terminal_reason == 'stalled_rewrite':
                        flags.append(STALL_FLAG.format(i))
                else:
                    for item in history[-1].evidence:
                        selected[item.id] = item
            states.append(State.SYNTHESIZE)
            if self.mode == 'legacy':
                markdown, citations, dropped = await synthesize(self.model, prompt, list(selected.values()))
                route = 'legacy' if selected else 'none'
            else:
                markdown, citations, dropped, route = await self._finish_fast(prompt, histories, selected)
            flags.extend(dropped)
            covered_ids = {citation.chunk_id for citation in citations}
            for i, history in enumerate(histories):
                if history[-1].accepted and not covered_ids.intersection(item.id for item in history[-1].evidence):
                    flags.append(f'Subquery {i} has retrieved evidence but no published supporting quote.')
            if flags:
                markdown += '\n\nEvidence gaps:\n' + '\n'.join('- ' + escape_markdown(flag) for flag in flags)
            return AgentResult(prompt=prompt, states=states, plan=plan, hops=hops, markdown=markdown,
                citations=citations, flags=flags, model_calls=list(calls or []), elapsed_seconds=time.perf_counter()-start,
                execution_mode=self.mode, synthesis_route=route)


async def main_async(args: argparse.Namespace) -> None:
    registry = ProvenanceRegistry(Path(args.corpus), Path(args.provenance))
    async with HybridClient(args.backend) as backend:
        result = await Agent(ClaudeJSON(args.model), backend, registry, mode=args.agent_mode).run(args.prompt)
    print(result.model_dump_json(indent=2) if args.json else result.markdown)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prompt')
    parser.add_argument('--backend', default='http://127.0.0.1:8765')
    parser.add_argument('--corpus', default='data/sec/chunks.jsonl')
    parser.add_argument('--provenance', default='data/sec/provenance.json')
    parser.add_argument('--model', default='haiku')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--agent-mode', choices=EXECUTION_MODES, default='fast', help='fast (default): critic-selected quotes, host formatting; legacy: separate synthesis call')
    asyncio.run(main_async(parser.parse_args()))


if __name__ == '__main__':
    main()
