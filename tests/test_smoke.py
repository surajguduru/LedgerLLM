"""End-to-end smoke tests for the spine. These must stay green on every PR."""

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditEvent, BudgetPeriod, RequestLog, UsageLedger
from tests.conftest import ADMIN, SAMPLE_TEXT, make_tenant, summarize


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_missing_key_is_401_with_error_envelope(client):
    r = client.post("/v1/summarize", json={"text": "hello"})
    assert r.status_code == 401
    body = r.json()["error"]
    assert body["code"] == "missing_api_key"
    assert body["request_id"]
    assert r.headers["X-Request-ID"] == body["request_id"]


def test_invalid_key_is_401(client):
    r = client.post("/v1/summarize", json={"text": "hello"}, headers={"X-API-Key": "llk_nope_nope"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_admin_requires_token(client):
    assert client.post("/admin/tenants", json={"name": "x"}).status_code == 401


def test_validation_error_uses_envelope(client, api_key):
    r = client.post(
        "/v1/summarize",
        json={"text": "a", "url": "https://example.com"},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_summarize_text_happy_path_books_cost(client, api_key):
    r = summarize(client, api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"].startswith("- ")
    assert body["usage"]["model"] == "gemini-3.8-flash"
    assert body["usage"]["prompt_version"].startswith("summarize_v1@")
    assert body["usage"]["input_tokens"] > 0 and body["usage"]["output_tokens"] > 0
    assert body["usage"]["cost_usd"] > 0
    assert body["budget"]["spent_usd"] == body["usage"]["cost_usd"]
    assert body["guardrails"]["input"]["blocked"] is False
    assert r.headers["X-RateLimit-Limit"] == "60"  # pro plan

    with SessionLocal() as db:
        rows = db.scalars(select(UsageLedger)).all()
        assert len(rows) == 1
        assert rows[0].purpose == "completion"
        assert rows[0].cost_microusd == round(body["usage"]["cost_usd"] * 1_000_000)
        bp = db.scalars(select(BudgetPeriod)).one()
        assert bp.spent_microusd == rows[0].cost_microusd
        assert bp.reserved_microusd == 0  # reservation was released on settle
        log = db.scalars(select(RequestLog)).one()
        assert log.status_code == 200 and log.request_id == body["request_id"]


def test_model_not_allowed_on_plan(client):
    key = make_tenant(client, plan="free")["api_key"]
    r = summarize(client, key, model="claude-opus-5-5")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "model_not_allowed"


def test_upstream_failure_releases_reservation_and_audits(client, api_key):
    r = summarize(client, api_key, text=SAMPLE_TEXT + " [[MOCK_FAIL]]")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"
    with SessionLocal() as db:
        bp = db.scalars(select(BudgetPeriod)).one()
        assert bp.reserved_microusd == 0 and bp.spent_microusd == 0
        assert db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "request.upstream_error")
        ).one()


def test_usage_endpoint(client, api_key):
    summarize(client, api_key)
    r = client.get("/v1/usage", headers={"X-API-Key": api_key})
    assert r.status_code == 200
    u = r.json()
    assert u["requests"] == 1 and u["spent_usd"] > 0 and u["plan"] == "pro"
    assert u["by_model"][0]["model"] == "gemini-3.8-flash"


def test_feedback_roundtrip(client, api_key):
    rid = summarize(client, api_key).json()["request_id"]
    r = client.post(
        "/v1/feedback", json={"request_id": rid, "rating": "up"}, headers={"X-API-Key": api_key}
    )
    assert r.status_code == 201
    r = client.post(
        "/v1/feedback", json={"request_id": "nope", "rating": "up"}, headers={"X-API-Key": api_key}
    )
    assert r.status_code == 404


def test_metrics_exposed(client, api_key):
    summarize(client, api_key)
    text = client.get("/metrics").text
    assert "ledgerllm_cost_microusd_total" in text
    assert "ledgerllm_tokens_total" in text


def test_admin_list_tenants(client, api_key):
    r = client.get("/admin/tenants", headers=ADMIN)
    assert r.status_code == 200 and len(r.json()) == 1
