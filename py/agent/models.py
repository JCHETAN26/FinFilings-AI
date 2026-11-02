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
