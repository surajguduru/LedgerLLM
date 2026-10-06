"""Rate limiting: fixed 60 s window per API key (docs/DESIGN.md D3)."""

import time

from app.db import SessionLocal
from app.models import RateLimitWindow
from app.traffic import ratelimit
from tests.conftest import ADMIN, make_tenant, summarize


def _freeze(monkeypatch, epoch: int) -> None:
    monkeypatch.setattr(ratelimit.time, "time", lambda: float(epoch))


def test_rate_limit_headers_present(client, api_key):
    r = summarize(client, api_key)
    assert r.headers["X-RateLimit-Limit"] == "60"
    assert "X-RateLimit-Remaining" in r.headers and "X-RateLimit-Reset" in r.headers


def test_free_plan_sixth_request_in_a_minute_is_429(client, monkeypatch):
    _freeze(monkeypatch, 1_800_000_000)  # start of a window, so the test cannot straddle a boundary
    key = make_tenant(client, plan="free")["api_key"]
    statuses = [summarize(client, key).status_code for _ in range(6)]
    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429
    r = summarize(client, key)
    assert r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) >= 1
    assert r.headers["X-RateLimit-Remaining"] == "0"


def test_remaining_decrements(client, monkeypatch):
    _freeze(monkeypatch, 1_800_000_000)
    key = make_tenant(client, plan="free")["api_key"]
    r1 = summarize(client, key)
    r2 = summarize(client, key)
    assert int(r1.headers["X-RateLimit-Remaining"]) == 4
    assert int(r2.headers["X-RateLimit-Remaining"]) == 3


def test_limit_is_per_key(client, monkeypatch):
    _freeze(monkeypatch, 1_800_000_000)
    created = make_tenant(client, plan="free")
    k1 = created["api_key"]
    r = client.post(f"/admin/tenants/{created['tenant']['id']}/keys", json={}, headers=ADMIN)
    assert r.status_code == 201, r.text
    k2 = r.json()["api_key"]
    for _ in range(5):
        assert summarize(client, k1).status_code == 200
    assert summarize(client, k1).status_code == 429
    r2 = summarize(client, k2)
    assert r2.status_code == 200
    assert r2.headers["X-RateLimit-Remaining"] == "4"


def test_window_reset_admits_again(client, monkeypatch):
    start = 1_800_000_000
    _freeze(monkeypatch, start + 59)
    key = make_tenant(client, plan="free")["api_key"]
    for _ in range(5):
        assert summarize(client, key).status_code == 200
    r = summarize(client, key)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "1"
    assert r.headers["X-RateLimit-Reset"] == str(start + 60)
    _freeze(monkeypatch, start + 60)
    r = summarize(client, key)
    assert r.status_code == 200
    assert r.headers["X-RateLimit-Remaining"] == "4"


def test_rejected_request_does_not_bill(client, monkeypatch):
    _freeze(monkeypatch, 1_800_000_000)
    key = make_tenant(client, plan="free")["api_key"]
    for _ in range(7):
        summarize(client, key)
    usage = client.get("/v1/usage", headers={"X-API-Key": key}).json()
    assert usage["requests"] == 5


def test_prune_removes_only_stale_windows():
    now = int(time.time())
    with SessionLocal() as db:
        db.add_all(
            [
                RateLimitWindow(key_id="k", window_start=now - 600, count=3),
                RateLimitWindow(key_id="k", window_start=now - 60, count=2),
            ]
        )
        db.commit()
        assert ratelimit.prune_windows(db, now) == 1
        db.commit()
        assert [w.window_start for w in db.query(RateLimitWindow).all()] == [now - 60]
