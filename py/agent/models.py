"""Validated wire contracts; model output is never executed as code."""
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from .filters import FilingFilter


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class State(str, Enum):
    PLAN = 'Plan'
    RETRIEVE_EVALUATE = 'Retrieve_Evaluate'
    SYNTHESIZE = 'Synthesize'


GOAL_MAX = 400


class SubQuery(StrictModel):
    query: str = Field(min_length=4, max_length=1000)  # retrieval hint; may expand lexical terms, never requirements
    # What the USER asked for this topic: a contiguous span of the audit prompt (validated by the planner and the
    # offline verifier). '' only in legacy saved plans, where the critic falls back to `query`.
    goal: str = Field(default='', max_length=GOAL_MAX)
    filters: FilingFilter


class Plan(StrictModel):
    subqueries: list[SubQuery] = Field(min_length=2, max_length=3)


class Critique(StrictModel):
    score: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1, max_length=1000)
    rewritten_query: str = Field(min_length=4, max_length=1000)
    retain_ids: list[str] = Field(default_factory=list, max_length=2)


MAX_CRITIC_CLAIMS = 4
HOP_QUOTE_MIN, HOP_QUOTE_MAX = 40, 2000


class CriticClaim(StrictModel):
    """Critic-proposed quote bound to a short in-context handle (E01..E05). Deliberately lenient on length: the host, not the
    wire schema, enforces 40..2000 characters, so one malformed quote is dropped and flagged instead of failing the whole call."""
    ref: str = Field(min_length=1, max_length=64)
    quote: str = Field(min_length=1, max_length=4000)


class CritiqueWithQuotes(StrictModel):
    """Fast-mode WIRE proposal (the schema the model sees; ledger name 'CritiqueWithQuotes'). It never contains canonical passage IDs:
    retention names passages by the same short handles (E01..E05) as the quotes. The host turns it into a CriticVerdict."""
    score: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1, max_length=1000)
    rewritten_query: str = Field(min_length=4, max_length=1000)
    retain_refs: list[str] = Field(default_factory=list, max_length=2)
    claims: list[CriticClaim] = Field(default_factory=list, max_length=MAX_CRITIC_CLAIMS)


class CriticVerdict(Critique):
    """Host verdict of a fast critic call: the legacy Critique fields with retain_ids already resolved from exact handles to canonical IDs,
    plus the still-untrusted quote proposals (grounded by the controller only for an accepted hop)."""
    claims: list[CriticClaim] = Field(default_factory=list, max_length=MAX_CRITIC_CLAIMS)


class QuoteClaim(StrictModel):
    chunk_id: str = Field(min_length=1, max_length=256)
    quote: str = Field(min_length=40, max_length=2000)


class HandleClaim(StrictModel):
    """Model-facing claim: an opaque short handle bound by the host, never a canonical ID."""
    ref: str = Field(min_length=1, max_length=64)
    quote: str = Field(min_length=40, max_length=2000)


class HandleDraft(StrictModel):
    claims: list[HandleClaim] = Field(max_length=12)


class Draft(StrictModel):
    claims: list[QuoteClaim] = Field(max_length=12)


class Evidence(StrictModel):
    id: str
    text: str = Field(min_length=1, max_length=32768)
    metadata: dict[str, Any]
    accession: str
    url: str
    tag: str


class HopQuote(StrictModel):
    """A critic-selected quote that the HOST verified verbatim inside this hop's own evidence (never an unverified proposal)."""
    chunk_id: str = Field(min_length=1, max_length=256)
    quote: str = Field(min_length=HOP_QUOTE_MIN, max_length=HOP_QUOTE_MAX)


class Hop(StrictModel):
    subquery: int
    attempt: int
    query: str
    requested_goal: str = Field(default='', max_length=GOAL_MAX)  # goal the critic graded against ('' = legacy)
    filters: FilingFilter
    score: int = Field(ge=1, le=5)
    rationale: str
    evidence: list[Evidence] = Field(max_length=5)
    accepted: bool
    retained_evidence_ids: list[str] = Field(default_factory=list, max_length=2)  # passages ACTUALLY carried into this context
    # Optional selection trace (older saved results omit it): nominees from the previous critic, the raw
    # reranked IDs returned by retrieval (<=10) and the source-verified fresh IDs in rank order (<=5).
    nominated_retained_ids: list[str] = Field(default_factory=list, max_length=2)
    # The critic's actual proposed next query for a non-accepted hop ('' when accepted). terminal_reason marks the ONLY
    # early stop: an empty-evidence hop, before the final attempt, whose critic returned the unchanged query.
    proposed_rewrite: str = Field(default='', max_length=1000)
    terminal_reason: Literal['stalled_rewrite'] | None = None
    retrieved_ids: list[str] = Field(default_factory=list, max_length=10)
    fresh_verified_ids: list[str] = Field(default_factory=list, max_length=5)
    # Fast mode only (empty in legacy): host-verified quote selections of an ACCEPTED hop, and bounded drop flags that never contain
    # the rejected text. A below-threshold hop never records or publishes quotes.
    quote_selections: list[HopQuote] = Field(default_factory=list, max_length=MAX_CRITIC_CLAIMS)
    quote_flags: list[str] = Field(default_factory=list, max_length=MAX_CRITIC_CLAIMS)
    elapsed_seconds: float


class Citation(StrictModel):
    tag: str
    chunk_id: str
    accession: str
    url: str
    quote: str


class AgentResult(StrictModel):
    prompt: str
    states: list[State]
    plan: Plan
    hops: list[Hop]
    markdown: str
    citations: list[Citation]
    flags: list[str]
    model_calls: list[dict[str, Any]]
    elapsed_seconds: float
    # What actually ran. Saved results from before fast mode lack both keys and mean the legacy path.
    execution_mode: Literal['fast', 'legacy'] = 'legacy'
    synthesis_route: Literal['host_quotes', 'fallback', 'legacy', 'none'] = 'legacy'
