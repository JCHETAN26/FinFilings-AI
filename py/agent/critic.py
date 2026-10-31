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
    instruction += (' The ORIGINAL AUDIT PROMPT controls the requested dates. Filter year is FILING year, '
                    'not fiscal year. Use the cover-page fiscal_period_end in the catalog; do not demand fiscal '
                    '2024 figures from a 2024 filing reporting fiscal 2023. Rewrites must preserve that distinction. '
                    'Assess direct usefulness, not whether a single excerpt exhaustively answers all topics.')
    response = await model.generate(Critique, instruction, dict(audit_prompt=audit_prompt,
        filing_catalog=filing_catalog, original=original.model_dump(), query=query,
        evidence=[dict(id=item.id, text=item.text) for item in evidence]))
    if not evidence and response.score != 1:
        raise ValueError('critic tried to accept absent evidence')
    return response
