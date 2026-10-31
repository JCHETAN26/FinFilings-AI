"""Validated wire contracts; model output is never executed as code."""
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from .filters import FilingFilter


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class State(str, Enum):
    PLAN = 'Plan'
    RETRIEVE_EVALUATE = 'Retrieve_Evaluate'
    SYNTHESIZE = 'Synthesize'


class SubQuery(StrictModel):
    query: str = Field(min_length=4, max_length=1000)
    filters: FilingFilter


class Plan(StrictModel):
    subqueries: list[SubQuery] = Field(min_length=2, max_length=3)


class Critique(StrictModel):
    score: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1, max_length=1000)
    rewritten_query: str = Field(min_length=4, max_length=1000)


class QuoteClaim(StrictModel):
    chunk_id: str = Field(min_length=1, max_length=256)
    quote: str = Field(min_length=40, max_length=2000)


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
    filters: FilingFilter
    score: int = Field(ge=1, le=5)
    rationale: str
    evidence: list[Evidence] = Field(max_length=5)
    accepted: bool
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
