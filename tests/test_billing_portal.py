"""Portal billing: plan catalogue, mock checkout, upgrades and downgrades, payment history (D27)."""

from sqlalchemy import select

from app.billing import payments
from app.db import SessionLocal
from app.models import AuditEvent, BudgetPeriod, Payment, Tenant
from tests.conftest import ADMIN, summarize
from tests.test_portal import code, post, signup

CARD = {
    "name": "Ada Lovelace",
    "number": "4242 4242 4242 4242",
    "exp_month": 12,
    "exp_year": 99,
    "cvc": "123",
}


def upgrade(client, plan="pro", card=CARD):
    return post(client, "/billing/plan", {"plan": plan, "card": card})


def test_billing_lists_plans_cheapest_first(client):
    signup(client)
    b = client.get("/app/api/billing").json()
    assert b["plan"] == "free" and b["payments"] == [] and b["budget_override_usd"] is None
    assert [p["id"] for p in b["plans"]] == ["free", "pro", "enterprise"]
    by_id = {p["id"]: p for p in b["plans"]}
    assert by_id["free"]["price_usd_month"] == 0 and by_id["pro"]["price_usd_month"] == 19
    assert by_id["free"]["map_reduce_max_chars"] == 0 and by_id["pro"]["map_reduce_max_chars"] > 0


def test_upgrade_charges_card_and_applies_the_plan(client):
    j = signup(client)
    summarize(client, j["api_key"])  # creates this month's budget row at the free limit
    pro_model = summarize(client, j["api_key"], model="qwen/qwen3.8-27b")
    assert code(pro_model) == "model_not_allowed"
    r = upgrade(client)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["plan"] == "pro" and out["direction"] == "upgrade"
    assert out["payment"]["amount_usd"] == 19 and out["payment"]["card"] == "Visa •••• 4242"

    # the overview shows the new limit at once, before any further request
    assert client.get("/app/api/usage").json()["summary"]["limit_usd"] == 10
    assert client.get("/app/api/me").json()["tenant"]["plan"] == "pro"
    assert summarize(client, j["api_key"], model="qwen/qwen3.8-27b").status_code == 200

    with SessionLocal() as db:
        pay = db.scalars(select(Payment)).one()
        assert (pay.status, pay.provider, pay.card_last4) == ("succeeded", "mock", "4242")
        assert db.scalars(select(BudgetPeriod)).one().hard_limit_microusd == 10_000_000
        events = {e.event_type: e.details for e in db.scalars(select(AuditEvent))}
        assert events["tenant.plan_changed"]["from"] == "free"
        assert events["payment.succeeded"]["payment_id"] == pay.id
        assert "4242424242424242" not in str(events)

    hist = client.get("/app/api/billing").json()["payments"]
    assert len(hist) == 1 and hist[0]["plan"] == "pro" and hist[0]["previous_plan"] == "free"


def test_downgrade_to_free_needs_no_card_and_charges_nothing(client):
    signup(client)
    upgrade(client, "enterprise")
    r = post(client, "/billing/plan", {"plan": "free"})
    assert r.status_code == 200 and r.json()["direction"] == "downgrade"
    assert r.json()["payment"] is None
    with SessionLocal() as db:
        assert db.scalars(select(Tenant)).one().plan == "free"
        assert len(db.scalars(select(Payment)).all()) == 1  # only the enterprise charge


def test_switching_between_paid_plans_charges_the_new_price(client):
    signup(client)
    upgrade(client, "enterprise")
    r = upgrade(client, "pro")
    assert r.json()["direction"] == "downgrade" and r.json()["payment"]["amount_usd"] == 19


def test_plan_change_refusals(client):
    signup(client)
    assert code(post(client, "/billing/plan", {"plan": "pro"})) == "card_required"
    assert code(post(client, "/billing/plan", {"plan": "platinum"})) == "validation_error"
    assert code(post(client, "/billing/plan", {"plan": "free"})) == "already_on_plan"
    bad = {**CARD, "number": "4242 4242 4242 4241"}
    r = upgrade(client, card=bad)
    assert r.status_code == 422 and code(r) == "card_invalid"
    assert "4241" not in r.text  # never echo the card back
    assert code(upgrade(client, card={**CARD, "exp_year": 20})) == "card_invalid"
    assert code(upgrade(client, card={**CARD, "cvc": "12"})) == "card_invalid"
    with SessionLocal() as db:
        assert db.scalars(select(Payment)).all() == []
        assert db.scalars(select(Tenant)).one().plan == "free"


def test_suspended_tenant_cannot_change_plan(client):
    j = signup(client)
    client.patch(f"/admin/tenants/{j['tenant']['id']}", json={"status": "suspended"}, headers=ADMIN)
    assert code(upgrade(client)) == "tenant_suspended"


def test_billing_needs_session_and_same_origin(client):
    assert code(client.get("/app/api/billing")) == "not_signed_in"
    signup(client)
    r = client.post("/app/api/billing/plan", json={"plan": "pro", "card": CARD})
    assert r.status_code == 403 and code(r) == "cross_origin"


def test_override_is_kept_across_plan_changes(client):
    j = signup(client)
    client.patch(
        f"/admin/tenants/{j['tenant']['id']}", json={"budget_override_usd": 3}, headers=ADMIN
    )
    upgrade(client)
    b = client.get("/app/api/billing").json()
    assert b["plan"] == "pro" and b["budget_override_usd"] == 3


def test_mock_processor_validation():
    card = payments.Card(
        name="A", number="5555 5555 5555 4444", exp_month=1, exp_year=2099, cvc="999"
    )
    c = payments.charge(card, 19_000_000)
    assert (c.status, c.brand, c.last4) == ("succeeded", "mastercard", "4444")
    assert c.ref.startswith("ch_mock_")
    assert payments.brand_of("378282246310005") == "amex"
