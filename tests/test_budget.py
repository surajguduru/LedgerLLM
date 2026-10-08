"""Budget enforcement: hard cutoff, soft warning once per period, rollover, limit sync, and a real
concurrent burst against Postgres (SQLite serialises writers, so only Postgres can prove it)."""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select

from app.billing import budget
from app.config import get_settings
from app.db import SessionLocal
from app.llm import get_provider
from app.main import create_app
from app.models import AuditEvent, BudgetPeriod, Tenant, UsageLedger
from app.plans import usd_to_microusd
from tests.conftest import ADMIN, SAMPLE_TEXT, make_tenant, summarize

ON_POSTGRES = get_settings().database_url.startswith("postgresql")


def _soft_warning_events(db):
    return db.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "budget.soft_warning")
    ).all()


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


def test_soft_warning_audit_event_emitted_once(client):
    # Big enough that several admitted requests land between 80 % and 100 % of the limit.
    key = make_tenant(client, plan="pro", budget_override_usd=0.02)["api_key"]
    warned_responses = 0
    for _ in range(40):
        r = summarize(client, key)
        if r.status_code != 200:
            break
        warned_responses += "X-Budget-Warning" in r.headers
    assert r.status_code == 402
    assert warned_responses >= 2  # the header repeats; the audit event must not

    with SessionLocal() as db:
        events = _soft_warning_events(db)
        assert len(events) == 1
        bp = db.scalars(select(BudgetPeriod)).one()
        assert bp.soft_warned_at is not None
        assert events[0].request_id and events[0].details["period"] == bp.period
        assert events[0].details["committed_microusd"] >= 0.8 * bp.hard_limit_microusd


def test_no_soft_warning_below_threshold(client, api_key):
    for _ in range(3):
        r = summarize(client, api_key)
        assert r.status_code == 200 and "X-Budget-Warning" not in r.headers
    with SessionLocal() as db:
        assert _soft_warning_events(db) == []


def test_soft_warning_claim_is_won_exactly_once(client, api_key):
    summarize(client, api_key)
    with SessionLocal() as db:
        tenant_id = db.scalars(select(Tenant.id)).one()
        period = budget.current_period()
        claims = [budget._claim_soft_warning(db, tenant_id, period) for _ in range(5)]
    assert claims == [True, False, False, False, False]


def test_new_month_starts_a_fresh_period(client, api_key, monkeypatch):
    assert summarize(client, api_key).status_code == 200
    october = budget.current_period()
    monkeypatch.setattr(budget, "current_period", lambda now=None: "2099-01")
    r = summarize(client, api_key)
    assert r.status_code == 200 and r.json()["budget"]["period"] == "2099-01"

    with SessionLocal() as db:
        rows = {bp.period: bp for bp in db.scalars(select(BudgetPeriod))}
        assert set(rows) == {october, "2099-01"}
        assert rows["2099-01"].spent_microusd == usd_to_microusd(r.json()["usage"]["cost_usd"])
        assert rows[october].spent_microusd > 0  # last month's bill is untouched
        assert all(bp.reserved_microusd == 0 for bp in rows.values())


def test_soft_warning_resets_in_a_new_period(client, monkeypatch):
    key = make_tenant(client, plan="pro", budget_override_usd=0.004)["api_key"]
    while summarize(client, key).status_code == 200:
        pass
    monkeypatch.setattr(budget, "current_period", lambda now=None: "2099-01")
    assert summarize(client, key).status_code == 200  # fresh budget
    while summarize(client, key).status_code == 200:
        pass
    with SessionLocal() as db:
        assert len(_soft_warning_events(db)) == 2


def test_limit_change_mid_month_applies_on_next_request(client):
    key = make_tenant(client, plan="pro", budget_override_usd=0.004)["api_key"]
    while summarize(client, key).status_code == 200:
        pass
    with SessionLocal() as db:
        tenant = db.scalars(select(Tenant)).one()
        tenant.budget_override_microusd = None  # back to the pro plan's $10
        db.commit()

    r = summarize(client, key)
    assert r.status_code == 200
    assert r.json()["budget"]["limit_usd"] == 10.0
    with SessionLocal() as db:
        bp = db.scalars(select(BudgetPeriod)).one()
        assert bp.hard_limit_microusd == usd_to_microusd(10.0)


def test_estimate_is_recorded_and_bounds_actual_cost(client, api_key):
    assert summarize(client, api_key).status_code == 200
    with SessionLocal() as db:
        row = db.scalars(select(UsageLedger).where(UsageLedger.purpose == "completion")).one()
        assert row.estimate_microusd is not None
        assert row.estimate_microusd >= row.cost_microusd > 0


# --- concurrent burst on Postgres -------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(monkeypatch):
    """The real app under uvicorn in a background thread, so requests truly run concurrently."""
    # Widen the window between reserve and settle, where a stale-counter check would overspend.
    monkeypatch.setattr(get_provider(), "latency_ms", 50)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.skipif(not ON_POSTGRES, reason="needs DATABASE_URL=postgresql+psycopg://…")
def test_burst_of_concurrent_requests_cannot_overspend(live_server):
    with httpx.Client(base_url=live_server, timeout=60) as http:
        r = http.post(
            "/admin/tenants",
            json={"name": "burst", "plan": "pro", "budget_override_usd": 0.004},
            headers=ADMIN,
        )
        assert r.status_code == 201, r.text
        key = r.json()["api_key"]
        body = {"text": SAMPLE_TEXT, "style": "bullets", "max_words": 100}

        def fire(_):
            return http.post("/v1/summarize", json=body, headers={"X-API-Key": key})

        with ThreadPoolExecutor(max_workers=50) as pool:
            responses = list(pool.map(fire, range(50)))

    statuses = [r.status_code for r in responses]
    ok = statuses.count(200)
    assert ok + statuses.count(402) == 50, statuses
    assert ok >= 1 and statuses.count(402) >= 1, statuses

    with SessionLocal() as db:
        bp = db.scalars(select(BudgetPeriod)).one()
        ledger_total = db.scalar(select(func.coalesce(func.sum(UsageLedger.cost_microusd), 0)))
        completions = db.scalar(
            select(func.count(func.distinct(UsageLedger.request_id))).where(
                UsageLedger.purpose == "completion", UsageLedger.status == "ok"
            )
        )
        estimate = db.scalar(select(func.max(UsageLedger.estimate_microusd)))
    assert ledger_total == bp.spent_microusd
    assert bp.spent_microusd <= bp.hard_limit_microusd
    assert bp.reserved_microusd == 0
    assert completions == ok
    # The burst asked for far more than the budget: a check against a stale `spent` would overspend.
    assert 50 * estimate > bp.hard_limit_microusd
