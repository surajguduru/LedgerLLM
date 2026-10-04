"""Writes the per-call cost attribution row. Everything billable goes through here."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import UsageLedger


def book(
    db: Session,
    *,
    tenant_id: str,
    key_id: str | None,
    request_id: str,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_microusd: int,
    price_version: str,
    prompt_version: str | None,
    latency_ms: int,
    status: str = "ok",
) -> UsageLedger:
    row = UsageLedger(
        tenant_id=tenant_id,
        key_id=key_id,
        request_id=request_id,
        purpose=purpose,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_microusd=cost_microusd,
        price_version=price_version,
        prompt_version=prompt_version,
        latency_ms=latency_ms,
        status=status,
    )
    db.add(row)
    return row
