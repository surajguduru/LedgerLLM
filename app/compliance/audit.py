"""Structured audit events. One row per security- or billing-relevant decision."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditEvent

# Event type catalog — add here, reference by constant.
TENANT_CREATED = "tenant.created"
KEY_CREATED = "key.created"
KEY_REVOKED = "key.revoked"
RATE_LIMITED = "request.rate_limited"
BUDGET_EXCEEDED = "request.budget_exceeded"
BUDGET_SOFT_WARNING = "budget.soft_warning"
BLOCKED_INPUT = "request.blocked_input"
OUTPUT_MODERATED = "request.output_moderated"
UPSTREAM_ERROR = "request.upstream_error"
GUARDRAIL_SHADOW = "guardrail.shadow_block"
IDEMPOTENCY_CONFLICT = "request.idempotency_conflict"
FEEDBACK = "request.feedback"


def audit(
    db: Session,
    event_type: str,
    *,
    tenant_id: str | None = None,
    key_id: str | None = None,
    request_id: str | None = None,
    actor: str = "system",
    details: dict | None = None,
) -> None:
    db.add(
        AuditEvent(
            tenant_id=tenant_id,
            key_id=key_id,
            request_id=request_id,
            actor=actor,
            event_type=event_type,
            details=details,
        )
    )
