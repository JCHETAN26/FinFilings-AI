"""Relevance grading and query-only correction; scope cannot be rewritten."""
from .models import Critique, Evidence, SubQuery
from .provider import StructuredModel


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
                    'Assess direct usefulness, not whether a single excerpt exhaustively answers all topics.')
    response = await model.generate(Critique, instruction, dict(audit_prompt=audit_prompt,
        filing_catalog=filing_catalog, original=original.model_dump(), query=query,
        evidence=[dict(id=item.id, text=item.text) for item in evidence]))
    if len(set(response.retain_ids)) != len(response.retain_ids) or not set(response.retain_ids).issubset(item.id for item in evidence):
        raise ValueError('critic retained unknown or duplicate evidence')
    if not evidence and response.score != 1:
        raise ValueError('critic tried to accept absent evidence')
    return response
