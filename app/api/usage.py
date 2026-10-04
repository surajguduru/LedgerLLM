from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.dependency import AuthContext, get_auth_context
from app.billing.usage import summarize_usage
from app.db import get_db
from app.schemas import UsageSummary

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage", response_model=UsageSummary)
def usage(
    db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth_context)
) -> UsageSummary:
    return summarize_usage(db, auth.tenant, auth.plan)
