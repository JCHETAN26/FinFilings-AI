"""Relevance grading and query-only correction; scope cannot be rewritten."""
import re
from .models import Critique, Evidence, SubQuery
from .provider import StructuredModel

_PASSAGE = re.compile(r'^(.*#passage-)(\d+)$')


def _scope(item: Evidence):
    """Verified identity shared by passages of one filing section, or None if the ID has no passage number."""
    match = _PASSAGE.match(item.id)
    if match is None:
        return None
    meta = item.metadata
    return (item.accession, meta.get('Ticker'), meta.get('FilingYear'), meta.get('SectionItem'), match.group(1)), int(match.group(2))


def review_view(evidence: list[Evidence]) -> tuple[list[Evidence], list[dict]]:
    """Critic-only presentation: document order within a verified scope plus adjacency markers.

    Retrieval rank interleaves passages, so a sentence cut at the end of passage N can be shown
    after its continuation in N+1. Only already-verified in-context items are reordered (groups keep
    first-appearance order); no text is merged, edited, or fetched, and IDs stay separate.
    """
    groups: dict = {}
    order: list = []
    for index, item in enumerate(evidence):
        info = _scope(item)
        key = info[0] if info else ('single', index)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((info[1] if info else 0, index, item))
    ordered, pairs = [], []
    for key in order:
        members = sorted(groups[key], key=lambda entry: (entry[0], entry[1]))
        ordered.extend(entry[2] for entry in members)
        if key[0] != 'single':
            pairs.extend(dict(before=a[2].id, after=b[2].id) for a, b in zip(members, members[1:]) if b[0] == a[0] + 1)
    return ordered, pairs


async def critique(model: StructuredModel, original: SubQuery, query: str, evidence: list[Evidence],
                   audit_prompt: str, filing_catalog: list[dict]) -> Critique:
    if not evidence:
        # No model can grade absent evidence as relevant. This explicit rule
        # still requests a real model rewrite to exercise bounded correction.
        instruction = 'There is NO verified evidence. You MUST score 1. Rewrite the query to improve lexical retrieval without changing scope.'
    else:
        instruction = ('Grade how well the evidence answers THIS subquery, 1 irrelevant, 2 weak, 3 partial, '
                       '4 directly useful, 5 fully supported. Judge actual text, not rank or keyword overlap alone. '
                       'Suggest a more targeted rewritten_query, preserving ticker/year/section. Do not answer the audit.')
    instruction += (' Grade the full ORIGINAL subquery goal against the supplied accumulated evidence, not just the latest search phrase. '
                    'When evidence is partial, set retain_ids to at most two supplied passage IDs containing useful support for the original goal; '
                    'the next search will retain those passages and target the missing topic. Never retain irrelevant passages. '
                    'Write a short topical rewritten_query for the missing information. Filing identity already lives in immutable filters: '
                    'do not add accession numbers, filing years, form names, or section locators to the search phrase. '
                    'Do not invent requirements for named competitors, market shares or numeric breakdowns that the original goal did not request. '
                    ' The ORIGINAL AUDIT PROMPT controls the requested dates. Filter year is FILING year, '
                    'not fiscal year. Use the cover-page fiscal_period_end in the catalog; do not demand fiscal '
                    '2024 figures from a 2024 filing reporting fiscal 2023. Rewrites must preserve that distinction. '
                    'Assess direct usefulness, not whether a single excerpt exhaustively answers all topics. '
                    'RUBRIC: (a) Judge only what THIS subquery asks, within its own filing scope; other subqueries, other filings or years, '
                    'and cross-filing comparison belong to other subqueries or the final synthesis and must never lower the score or appear in the rewrite. '
                    '(b) Read the supplied passages COLLECTIVELY: facts may be split across passages. Entries in adjacent_pairs are consecutive passages '
                    'of the same filing section listed in document order, so a sentence cut off at the end of "before" may continue at the start of "after"; '
                    'combine them for judging only, never assume text beyond what is shown, and retain_ids must name each passage separately. '
                    '(c) Score 4 or higher when the passages together state the core facts requested; details the goal did not explicitly ask for '
                    '(for example an original authorization size, share counts, approval dates) neither lower the score nor belong in a rewrite. '
                    'Score 3 or lower only for a requested element that is genuinely absent from every passage, and name that element in the rationale.')
    viewed, pairs = review_view(evidence)
    response = await model.generate(Critique, instruction, dict(audit_prompt=audit_prompt,
        filing_catalog=filing_catalog, original=original.model_dump(), query=query,
        evidence=[dict(id=item.id, text=item.text) for item in viewed], adjacent_pairs=pairs))
    if len(set(response.retain_ids)) != len(response.retain_ids) or not set(response.retain_ids).issubset(item.id for item in evidence):
        raise ValueError('critic retained unknown or duplicate evidence')
    if not evidence and response.score != 1:
        raise ValueError('critic tried to accept absent evidence')
    return response
