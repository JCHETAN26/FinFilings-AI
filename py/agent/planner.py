"""Structured multi-hop decomposition with immutable filing constraints."""
from .models import Plan
from .provider import StructuredModel
from .provenance import ProvenanceRegistry


async def plan_queries(model: StructuredModel, prompt: str, registry: ProvenanceRegistry) -> Plan:
    plan = await model.generate(Plan,
        'Decompose the audit into exactly 2 or 3 distinct targeted retrieval queries. Each must have a ticker '
        'and filing year. Preserve the user\'s requested ticker/year/section exactly, even if absent from the catalog. '
        'Year means filing_year, not fiscal year. Consult fiscal_period_end from the verified cover page. '
        'Do not put years or fiscal-year constraints into the query text unless the USER explicitly asks for a fiscal period; '
        'filing date constraints belong exclusively in filters. A 2024 filing can report fiscal 2023. '
        'A missing filing is not permission to use another year. '
        'Set section="" unless an exact section is explicitly requested; never relax an explicit section based on catalog availability. '
        'Use concise topical search phrases; filing identifiers belong in filters, not the query. '
        'Use accession=""; the controller resolves trusted accessions. Do not answer the audit or invent facts.',
        dict(prompt=prompt, verified_filing_catalog=registry.catalog()))
    resolved = [query.model_copy(update={'filters': registry.resolve_filter(query.filters)}) for query in plan.subqueries]
    if len({(query.query.casefold(), query.filters) for query in resolved}) != len(resolved):
        raise ValueError('planner produced duplicate subqueries')
    return Plan(subqueries=resolved)
