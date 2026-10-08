from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth.dependency import AuthContext, get_auth_context
from app.billing.budget import current_period
from app.billing.usage import statement_csv, summarize_usage
from app.db import get_db
from app.schemas import UsageSummary

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage", response_model=UsageSummary)
def usage(
    db: Session = Depends(get_db), auth: AuthContext = Depends(get_auth_context)
) -> UsageSummary:
    return summarize_usage(db, auth.tenant, auth.plan)


@router.get("/usage/statement.csv", response_class=Response)
def statement(
    period: str | None = Query(
        None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM (UTC); default: current"
    ),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
) -> Response:
    """Itemised statement: one line per ledger row of the period, plus a TOTAL line."""
    period = period or current_period()
    return Response(
        content=statement_csv(db, auth.tenant, period),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="ledgerllm-{auth.tenant.id}-{period}.csv"'
        },
    )
