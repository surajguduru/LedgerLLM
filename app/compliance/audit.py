"""Structured audit events. One row per security- or billing-relevant decision.

Audit policy (owner: Loukik): refusals caused by the *tenant* are audited;
infrastructure noise is logged only (request log, no audit row).

Audited: rate_limited, budget_exceeded, blocked_input, output_moderated,
model_not_allowed (tenant requested a model outside their plan),
fetch_blocked (tenant-supplied URL rejected by the SSRF guard),
idempotency_conflict, feedback, key/tenant lifecycle, guardrail shadow.
Logged only: fetch_failed / upstream_error transport failures after the
SSRF check (recorded in the request log with error_code; UPSTREAM_ERROR
is still audited for provider 5xx/timeouts because it affects billing).
Audit details are PII-redacted via redact_dict before persistence.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.compliance.redaction import redact_dict
from app.models import AuditEvent

# Event type catalog — add here, reference by constant.
TENANT_CREATED = "tenant.created"
TENANT_UPDATED = "tenant.updated"
KEY_CREATED = "key.created"
KEY_REVOKED = "key.revoked"
KEY_ROTATED = "key.rotated"
RATE_LIMITED = "request.rate_limited"
BUDGET_EXCEEDED = "request.budget_exceeded"
BUDGET_SOFT_WARNING = "budget.soft_warning"
BLOCKED_INPUT = "request.blocked_input"
OUTPUT_MODERATED = "request.output_moderated"
MODEL_NOT_ALLOWED = "request.model_not_allowed"
FETCH_BLOCKED = "request.fetch_blocked"
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
            details=redact_dict(details) if details else None,
        )
    )
