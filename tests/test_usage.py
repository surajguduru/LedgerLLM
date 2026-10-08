"""Usage API: summary series, recent calls and the CSV statement. All money must reconcile with the
budget row admission control uses."""

import csv
import io

from sqlalchemy import select

from app.billing import budget, ledger
from app.db import SessionLocal
from app.models import BudgetPeriod, UsageLedger
from app.plans import microusd_to_usd
from tests.conftest import make_tenant, summarize


def _usage(client, key):
    r = client.get("/v1/usage", headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()


def _statement(client, key, **params):
    return client.get("/v1/usage/statement.csv", headers={"X-API-Key": key}, params=params)


def test_by_day_and_last_requests(client, api_key):
    for _ in range(3):
        assert summarize(client, api_key).status_code == 200
    u = _usage(client, api_key)

    assert len(u["by_day"]) == 1
    today = u["by_day"][0]
    assert today["requests"] == 3 == u["requests"]
    assert today["cost_usd"] == u["spent_usd"]
    # Postgres SUM(bigint) is a Decimal; money must still serialise as a JSON number
    assert all(isinstance(m["cost_usd"], float) for m in u["by_model"] + u["by_purpose"])
    assert today["date"].startswith(u["period"])

    assert len(u["last_requests"]) == 3
    stamps = [r["created_at"] for r in u["last_requests"]]
    assert stamps == sorted(stamps, reverse=True)
    assert {r["purpose"] for r in u["last_requests"]} == {"completion"}


def test_last_requests_is_capped_at_ten(client):
    key = make_tenant(client, plan="enterprise")["api_key"]
    for _ in range(12):
        assert summarize(client, key).status_code == 200
    assert len(_usage(client, key)["last_requests"]) == 10


def test_statement_csv_reconciles_with_spent(client, api_key):
    for _ in range(2):
        summarize(client, api_key)
    r = _statement(client, api_key)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]

    rows = list(csv.DictReader(io.StringIO(r.text)))
    items, total = rows[:-1], rows[-1]
    assert len(items) == 2 and total["created_at"] == "TOTAL"
    assert sum(int(i["cost_microusd"]) for i in items) == int(total["cost_microusd"])
    with SessionLocal() as db:
        bp = db.scalars(select(BudgetPeriod)).one()
    assert int(total["cost_microusd"]) == bp.spent_microusd
    assert float(total["cost_usd"]) == microusd_to_usd(bp.spent_microusd)
    assert all(i["price_version"] and i["status"] == "ok" for i in items)


def test_statement_for_another_period_is_empty(client, api_key):
    summarize(client, api_key)
    rows = list(csv.reader(io.StringIO(_statement(client, api_key, period="2001-01").text)))
    assert len(rows) == 2 and rows[1][0] == "TOTAL" and rows[1][7] == "0"


def test_statement_rejects_a_malformed_period(client, api_key):
    r = _statement(client, api_key, period="2026-13")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_statement_is_tenant_scoped(client):
    a = make_tenant(client, name="a")["api_key"]
    b = make_tenant(client, name="b")["api_key"]
    summarize(client, a)
    rows = list(csv.reader(io.StringIO(_statement(client, b).text)))
    assert [r[0] for r in rows[1:]] == ["TOTAL"]
    assert _usage(client, b)["last_requests"] == []


def test_usage_follows_the_current_period(client, api_key, monkeypatch):
    summarize(client, api_key)
    monkeypatch.setattr(budget, "current_period", lambda now=None: "2099-01")
    u = _usage(client, api_key)
    assert u["period"] == "2099-01" and u["spent_usd"] == 0 and u["by_day"] == []


def test_judge_rows_are_listed_but_not_billed(client, api_key):
    """Online-judge calls (D17) are attributed to the tenant but paid by the platform."""
    r = summarize(client, api_key)
    with SessionLocal() as db:
        completion = db.scalars(select(UsageLedger)).one()
        ledger.book(
            db,
            tenant_id=completion.tenant_id,
            key_id=None,
            request_id=r.json()["request_id"],
            purpose="judge",
            model="gemini-3.8-flash",
            input_tokens=3000,
            output_tokens=60,
            cost_microusd=2475,
            price_version=completion.price_version,
            prompt_version="judge",
            latency_ms=900,
        )
        db.commit()
        spent = db.scalars(select(BudgetPeriod)).one().spent_microusd

    u = _usage(client, api_key)
    assert u["spent_usd"] == microusd_to_usd(spent)
    assert u["by_day"][0]["cost_usd"] == u["spent_usd"]
    assert sum(m["cost_usd"] for m in u["by_model"]) == u["spent_usd"]
    assert u["input_tokens"] == completion.input_tokens
    judge = next(p for p in u["by_purpose"] if p["purpose"] == "judge")
    assert judge["billed"] is False and judge["cost_usd"] == 0.002475
    assert {(x["purpose"], x["billed"]) for x in u["last_requests"]} == {
        ("completion", True),
        ("judge", False),
    }

    rows = list(csv.DictReader(io.StringIO(_statement(client, api_key).text)))
    assert [(x["purpose"], x["billed"]) for x in rows[:-1]] == [
        ("completion", "yes"),
        ("judge", "no"),
    ]
    assert int(rows[-1]["cost_microusd"]) == spent
