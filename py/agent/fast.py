"""Fast execution mode: the critic that already read each scoped context also nominates its quotes; the HOST verifies them.

Nothing here talks to a model. Quote proposals are untrusted: each one is bound by an EXACT dictionary lookup of its short handle
(E01..E05) in the context the critic saw and must appear verbatim (whitespace-normalized) in that passage, with the same 40..2000
character bounds as the legacy synthesis. Proposals from a below-threshold hop are discarded unseen; nothing is forced to be accepted.
"""
from .models import HOP_QUOTE_MAX, HOP_QUOTE_MIN, Citation, CriticClaim, Evidence, HandleClaim, HandleDraft, HopQuote
from .synthesizer import ground_handles, normalize

MAX_FINAL_CITATIONS = 12  # same bound as the legacy HandleDraft
LENGTH_FLAG = 'quote length outside {}..{} characters'.format(HOP_QUOTE_MIN, HOP_QUOTE_MAX)


def ground_critic_claims(claims: list[CriticClaim], handles: dict[str, Evidence], position: int) -> tuple[list[HopQuote], list[str]]:
    """Verified quotes for one ACCEPTED hop plus bounded flags. A flag names the subquery and the reason, never the rejected text."""
    usable: list[HandleClaim] = []
    flags: list[str] = []
    for claim in claims:
        if not HOP_QUOTE_MIN <= len(normalize(claim.quote)) <= HOP_QUOTE_MAX:
            flags.append(f'Critic quote (subquery {position}): {LENGTH_FLAG}')
        else:
            usable.append(HandleClaim(ref=claim.ref, quote=normalize(claim.quote)))
    citations, dropped = ground_handles(HandleDraft(claims=usable), handles)  # the legacy lookup and substring guard, unchanged
    flags.extend(f'Critic quote (subquery {position}): {text}' for text in dropped)
    return [HopQuote(chunk_id=c.chunk_id, quote=c.quote) for c in citations], flags


def citation_for(item: Evidence, quote: str) -> Citation:
    return Citation(tag=item.tag, chunk_id=item.id, accession=item.accession, url=item.url, quote=quote)


def merge_citations(groups: list[list[Citation]], fallback: list[Citation], context: list[list[Evidence]], cap: int = MAX_FINAL_CITATIONS) -> list[Citation]:
    """Deduplicate and cap the published set; verified critic quotes are never displaced by fallback redundancy.

    `groups[i]` are the host-verified critic quotes of accepted subquery i (empty when it has none), `context[i]` its accepted evidence
    (empty list for a non-accepted subquery). Fallback citations come only from gap-subquery contexts. Priority when the cap binds:
    (1) every critic quote, (2) one fallback representative for each accepted goal that has no critic quote, (3) remaining fallback quotes.
    Output order is stable: critic quotes by subquery, then fallback. A job has at most 3 subqueries x 4 critic quotes = 12 and a gap
    leaves at most 8, so with the cap of 12 rule (1) is never cut; the reserve below keeps the guarantee even for other caps.
    """
    candidates: list[tuple[int | None, Citation]] = []
    seen: set[tuple[str, str]] = set()
    for position, group in enumerate(groups):
        for citation in group:
            if (citation.chunk_id, citation.quote) not in seen:
                seen.add((citation.chunk_id, citation.quote))
                candidates.append((position, citation))
    for citation in fallback:
        if (citation.chunk_id, citation.quote) not in seen:
            seen.add((citation.chunk_id, citation.quote))
            candidates.append((None, citation))
    has_own = {owner for owner, _ in candidates if owner is not None}
    reps: set[int] = set()
    for position, items in enumerate(context):
        if not items or position in has_own:
            continue
        ids = {item.id for item in items}
        for index, (_, citation) in enumerate(candidates):  # published citations of this goal (fallback, or a deduplicated shared quote)
            if citation.chunk_id in ids:
                reps.add(index)
                break
    chosen = set(reps)
    for index, (owner, _) in enumerate(candidates):
        if owner is not None and len(chosen) < cap:
            chosen.add(index)
    for index in range(len(candidates)):
        if len(chosen) >= cap:
            break
        chosen.add(index)
    return [candidates[i][1] for i in sorted(chosen)]
