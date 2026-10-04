"""Monthly budget enforcement with reserve -> settle accounting.

Why reserve-then-settle: we only know the real cost after the LLM responds, but a burst of
concurrent requests must not all pass a check against a stale 'spent' value. So each request
atomically reserves its worst-case cost up front (one UPDATE ... WHERE spent+reserved+est <= limit),
and after the call we settle the actual cost and release the reservation.

OWNER: Naresh. Base ships a working atomic reserve/settle. Naresh owns: concurrency tests
(tests/test_budget.py), soft-warning audit event exactly once per period, period rollover,
usage summary + dashboard (app/api/usage.py, app/api/dashboard.py), and the cost-per-request numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import BudgetPeriod, Tenant, utcnow
from app.plans import Plan, load_plans


def current_period(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def limit_for(tenant: Tenant, plan: Plan) -> int:
    if tenant.budget_override_microusd is not None:
        return tenant.budget_override_microusd
    return plan.monthly_budget_microusd


@dataclass
class BudgetDecision:
    allowed: bool
    period: str
    limit_microusd: int
    spent_microusd: int
    reserved_microusd: int
    warning: bool

    @property
    def remaining_microusd(self) -> int:
        return max(0, self.limit_microusd - self.spent_microusd - self.reserved_microusd)


def _ensure_period(db: Session, tenant_id: str, period: str, limit: int) -> BudgetPeriod:
    bp = db.get(BudgetPeriod, (tenant_id, period))
    if bp is None:
        bp = BudgetPeriod(tenant_id=tenant_id, period=period, hard_limit_microusd=limit)
        db.add(bp)
        try:
            db.commit()
        except IntegrityError:  # lost a race with a concurrent request; fine
            db.rollback()
            bp = db.get(BudgetPeriod, (tenant_id, period))
            assert bp is not None
    if bp.hard_limit_microusd != limit:  # plan or override changed mid-month
        bp.hard_limit_microusd = limit
        db.commit()
    return bp


def reserve(db: Session, tenant: Tenant, plan: Plan, est_microusd: int) -> BudgetDecision:
    period = current_period()
    limit = limit_for(tenant, plan)
    _ensure_period(db, tenant.id, period, limit)

    stmt = (
        update(BudgetPeriod)
        .where(
            BudgetPeriod.tenant_id == tenant.id,
            BudgetPeriod.period == period,
            BudgetPeriod.spent_microusd + BudgetPeriod.reserved_microusd + est_microusd
            <= BudgetPeriod.hard_limit_microusd,
        )
        .values(
            reserved_microusd=BudgetPeriod.reserved_microusd + est_microusd, updated_at=utcnow()
        )
    )
    allowed = db.execute(stmt).rowcount == 1
    db.commit()

    bp = db.get(BudgetPeriod, (tenant.id, period))
    db.refresh(bp)
    committed = bp.spent_microusd + bp.reserved_microusd
    warning = committed >= load_plans().soft_warning_fraction * bp.hard_limit_microusd
    return BudgetDecision(
        allowed=allowed,
        period=period,
        limit_microusd=bp.hard_limit_microusd,
        spent_microusd=bp.spent_microusd,
        reserved_microusd=bp.reserved_microusd,
        warning=warning,
    )


def _release_expr(est_microusd: int):
    return case(
        (
            BudgetPeriod.reserved_microusd >= est_microusd,
            BudgetPeriod.reserved_microusd - est_microusd,
        ),
        else_=0,
    )


def settle(
    db: Session, tenant_id: str, period: str, est_microusd: int, actual_microusd: int
) -> None:
    db.execute(
        update(BudgetPeriod)
        .where(BudgetPeriod.tenant_id == tenant_id, BudgetPeriod.period == period)
        .values(
            spent_microusd=BudgetPeriod.spent_microusd + actual_microusd,
            reserved_microusd=_release_expr(est_microusd),
            updated_at=utcnow(),
        )
    )
    db.commit()


def release(db: Session, tenant_id: str, period: str, est_microusd: int) -> None:
    """Undo a reservation when the request fails before/at the LLM call."""
    db.execute(
        update(BudgetPeriod)
        .where(BudgetPeriod.tenant_id == tenant_id, BudgetPeriod.period == period)
        .values(reserved_microusd=_release_expr(est_microusd), updated_at=utcnow())
    )
    db.commit()
