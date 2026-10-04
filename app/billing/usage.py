"""Per-tenant usage summary for the current period. OWNER: Naresh (extend: by-day series, CSV export)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.billing.budget import current_period, limit_for
from app.models import BudgetPeriod, Tenant, UsageLedger
from app.plans import Plan, load_plans, microusd_to_usd
from app.schemas import UsageSummary


def summarize_usage(db: Session, tenant: Tenant, plan: Plan) -> UsageSummary:
    period = current_period()
    bp = db.get(BudgetPeriod, (tenant.id, period))
    limit = bp.hard_limit_microusd if bp else limit_for(tenant, plan)
    spent = bp.spent_microusd if bp else 0
    reserved = bp.reserved_microusd if bp else 0

    # Month-scoped, portable (SQLite + Postgres): half-open [period_start, next_period_start).
    year, month = int(period[:4]), int(period[5:7])
    period_start = datetime(year, month, 1, tzinfo=UTC)
    next_start = datetime(year + (month // 12), (month % 12) + 1, 1, tzinfo=UTC)
    in_period = (
        (UsageLedger.tenant_id == tenant.id)
        & (UsageLedger.created_at >= period_start)
        & (UsageLedger.created_at < next_start)
    )

    totals = db.execute(
        select(
            func.count(func.distinct(UsageLedger.request_id)),
            func.coalesce(func.sum(UsageLedger.input_tokens), 0),
            func.coalesce(func.sum(UsageLedger.output_tokens), 0),
        ).where(in_period)
    ).one()

    by_model = [
        {"model": m, "requests": n, "cost_usd": microusd_to_usd(c or 0)}
        for m, n, c in db.execute(
            select(UsageLedger.model, func.count(), func.sum(UsageLedger.cost_microusd))
            .where(in_period)
            .group_by(UsageLedger.model)
        ).all()
    ]
    by_purpose = [
        {"purpose": p, "calls": n, "cost_usd": microusd_to_usd(c or 0)}
        for p, n, c in db.execute(
            select(UsageLedger.purpose, func.count(), func.sum(UsageLedger.cost_microusd))
            .where(in_period)
            .group_by(UsageLedger.purpose)
        ).all()
    ]

    return UsageSummary(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        plan=tenant.plan,
        period=period,
        limit_usd=microusd_to_usd(limit),
        spent_usd=microusd_to_usd(spent),
        reserved_usd=microusd_to_usd(reserved),
        remaining_usd=microusd_to_usd(max(0, limit - spent - reserved)),
        warning=(spent + reserved) >= load_plans().soft_warning_fraction * limit,
        requests=int(totals[0]),
        input_tokens=int(totals[1]),
        output_tokens=int(totals[2]),
        by_model=by_model,
        by_purpose=by_purpose,
    )
