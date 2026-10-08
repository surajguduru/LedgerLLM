"""Billing review items: no double bill on an idempotent retry, ledger rows in the spend's month,
remaining budget net of other requests' reservations."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update

import app.api.summarize as pipeline
from app.billing import budget
from app.db import SessionLocal
from app.models import BudgetPeriod, UsageLedger
from tests.conftest import make_tenant, summarize


def _spent_and_rows(tenant_id: str) -> tuple[int, int]:
    with SessionLocal() as db:
        spent = db.scalar(
            select(func.coalesce(func.sum(BudgetPeriod.spent_microusd), 0)).where(
                BudgetPeriod.tenant_id == tenant_id
            )
        )
        rows = db.scalar(
            select(func.count())
            .select_from(UsageLedger)
            .where(UsageLedger.tenant_id == tenant_id, UsageLedger.purpose == "completion")
        )
    return int(spent), int(rows)


def test_failure_after_settle_does_not_bill_the_retry_twice(client, monkeypatch):
    t = make_tenant(client)
    tid, key = t["tenant"]["id"], t["api_key"]
    headers = {"Idempotency-Key": "pay-once"}

    real_store = pipeline.idempotency.store
    calls = {"n": 0}

    def flaky_store(*a, **kw):  # the write after settle fails the first time only
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup")
        return real_store(*a, **kw)

    monkeypatch.setattr(pipeline.idempotency, "store", flaky_store)
    with pytest.raises(RuntimeError):  # TestClient re-raises what would be a 500
        summarize(client, key, _headers=headers)
    assert _spent_and_rows(tid) == (0, 0), "the failed attempt must not be billed"

    r = summarize(client, key, _headers=headers)
    assert r.status_code == 200, r.text
    spent, rows = _spent_and_rows(tid)
    assert rows == 1
    assert spent == round(r.json()["usage"]["cost_usd"] * 1_000_000)
    with SessionLocal() as db:
        assert db.scalar(select(BudgetPeriod.reserved_microusd)) == 0


def test_ledger_rows_land_in_the_month_that_was_charged(client, api_key, monkeypatch):
    month_end = datetime(2026, 9, 30, 23, 59, 59, 900_000, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return month_end

    monkeypatch.setattr(pipeline, "datetime", FrozenDatetime)
    assert summarize(client, api_key).status_code == 200
    with SessionLocal() as db:
        periods = {bp.period: bp.spent_microusd for bp in db.scalars(select(BudgetPeriod))}
        created = [r.created_at for r in db.scalars(select(UsageLedger))]
    assert periods.get("2026-09", 0) > 0
    assert created and all(budget.current_period(c) == "2026-09" for c in created)


def test_remaining_budget_excludes_other_in_flight_reservations(client):
    t = make_tenant(client, budget_override_usd=1.0)
    tid, key = t["tenant"]["id"], t["api_key"]
    assert summarize(client, key, max_words=60).status_code == 200  # creates the period row
    held = 200_000  # another request holding $0.20
    with SessionLocal() as db:
        db.execute(
            update(BudgetPeriod).where(BudgetPeriod.tenant_id == tid).values(reserved_microusd=held)
        )
        db.commit()
    r = summarize(client, key)
    b = r.json()["budget"]
    assert r.status_code == 200
    assert b["remaining_usd"] == pytest.approx(b["limit_usd"] - b["spent_usd"] - 0.2, abs=1e-6)
    usage = client.get("/v1/usage", headers={"X-API-Key": key}).json()
    assert float(usage["remaining_usd"]) == pytest.approx(b["remaining_usd"], abs=1e-6)


def test_cached_response_remaining_budget_matches_usage(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "response_cache_enabled", True)
    t = make_tenant(client, budget_override_usd=1.0)
    tid, key = t["tenant"]["id"], t["api_key"]
    assert summarize(client, key).status_code == 200
    with SessionLocal() as db:
        db.execute(
            update(BudgetPeriod)
            .where(BudgetPeriod.tenant_id == tid)
            .values(reserved_microusd=300_000)
        )
        db.commit()
    hit = summarize(client, key)
    assert hit.json()["usage"]["cached"] is True
    usage = client.get("/v1/usage", headers={"X-API-Key": key}).json()
    assert hit.json()["budget"]["remaining_usd"] == pytest.approx(
        float(usage["remaining_usd"]), abs=1e-6
    )
