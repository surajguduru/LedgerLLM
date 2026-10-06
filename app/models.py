"""Database schema — the shared contract between all workstreams.

Design notes (see docs/DESIGN.md):
- All money is stored as integer micro-USD (1 USD = 1_000_000 uUSD). No floats, exact sums, atomic increments.
- IDs are UUID strings: portable across SQLite/Postgres and not guessable across tenants.
- Every table that holds tenant data carries tenant_id so every query can be tenant-scoped.

Adding a column: edit here, note it in your PR description. Dev DBs are recreated with create_all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="free")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )  # active|suspended
    budget_override_microusd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="default")
    key_prefix: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # shown in UIs, never the secret
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RateLimitWindow(Base):
    """Fixed-window counter per key. Owner: Suraj. Row = (key, minute bucket) -> count."""

    __tablename__ = "rate_limit_windows"

    key_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    window_start: Mapped[int] = mapped_column(
        BigInteger, primary_key=True
    )  # epoch seconds, floored
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class BudgetPeriod(Base):
    """Monthly budget state per tenant. Reserve-then-settle keeps bursts from overspending."""

    __tablename__ = "budget_periods"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # "YYYY-MM" (UTC)
    hard_limit_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    spent_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    soft_warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageLedger(Base):
    """One row per billable LLM call. This is the source of truth for cost attribution."""

    __tablename__ = "usage_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)  # completion|guardrail|judge
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    price_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ok"
    )  # ok|error|blocked|cached


Index("ix_usage_ledger_tenant_created", UsageLedger.tenant_id, UsageLedger.created_at)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResponseCache(Base):
    """Exact-match response cache. Owner: Suraj. Key = sha256(tenant, model, prompt hash, options, text)."""

    __tablename__ = "response_cache"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class AuditEvent(Base):
    """Structured audit log: who did what to which tenant, and why a request was refused."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor: Mapped[str] = mapped_column(
        String(20), nullable=False, default="system"
    )  # system|admin|tenant
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class RequestLog(Base):
    """Redacted request/response log. Owner: Loukik. Never store raw PII here."""

    __tablename__ = "request_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    endpoint: Mapped[str] = mapped_column(String(100), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    redacted_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    redacted_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    redaction_counts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    guardrails: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Feedback(Base):
    """Thumbs up/down per request — the 'online' signal in our evaluation plan."""

    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rating: Mapped[str] = mapped_column(String(8), nullable=False)  # up|down
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
