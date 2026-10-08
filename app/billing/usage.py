"""Per-tenant usage for a billing period: summary, daily series, recent calls and a CSV statement.

Everything is read from the ledger (the source of truth for cost attribution) except spent/reserved,
which come from the budget row that admission control uses. Online-judge rows (D17) are attributed to
the tenant but paid by the platform: they are listed with billed=false and left out of every cost,
token and request total, so the totals reconcile with `spent`. Money stays integer micro-USD until the
response layer. Days are UTC calendar days on both SQLite and Postgres. OWNER: Naresh.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.billing import budget
from app.models import BudgetPeriod, Tenant, UsageLedger
from app.plans import Plan, load_plans, microusd_to_usd
from app.schemas import UsageSummary

LAST_REQUESTS = 10
UNBILLED_PURPOSES = ("judge",)  # platform cost, never settled against the tenant's budget (D17)
BILLED = UsageLedger.purpose.not_in(UNBILLED_PURPOSES)

STATEMENT_COLUMNS = (
    "created_at",
    "request_id",
    "key_id",
    "purpose",
    "model",
    "input_tokens",
    "output_tokens",
    "cost_microusd",
    "cost_usd",
    "price_version",
    "prompt_version",
    "status",
    "billed",
)


def period_bounds(period: str) -> tuple[datetime, datetime]:
    """Half-open [start, next_start) in UTC for a "YYYY-MM" period."""
    year, month = int(period[:4]), int(period[5:7])
    start = datetime(year, month, 1, tzinfo=UTC)
    next_start = datetime(year + (month // 12), (month % 12) + 1, 1, tzinfo=UTC)
    return start, next_start


def _in_period(tenant_id: str, period: str):
    start, next_start = period_bounds(period)
    return (
        (UsageLedger.tenant_id == tenant_id)
        & (UsageLedger.created_at >= start)
        & (UsageLedger.created_at < next_start)
    )


def _utc_day(db: Session):
    # Postgres date() of a timestamptz uses the session time zone; pin it to UTC. SQLite stores UTC.
    if db.get_bind().dialect.name == "postgresql":
        return func.date(func.timezone("UTC", UsageLedger.created_at))
    return func.date(UsageLedger.created_at)


def _iso(ts: datetime) -> str:
    return (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def summarize_usage(db: Session, tenant: Tenant, plan: Plan) -> UsageSummary:
    period = budget.current_period()
    bp = db.get(BudgetPeriod, (tenant.id, period))
    limit = bp.hard_limit_microusd if bp else budget.limit_for(tenant, plan)
    spent = bp.spent_microusd if bp else 0
    reserved = bp.reserved_microusd if bp else 0
    in_period = _in_period(tenant.id, period)

    totals = db.execute(
        select(
            func.count(func.distinct(UsageLedger.request_id)),
            func.coalesce(func.sum(UsageLedger.input_tokens), 0),
            func.coalesce(func.sum(UsageLedger.output_tokens), 0),
        ).where(in_period & BILLED)
    ).one()

    by_model = [
        {"model": m, "requests": n, "cost_usd": microusd_to_usd(int(c or 0))}
        for m, n, c in db.execute(
            select(UsageLedger.model, func.count(), func.sum(UsageLedger.cost_microusd))
            .where(in_period & BILLED)
            .group_by(UsageLedger.model)
        ).all()
    ]
    by_purpose = [
        {
            "purpose": p,
            "calls": n,
            "cost_usd": microusd_to_usd(int(c or 0)),
            "billed": p not in UNBILLED_PURPOSES,
        }
        for p, n, c in db.execute(
            select(UsageLedger.purpose, func.count(), func.sum(UsageLedger.cost_microusd))
            .where(in_period)
            .group_by(UsageLedger.purpose)
        ).all()
    ]
    day = _utc_day(db)
    by_day = [
        {"date": str(d), "requests": n, "cost_usd": microusd_to_usd(int(c or 0))}
        for d, n, c in db.execute(
            select(
                day,
                func.count(func.distinct(UsageLedger.request_id)),
                func.sum(UsageLedger.cost_microusd),
            )
            .where(in_period & BILLED)
            .group_by(day)
            .order_by(day)
        ).all()
    ]
    last_requests = [
        {
            "created_at": _iso(r.created_at),
            "request_id": r.request_id,
            "purpose": r.purpose,
            "model": r.model,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cost_usd": microusd_to_usd(r.cost_microusd),
            "status": r.status,
            "billed": r.purpose not in UNBILLED_PURPOSES,
        }
        for r in db.scalars(
            select(UsageLedger)
            .where(UsageLedger.tenant_id == tenant.id)
            .order_by(UsageLedger.created_at.desc(), UsageLedger.id.desc())
            .limit(LAST_REQUESTS)
        )
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
        by_day=by_day,
        last_requests=last_requests,
    )


def statement_csv(db: Session, tenant: Tenant, period: str) -> str:
    """Every ledger row of the period, oldest first, with a TOTAL row that equals the bill
    (billed rows only)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(STATEMENT_COLUMNS)
    total = 0
    for r in db.scalars(
        select(UsageLedger)
        .where(_in_period(tenant.id, period))
        .order_by(UsageLedger.created_at, UsageLedger.id)
    ):
        billed = r.purpose not in UNBILLED_PURPOSES
        total += r.cost_microusd if billed else 0
        writer.writerow(
            (
                _iso(r.created_at),
                r.request_id,
                r.key_id or "",
                r.purpose,
                r.model,
                r.input_tokens,
                r.output_tokens,
                r.cost_microusd,
                f"{microusd_to_usd(r.cost_microusd):.6f}",
                r.price_version,
                r.prompt_version or "",
                r.status,
                "yes" if billed else "no",
            )
        )
    writer.writerow(
        ("TOTAL", "", "", "", "", "", "", total, f"{microusd_to_usd(total):.6f}", "", "", "", "")
    )
    return out.getvalue()
