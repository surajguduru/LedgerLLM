"""Exact-match response cache (app/traffic/cache.py, docs/DESIGN.md D16)."""

import dataclasses
from datetime import timedelta

import pytest

from app.api import summarize as pipeline
from app.config import get_settings
from app.db import SessionLocal
from app.guardrails.types import GuardrailVerdict
from app.models import ResponseCache, UsageLedger, utcnow
from app.traffic import cache
from tests.conftest import make_tenant, summarize


@pytest.fixture(autouse=True)
def cache_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "response_cache_enabled", True)


def _usage(client, key):
    return client.get("/v1/usage", headers={"X-API-Key": key}).json()


def _ledger_cost():
    with SessionLocal() as db:
        return sum(r.cost_microusd for r in db.query(UsageLedger).all())


def test_identical_request_is_a_free_hit(client, api_key):
    r1 = summarize(client, api_key)
    cost_after_first = _ledger_cost()
    r2 = summarize(client, api_key)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["usage"]["cached"] is False
    hit = r2.json()
    assert hit["usage"]["cached"] is True
    assert hit["usage"]["cost_usd"] == 0
    assert hit["summary"] == r1.json()["summary"]
    assert hit["request_id"] != r1.json()["request_id"]
    assert _ledger_cost() == cost_after_first  # budget untouched
    assert hit["budget"]["spent_usd"] == r1.json()["budget"]["spent_usd"]
    assert hit["budget"]["remaining_usd"] > 0


def test_hit_is_counted_as_a_request_with_zero_tokens(client, api_key):
    summarize(client, api_key)
    tokens_before = _usage(client, api_key)
    summarize(client, api_key)
    after = _usage(client, api_key)
    assert after["requests"] == 2
    assert after["input_tokens"] == tokens_before["input_tokens"]
    with SessionLocal() as db:
        statuses = sorted(r.status for r in db.query(UsageLedger).all())
    assert statuses == ["cached", "ok"]


def test_different_options_miss(client, api_key):
    summarize(client, api_key)
    assert summarize(client, api_key, max_words=120).json()["usage"]["cached"] is False
    assert summarize(client, api_key, style="tldr").json()["usage"]["cached"] is False


def test_prompt_change_invalidates(client, api_key, monkeypatch):
    summarize(client, api_key)
    real = pipeline.load_prompt
    monkeypatch.setattr(
        pipeline,
        "load_prompt",
        lambda version: dataclasses.replace(real(version), content_hash="000000000000"),
    )
    assert summarize(client, api_key).json()["usage"]["cached"] is False


def test_entries_expire_after_plan_ttl(client, api_key, monkeypatch):
    summarize(client, api_key)
    later = utcnow() + timedelta(seconds=86_400 + 1)  # pro plan cache_ttl_s
    monkeypatch.setattr(cache, "utcnow", lambda: later)
    assert summarize(client, api_key).json()["usage"]["cached"] is False


def test_never_shared_across_tenants(client):
    k1 = make_tenant(client, name="a")["api_key"]
    k2 = make_tenant(client, name="b")["api_key"]
    summarize(client, k1)
    assert summarize(client, k2).json()["usage"]["cached"] is False


def test_withheld_summary_is_never_cached(client, api_key, monkeypatch):
    flagged = GuardrailVerdict(blocked=True, category="pii_leak", score=1.0, method="test")
    monkeypatch.setattr(pipeline, "moderate_output", lambda text, **kw: flagged)
    r = summarize(client, api_key)
    assert r.json()["summary"] == pipeline.WITHHELD
    with SessionLocal() as db:
        assert db.query(ResponseCache).count() == 0


def test_shadow_flagged_output_is_not_cached(client, api_key, monkeypatch):
    monkeypatch.setattr(get_settings(), "guardrails_mode", "shadow")
    flagged = GuardrailVerdict(blocked=True, category="pii_leak", score=1.0, method="test")
    monkeypatch.setattr(pipeline, "moderate_output", lambda text, **kw: flagged)
    r = summarize(client, api_key)
    assert r.json()["summary"] != pipeline.WITHHELD  # shadow serves it once...
    with SessionLocal() as db:
        assert db.query(ResponseCache).count() == 0  # ...but never again from cache


def test_no_cache_header_bypasses_lookup_and_refreshes(client, api_key):
    summarize(client, api_key)
    r = summarize(client, api_key, _headers={"Cache-Control": "no-cache"})
    assert r.json()["usage"]["cached"] is False
    assert summarize(client, api_key).json()["usage"]["cached"] is True


def test_kill_switch_disables_cache(client, api_key, monkeypatch):
    monkeypatch.setattr(get_settings(), "response_cache_enabled", False)
    summarize(client, api_key)
    assert summarize(client, api_key).json()["usage"]["cached"] is False


def test_hit_with_idempotency_key_is_replayable(client, api_key):
    summarize(client, api_key)
    h = {"Idempotency-Key": "hit-1"}
    r1 = summarize(client, api_key, _headers=h)
    r2 = summarize(client, api_key, _headers=h)
    assert r1.json()["usage"]["cached"] is True
    assert r2.status_code == 200
    assert r2.headers.get("Idempotent-Replayed") == "true"


def test_hits_counter_and_metric(client, api_key):
    summarize(client, api_key)
    summarize(client, api_key)
    summarize(client, api_key)
    with SessionLocal() as db:
        assert db.query(ResponseCache).one().hits == 2
    text = client.get("/metrics").text
    assert 'ledgerllm_cache_total{result="hit"}' in text
    assert 'ledgerllm_cache_total{result="miss"}' in text


def test_purge_removes_expired_rows(client, api_key, monkeypatch):
    summarize(client, api_key)
    with SessionLocal() as db:
        assert cache.purge_expired(db) == 0
        monkeypatch.setattr(cache, "utcnow", lambda: utcnow() + timedelta(days=2))
        assert cache.purge_expired(db) == 1
        db.commit()
