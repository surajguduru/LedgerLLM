"""Exact-match response cache per tenant.

OWNER: Suraj. Stage 4½ of the pipeline, after content is acquired and before the budget reserve, so a hit
never touches the budget and costs no model call. The key covers tenant, model, prompt content hash, style,
max_words, instructions, the page title and the extracted text: a changed page, a new prompt version or a different model is
a miss by construction, and entries are never shared across tenants.

Billing policy (docs/DESIGN.md D16): a hit is free to the tenant. It is still booked as a ledger row with
cost 0, zero tokens and status "cached", so request counts stay honest while token totals keep matching what
the provider actually served. Entries live for the plan's `cache_ttl_s`; expired rows are ignored on lookup
and deleted opportunistically on about 1 % of stores.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import ResponseCache, utcnow
from app.traffic import dialect_insert

PURGE_PROBABILITY = 0.01


@dataclass
class CachedSummary:
    text: str
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int


def cache_key(
    *,
    tenant_id: str,
    model: str,
    prompt_hash: str,
    style: str,
    max_words: int,
    instructions: str | None,
    text: str,
    guardrail_version: str = "",
    title: str = "",
) -> str:
    """`guardrail_version` (rules hash + mode) is part of the key: a hit skips the guardrails, so an
    entry cached before a rule change or under GUARDRAILS_MODE=off must not be served afterwards."""
    raw = "\x1f".join(
        [
            tenant_id,
            model,
            prompt_hash,
            style,
            str(max_words),
            instructions or "",
            text,
            guardrail_version,
            title,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lookup(db: Session, tenant_id: str, key: str) -> CachedSummary | None:
    row = db.scalar(
        select(ResponseCache).where(
            ResponseCache.tenant_id == tenant_id,
            ResponseCache.cache_key == key,
            ResponseCache.expires_at > utcnow(),
        )
    )
    if row is None:
        return None
    db.execute(
        update(ResponseCache)
        .where(ResponseCache.tenant_id == tenant_id, ResponseCache.cache_key == key)
        .values(hits=ResponseCache.hits + 1)
    )
    return CachedSummary(
        text=row.summary,
        model=row.model,
        prompt_version=row.prompt_version,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
    )


def store(db: Session, tenant_id: str, key: str, summary: CachedSummary, *, ttl_s: int) -> None:
    """Upsert, so two identical requests that both missed cannot collide on the primary key."""
    if ttl_s <= 0:
        return
    now = utcnow()
    values = {
        "summary": summary.text,
        "model": summary.model,
        "prompt_version": summary.prompt_version,
        "input_tokens": summary.input_tokens,
        "output_tokens": summary.output_tokens,
        "created_at": now,
        "expires_at": now + timedelta(seconds=ttl_s),
    }
    stmt = (
        dialect_insert(db)(ResponseCache)
        .values(tenant_id=tenant_id, cache_key=key, hits=0, **values)
        .on_conflict_do_update(
            index_elements=[ResponseCache.tenant_id, ResponseCache.cache_key], set_=values
        )
    )
    db.execute(stmt)
    if random.random() < PURGE_PROBABILITY:
        purge_expired(db)


def purge_expired(db: Session) -> int:
    return (
        db.execute(delete(ResponseCache).where(ResponseCache.expires_at <= utcnow())).rowcount or 0
    )
