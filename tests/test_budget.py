"""Budget enforcement. OWNER: Naresh — make the xfail tests pass and add a true concurrency test."""

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditEvent, BudgetPeriod, UsageLedger
from tests.conftest import make_tenant, summarize


def test_hard_cutoff_returns_402_and_never_overspends(client):
    key = make_tenant(client, plan="pro", budget_override_usd=0.004)["api_key"]
    statuses = [summarize(client, key).status_code for _ in range(10)]
    assert 200 in statuses and 402 in statuses, statuses
    # once over budget, it stays over budget
    first_402 = statuses.index(402)
    assert all(s == 402 for s in statuses[first_402:])

    with SessionLocal() as db:
        total = sum(r.cost_microusd for r in db.scalars(select(UsageLedger)).all())
        bp = db.scalars(select(BudgetPeriod)).one()
        assert total == bp.spent_microusd
        assert bp.spent_microusd <= bp.hard_limit_microusd
        assert bp.reserved_microusd == 0
        assert db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "request.budget_exceeded")
        ).first()


def test_soft_warning_header_past_80_percent(client):
    key = make_tenant(client, plan="pro", budget_override_usd=0.004)["api_key"]
    warned = False
    for _ in range(10):
        r = summarize(client, key)
        if r.status_code != 200:
            break
        warned = warned or "X-Budget-Warning" in r.headers
    assert warned


@pytest.mark.xfail(
    reason="TODO(Naresh): emit budget.soft_warning audit event exactly once per period",
    strict=False,
)
def test_soft_warning_audit_event_emitted_once(client):
    key = make_tenant(client, plan="pro", budget_override_usd=0.004)["api_key"]
    for _ in range(10):
        if summarize(client, key).status_code != 200:
            break
    with SessionLocal() as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "budget.soft_warning")
        ).all()
        assert len(events) == 1


@pytest.mark.xfail(
    reason="TODO(Naresh): concurrency test with threads against Postgres (sqlite serialises)",
    strict=False,
)
def test_burst_of_concurrent_requests_cannot_overspend(client):
    raise NotImplementedError
