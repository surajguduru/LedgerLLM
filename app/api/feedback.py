"""POST /v1/feedback — thumbs up/down on a request id. Our online quality signal."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependency import AuthContext, get_auth_context
from app.compliance import audit as audit_events
from app.compliance.audit import audit
from app.db import get_db
from app.errors import ApiError
from app.models import Feedback, RequestLog, UsageLedger
from app.observability import metrics
from app.schemas import FeedbackRequest

router = APIRouter(prefix="/v1", tags=["feedback"])

UNKNOWN_PROMPT = "unknown"


def _prompt_version(db: Session, tenant_id: str, request_id: str) -> str:
    """Which prompt produced the summary being rated.

    The rating arrives with only a request_id, but request_logs does not store the prompt version --
    the ledger does. One lookup on the completion row gives us the label, so the feedback metric can
    be sliced per prompt version. Guardrail/judge rows are skipped: they did not write the summary.
    """
    return (
        db.scalar(
            select(UsageLedger.prompt_version).where(
                UsageLedger.request_id == request_id,
                UsageLedger.tenant_id == tenant_id,
                UsageLedger.purpose == "completion",
            )
        )
        or UNKNOWN_PROMPT
    )


@router.post("/feedback", status_code=201)
def feedback(
    payload: FeedbackRequest,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    owned = db.scalar(
        select(RequestLog.id).where(
            RequestLog.request_id == payload.request_id, RequestLog.tenant_id == auth.tenant.id
        )
    )
    if owned is None:
        raise ApiError(404, "request_not_found", "no such request for this tenant")
    db.add(
        Feedback(
            tenant_id=auth.tenant.id,
            request_id=payload.request_id,
            rating=payload.rating,
            comment=payload.comment,
        )
    )
    audit(
        db,
        audit_events.FEEDBACK,
        tenant_id=auth.tenant.id,
        key_id=auth.api_key.id,
        request_id=payload.request_id,
        actor="tenant",
        details={"rating": payload.rating},
    )
    metrics.FEEDBACK.labels(
        payload.rating, _prompt_version(db, auth.tenant.id, payload.request_id)
    ).inc()
    db.commit()
    return {"ok": True}
