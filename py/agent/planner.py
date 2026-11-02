"""Structured multi-hop decomposition with immutable filing constraints."""
from .models import GOAL_MAX, Plan
from .provider import StructuredModel
from .provenance import ProvenanceRegistry


def normalize_span(text: str) -> str:
    return ' '.join(text.split()).casefold()


def validate_goal(prompt: str, goal: str) -> str:
    """Require a non-empty, bounded, case/whitespace-normalized CONTIGUOUS span of the actual user prompt."""
    cleaned = ' '.join(goal.split())
    if not cleaned or len(cleaned) > GOAL_MAX:
        raise ValueError(f'planner goal must contain 1..{GOAL_MAX} characters')
    if normalize_span(cleaned) not in normalize_span(prompt):
        raise ValueError('planner goal is not a contiguous span of the audit prompt')
    return cleaned


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
        'For every subquery set goal to a short but COMPLETE contiguous span copied verbatim from the user\'s prompt that states what '
        'that subquery must find: prefer concise but complete topic wording that keeps every explicit metric, granularity, period or other '
        'qualifier the user attached to that topic, and leave out other topics (at most 400 characters, no rewording). Where the user '
        'writes one shared clause for several companies or years ("each", "both"), copy that clause: it is interpreted within the '
        'subquery\'s own filters. The query may add search vocabulary, '
        'but the query never adds requirements: only the goal states what must be found. '
        'Use accession=""; the controller resolves trusted accessions. Do not answer the audit or invent facts.',
        dict(prompt=prompt, verified_filing_catalog=registry.catalog()))
    resolved = [query.model_copy(update={'filters': registry.resolve_filter(query.filters), 'goal': validate_goal(prompt, query.goal)})
                for query in plan.subqueries]
    if len({(query.query.casefold(), query.filters) for query in resolved}) != len(resolved):
        raise ValueError('planner produced duplicate subqueries')
    return Plan(subqueries=resolved)
