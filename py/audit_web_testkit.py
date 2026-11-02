"""TEST FIXTURES AND TEST DOUBLES ONLY. Synthetic passages, scripted models and scripted backends used to check the
workbench's contracts (admission, binding, security). Nothing here is SEC data and nothing measured with it is a retrieval,
model-quality or latency result."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent.filters import FilingFilter
from agent.models import AgentResult, Citation, Critique, Evidence, HandleDraft, HandleClaim, Hop, Plan, State, SubQuery

ACC = '000000000000000007'
ACC_DASH = '0000000000-00-000007'
URL = f'https://www.sec.gov/Archives/edgar/data/1234567/{ACC}/fixture-10k.htm'
TEXT_A = 'Our  fixture supplier\nconcentration is high: three vendors provide most critical components for the fixture products.'
TEXT_B = 'Fixture export controls could restrict shipments of the fixture products to certain regions of the world.'
QUOTE_A = 'Our fixture supplier concentration is high: three vendors provide most critical components'  # whitespace-normalized sub-span of TEXT_A
QUOTE_B = 'Fixture export controls could restrict shipments of the fixture products to certain regions'
PROMPT = 'Audit TST filing year 2024: retrieve supplier concentration and export controls.'


def evidence(n: int, text: str, section: str = 'Item 1A') -> Evidence:
    identifier = f'{ACC}#item1A#passage-{n:06}'
    meta = dict(ID=identifier, Ticker='TST', CIK='1234567', FilingYear=2024, FormType='10-K', SectionItem=section)
    return Evidence(id=identifier, text=text, metadata=meta, accession=ACC_DASH, url=URL, tag=f'TST-2024-10K-item1A-p{n:06}')


def hop(subquery: int, attempt: int, items: list[Evidence], *, accepted: bool = True, retained=(), score: int = 4) -> Hop:
    return Hop(subquery=subquery, attempt=attempt, query='fixture query', requested_goal='supplier concentration',
               filters=FilingFilter(ticker='TST', year=2024, accession=ACC), score=score, rationale='fixture rationale', evidence=items,
               accepted=accepted, retained_evidence_ids=list(retained), proposed_rewrite='' if accepted else 'next wording',
               retrieved_ids=[i.id for i in items], fresh_verified_ids=[i.id for i in items], elapsed_seconds=0.25)


def citation(item: Evidence, quote: str) -> Citation:
    return Citation(tag=item.tag, chunk_id=item.id, accession=item.accession, url=item.url, quote=quote)


def agent_result(*, citations=None, flags=None, hops=None, plan=None) -> AgentResult:
    a, b = evidence(1, TEXT_A), evidence(2, TEXT_B)
    filters = FilingFilter(ticker='TST', year=2024, accession=ACC)
    plan = plan or Plan(subqueries=[SubQuery(query='supplier concentration', goal='supplier concentration', filters=filters),
                                    SubQuery(query='export controls', goal='export controls', filters=filters)])
    hops = hops if hops is not None else [hop(0, 0, [a]), hop(1, 0, [b])]
    citations = citations if citations is not None else [citation(a, QUOTE_A), citation(b, QUOTE_B)]
    return AgentResult(prompt=PROMPT, states=[State.PLAN, State.RETRIEVE_EVALUATE, State.SYNTHESIZE], plan=plan, hops=hops,
                       markdown='unused by the workbench', citations=citations, flags=flags or [],
                       model_calls=[dict(schema='Plan', models=['test-double'], seconds=0.01, num_turns=1)], elapsed_seconds=1.5)


class BlockingAgent:
    """Runs until released; records whether cancellation cleanup completed (a stand-in for killing model process groups)."""
    def __init__(self, result_bytes_source=None, cleanup_gate: asyncio.Event | None = None):
        self.started, self.release, self.cleaned = asyncio.Event(), asyncio.Event(), False
        self.cleanup_gate, self.runs = cleanup_gate, 0

    async def run(self, prompt: str):
        self.runs += 1
        self.started.set()
        try:
            await self.release.wait()
            return agent_result()
        finally:
            if self.cleanup_gate is not None:
                await self.cleanup_gate.wait()  # cleanup that takes time; the slot must stay held meanwhile
            self.cleaned = True


class InstantAgent:
    def __init__(self, error: BaseException | None = None):
        self.error = error

    async def run(self, prompt: str):
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return agent_result()


def write_fixture_corpus(directory: Path) -> tuple[Path, Path]:
    """Tiny synthetic corpus + enrolled-style provenance manifest for the real ProvenanceRegistry."""
    corpus = directory / 'chunks.jsonl'
    lines = []
    for n, text in ((1, TEXT_A), (2, TEXT_B)):
        item = evidence(n, text)
        lines.append(json.dumps(dict(item.metadata, Text=text, Tokens=[])))
    first = dict(evidence(0, 'ANNUAL REPORT for the fiscal year ended December 31, 2023 fixture cover page.').metadata,
                 Text='ANNUAL REPORT for the fiscal year ended December 31, 2023 fixture cover page.', Tokens=[])
    corpus.write_text('\n'.join([json.dumps(first)] + lines) + '\n')
    manifest = directory / 'provenance.json'
    manifest.write_text(json.dumps(dict(corpus_sha256=hashlib.sha256(corpus.read_bytes()).hexdigest(), sources=[
        dict(ticker='TST', cik='1234567', filing_year=2024, form='10-K', url=URL, accession=ACC, raw_sha256='0' * 64, fiscal_period_end='2023-12-31')])))
    return corpus, manifest


class ScriptedModel:
    """Scripted StructuredModel: Plan -> two scopes, Critique -> fixed score, HandleDraft -> exact quotes of E01/E02."""
    def __init__(self, second_ticker: str = 'TST'):
        self.calls, self.second_ticker = [], second_ticker

    async def generate(self, schema, instruction, payload):
        self.calls.append(dict(schema=schema.__name__, models=['test-double'], seconds=0.0, num_turns=1))
        if schema is Plan:
            return Plan(subqueries=[SubQuery(query='supplier concentration vendors', goal='supplier concentration', filters=FilingFilter(ticker='TST', year=2024)),
                                    SubQuery(query='export controls shipments', goal='export controls', filters=FilingFilter(ticker=self.second_ticker, year=2024))])
        if schema is Critique:
            has = bool(payload['evidence'])
            return Critique(score=4 if has else 1, rationale='fixture critic', rewritten_query=payload['search_query'] if not has else 'rewrite words', retain_ids=[])
        quotes = {'supplier': QUOTE_A, 'export': QUOTE_B}
        claims = [HandleClaim(ref=view['ref'], quote=quotes['supplier' if 'supplier' in view['text'] else 'export']) for view in payload['evidence']]
        return HandleDraft(claims=claims)


class ScriptedBackend:
    """Returns the fixture passage matching the requested ticker, or nothing (an unavailable filing)."""
    async def search(self, query, filters):
        if filters.ticker != 'TST':
            return []
        item = evidence(1 if 'supplier' in query else 2, TEXT_A if 'supplier' in query else TEXT_B)
        return [dict(id=item.id, text=item.text, metadata=item.metadata)]
