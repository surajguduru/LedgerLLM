"""Pydantic request/response models — the public API contract. Changes here need a PR comment to Suraj."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

Style = Literal["bullets", "paragraph", "tldr"]


class SummarizeRequest(BaseModel):
    url: HttpUrl | None = Field(None, description="Public http(s) URL to fetch and summarize")
    text: str | None = Field(None, description="Raw text to summarize (alternative to url)")
    title: str | None = Field(None, max_length=300)
    instructions: str | None = Field(
        None, max_length=500, description="Optional focus, e.g. 'focus on pricing changes'"
    )
    style: Style = "bullets"
    max_words: int = Field(150, ge=20, le=600)
    model: str | None = Field(
        None, description="Override model; must be allowed by the tenant's plan"
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> SummarizeRequest:
        if bool(self.url) == bool(self.text):
            raise ValueError("provide exactly one of 'url' or 'text'")
        return self


class SourceInfo(BaseModel):
    url: str | None
    title: str
    chars: int  # characters the summary was made from
    truncated: bool  # True when part of the document was left out
    # full: the whole text in one call · head_tail: head and tail kept, middle omitted ·
    # map_reduce: summarised in chunks, then the chunk summaries summarised (D20)
    strategy: Literal["full", "head_tail", "map_reduce"] = "full"


class UsageInfo(BaseModel):
    model: str
    prompt_version: str
    price_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    cached: bool = False
    provider: str | None = None  # provider that answered, e.g. "gemini"
    fallback_from: str | None = None  # set when the fallback model answered: the model that failed


class BudgetInfo(BaseModel):
    period: str
    limit_usd: float
    spent_usd: float
    remaining_usd: float
    warning: bool


class GuardrailsInfo(BaseModel):
    mode: str
    input: dict
    output: dict


class SummarizeResponse(BaseModel):
    request_id: str
    summary: str
    source: SourceInfo
    usage: UsageInfo
    budget: BudgetInfo
    guardrails: GuardrailsInfo


class FeedbackRequest(BaseModel):
    request_id: str = Field(..., max_length=64)
    rating: Literal["up", "down"]
    comment: str | None = Field(None, max_length=1000)


class UsageSummary(BaseModel):
    tenant_id: str
    tenant_name: str
    plan: str
    period: str
    limit_usd: float
    spent_usd: float
    reserved_usd: float
    remaining_usd: float
    warning: bool
    requests: int
    input_tokens: int
    output_tokens: int
    by_model: list[dict]
    by_purpose: list[dict]
    by_day: list[dict] = []  # [{date: "YYYY-MM-DD" (UTC), requests, cost_usd}], oldest first
    last_requests: list[dict] = []  # the 10 most recent ledger rows, newest first


# --- admin -----------------------------------------------------------------------------------


class CreateTenantRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    plan: str = "free"
    budget_override_usd: float | None = Field(None, ge=0)
    key_name: str = "default"


class TenantOut(BaseModel):
    id: str
    name: str
    plan: str
    status: str
    budget_override_usd: float | None
    created_at: datetime


class ApiKeyOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    key_prefix: str
    created_at: datetime
    revoked_at: datetime | None


class CreateTenantResponse(BaseModel):
    tenant: TenantOut
    key: ApiKeyOut
    api_key: str = Field(
        ..., description="The raw key. Shown exactly once; we store only its hash."
    )


class CreateKeyRequest(BaseModel):
    name: str = "default"


class CreateKeyResponse(BaseModel):
    key: ApiKeyOut
    api_key: str


class UpdateTenantRequest(BaseModel):
    plan: str | None = None
    status: str | None = None
    budget_override_usd: float | None = Field(None, ge=0)


class AuditOut(BaseModel):
    id: str
    created_at: datetime
    tenant_id: str | None
    key_id: str | None
    request_id: str | None
    actor: str
    event_type: str
    details: dict | None = None
