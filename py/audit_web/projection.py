"""Project an AgentResult into the browser document, serialized exactly once.

Fail-closed: every citation must bind to verified evidence that appears in the same result (same chunk ID,
accession, tag, SEC URL and a verbatim quote); every link must be a real https://www.sec.gov/Archives/ URL.
Each passage text is stored once even if several hops or citations use it. The AgentResult is not retained.
"""
import json
import re
from urllib.parse import urlsplit
from agent.models import AgentResult, Evidence
from . import limits
from .errors import ProjectionError, ResultTooLarge

_BAD_URL_CHARS = re.compile(r'[\s\x00-\x1f\x7f\\<>"\'`]')
_ACCESSION = re.compile(r'\d{10}-\d{2}-\d{6}')
_SUBQUERY_FLAG = re.compile(r'^Subquery (\d+) ')


def is_sec_archive_url(url: object) -> bool:
    """Only https://www.sec.gov/Archives/... with no credentials, port, query or fragment."""
    if not isinstance(url, str) or not 20 <= len(url) <= 512 or _BAD_URL_CHARS.search(url):
        return False
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.netloc == 'www.sec.gov' and parts.hostname == 'www.sec.gov' and parts.port is None
                and not parts.username and not parts.password and not parts.query and not parts.fragment
                and parts.path.startswith('/Archives/') and '..' not in parts.path.split('/'))
    except ValueError:
        return False


def _utf16_offset(text: str, index: int) -> int:
    return len(text[:index].encode('utf-16-le')) // 2


def quote_span(text: str, quote: str) -> list[int] | None:
    """UTF-16 [start, end) of the whitespace-normalized quote inside the original passage (what JS string indexing uses)."""
    tokens = quote.split()
    if not tokens:
        return None
    match = re.search(r'\s+'.join(re.escape(t) for t in tokens), text)
    return [_utf16_offset(text, match.start()), _utf16_offset(text, match.end())] if match else None


def _evidence_row(item: Evidence) -> dict:
    if not is_sec_archive_url(item.url):
        raise ProjectionError('evidence has an untrusted source link')
    if not _ACCESSION.fullmatch(item.accession):
        raise ProjectionError('evidence has an invalid accession')
    meta = item.metadata
    return dict(id=item.id, tag=item.tag, accession=item.accession, url=item.url, company=meta.get('Ticker'),
                filing_year=meta.get('FilingYear'), form=meta.get('FormType'), section=meta.get('SectionItem'), text=item.text)


def project_result(result: AgentResult) -> bytes:
    evidence: dict[str, Evidence] = {}
    for hop in result.hops:
        for item in hop.evidence:
            known = evidence.setdefault(item.id, item)
            if known != item:
                raise ProjectionError('the same passage ID carries different content in one result')
    rows = {key: _evidence_row(item) for key, item in evidence.items()}
    accepted = {item.id for hop in result.hops if hop.accepted for item in hop.evidence}
    excerpts = []
    for number, citation in enumerate(result.citations, 1):
        item = evidence.get(citation.chunk_id)
        if item is None or citation.chunk_id not in accepted:
            raise ProjectionError('citation does not refer to accepted evidence in this result')
        if (citation.accession, citation.tag, citation.url) != (item.accession, item.tag, item.url):
            raise ProjectionError('citation identity differs from its evidence')
        span = quote_span(item.text, citation.quote)
        if span is None:
            raise ProjectionError('citation quote is not verbatim in its evidence')
        row = rows[item.id]
        excerpts.append(dict(n=number, quote=citation.quote, quote_span=span, tag=citation.tag, chunk_id=citation.chunk_id,
                             accession=citation.accession, url=citation.url, company=row['company'], filing_year=row['filing_year'],
                             form=row['form'], section=row['section']))
    for hop in result.hops:  # fast-mode quote selections must be verbatim inside their own ACCEPTED hop's evidence
        if hop.quote_selections and not hop.accepted:
            raise ProjectionError('quote selections recorded for a hop that was not accepted')
        context = {item.id: item for item in hop.evidence}
        for selection in hop.quote_selections:
            item = context.get(selection.chunk_id)
            if item is None or quote_span(item.text, selection.quote) is None:
                raise ProjectionError('recorded quote selection is not verbatim in its hop evidence')
    gaps = []
    for flag in result.flags:
        found = _SUBQUERY_FLAG.match(flag)
        gaps.append(dict(text=flag, subquery=int(found[1]) if found else None))
    plan = [dict(index=i, goal=q.goal, query=q.query, filters=q.filters.model_dump(), filing_resolved=bool(q.filters.accession))
            for i, q in enumerate(result.plan.subqueries)]
    hops = []
    for hop in result.hops:
        carried = set(hop.retained_evidence_ids)
        hops.append(dict(subquery=hop.subquery, attempt=hop.attempt, query=hop.query, goal=hop.requested_goal, score=hop.score,
                         accepted=hop.accepted, rationale=hop.rationale, proposed_rewrite=hop.proposed_rewrite,
                         terminal_reason=hop.terminal_reason, retrieved_count=len(hop.retrieved_ids),
                         context=[dict(id=i.id, origin='carried' if i.id in carried else 'fresh') for i in hop.evidence],
                         quote_selections=len(hop.quote_selections), quote_flags=len(hop.quote_flags),
                         elapsed_seconds=round(hop.elapsed_seconds, 3)))
    calls = [dict(schema=c.get('schema'), models=c.get('models'), seconds=round(float(c.get('seconds') or 0), 3), num_turns=c.get('num_turns'))
             for c in result.model_calls[:limits.MAX_MODEL_CALL_ROWS]]
    document = dict(prompt=result.prompt, elapsed_seconds=round(result.elapsed_seconds, 3), excerpts=excerpts, gaps=gaps, plan=plan,
                    hops=hops, evidence=list(rows.values()),
                    diagnostics=dict(execution_mode=result.execution_mode, synthesis_route=result.synthesis_route, states=[s.value for s in result.states], retrieval_calls=len(result.hops),
                                     retries=sum(h.attempt > 0 for h in result.hops), model_calls=calls))
    raw = json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode()
    if len(raw) > limits.MAX_RESULT_BYTES:
        raise ResultTooLarge('serialized result exceeds the bound')
    return raw
