"""Persists the redacted request/response record for a call."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.compliance.redaction import redact
from app.models import RequestLog


def write_request_log(
    db: Session,
    *,
    request_id: str,
    tenant_id: str | None,
    key_id: str | None,
    endpoint: str,
    status_code: int,
    latency_ms: int,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_microusd: int = 0,
    raw_input: str | None = None,
    raw_output: str | None = None,
    guardrails: dict | None = None,
    error_code: str | None = None,
) -> RequestLog:
    rin = redact(raw_input)
    rout = redact(raw_output)
    counts = {f"input.{k}": v for k, v in rin.counts.items()}
    counts.update({f"output.{k}": v for k, v in rout.counts.items()})
    row = RequestLog(
        request_id=request_id,
        tenant_id=tenant_id,
        key_id=key_id,
        endpoint=endpoint,
        status_code=status_code,
        latency_ms=latency_ms,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_microusd=cost_microusd,
        redacted_input=rin.text[:20_000] if raw_input else None,
        redacted_output=rout.text[:20_000] if raw_output else None,
        redaction_counts=counts or None,
        guardrails=guardrails,
        error_code=error_code,
    )
    db.add(row)
    return row
