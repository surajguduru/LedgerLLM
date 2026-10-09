from tests.conftest import make_tenant, summarize


def test_same_key_replays_same_response(client, api_key):
    h = {"Idempotency-Key": "abc-123"}
    r1 = summarize(client, api_key, _headers=h)
    r2 = summarize(client, api_key, _headers=h)
    assert r1.status_code == r2.status_code == 200
    assert r2.headers.get("Idempotent-Replayed") == "true"
    assert r1.json()["request_id"] == r2.json()["request_id"]
    # replay must not bill twice
    usage = client.get("/v1/usage", headers={"X-API-Key": api_key}).json()
    assert usage["requests"] == 1


def test_same_key_different_body_is_409(client, api_key):
    h = {"Idempotency-Key": "abc-456"}
    assert summarize(client, api_key, _headers=h).status_code == 200
    r = summarize(client, api_key, max_words=120, _headers=h)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"


def test_idempotency_is_scoped_per_tenant(client):
    from tests.conftest import make_tenant

    k1 = make_tenant(client, name="a")["api_key"]
    k2 = make_tenant(client, name="b")["api_key"]
    h = {"Idempotency-Key": "shared"}
    r1 = summarize(client, k1, _headers=h)
    r2 = summarize(client, k2, _headers=h)
    assert r1.json()["request_id"] != r2.json()["request_id"]
    assert "Idempotent-Replayed" not in r2.headers


def _records():
    from app.db import SessionLocal
    from app.models import IdempotencyRecord

    with SessionLocal() as db:
        return db.query(IdempotencyRecord).all()


def _tenant_id(client, key):
    from tests.conftest import ADMIN

    tenants = client.get("/admin/tenants", headers=ADMIN).json()
    assert len(tenants) == 1
    return tenants[0]["id"]


def test_duplicate_in_flight_gets_409_in_progress(client, api_key):
    from app.db import SessionLocal
    from app.schemas import SummarizeRequest
    from app.traffic import idempotency
    from tests.conftest import SAMPLE_TEXT

    body = SummarizeRequest(text=SAMPLE_TEXT, style="bullets", max_words=100)
    with SessionLocal() as db:
        claimed = idempotency.claim(
            db,
            tenant_id=_tenant_id(client, api_key),
            key="busy",
            request_hash=idempotency.hash_request(body),
        )
        assert claimed
    r = summarize(client, api_key, _headers={"Idempotency-Key": "busy"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_in_progress"
    assert r.headers["Retry-After"] == "1"


def test_second_claim_on_same_key_loses():
    from app.db import SessionLocal
    from app.traffic import idempotency

    with SessionLocal() as db:
        assert idempotency.claim(db, tenant_id="t", key="k", request_hash="h")
    with SessionLocal() as db:
        assert not idempotency.claim(db, tenant_id="t", key="k", request_hash="h")


def test_failure_clears_pending_so_retry_runs(client, api_key):
    h = {"Idempotency-Key": "flaky"}
    r1 = summarize(client, api_key, text="[[MOCK_FAIL]] " + "x " * 50, _headers=h)
    assert r1.status_code == 502
    assert _records() == []
    r2 = summarize(client, api_key, text="[[MOCK_FAIL]] " + "x " * 50, _headers=h)
    assert r2.status_code == 502  # ran again instead of 409 in progress


def test_refused_request_clears_pending(client):
    from tests.conftest import make_tenant

    key = make_tenant(client, budget_override_usd=0.0)["api_key"]
    r = summarize(client, key, _headers={"Idempotency-Key": "broke"})
    assert r.status_code == 402
    assert _records() == []


def test_record_expires_after_ttl(client, api_key, monkeypatch):
    from datetime import timedelta

    from app.models import utcnow
    from app.traffic import idempotency

    h = {"Idempotency-Key": "old"}
    r1 = summarize(client, api_key, _headers=h)
    later = utcnow() + idempotency.TTL + timedelta(minutes=1)
    monkeypatch.setattr(idempotency, "utcnow", lambda: later)
    r2 = summarize(client, api_key, _headers=h)
    assert r2.status_code == 200
    assert "Idempotent-Replayed" not in r2.headers
    assert r2.json()["request_id"] != r1.json()["request_id"]


def test_stale_pending_record_stops_blocking(client, api_key, monkeypatch):
    from datetime import timedelta

    from app.db import SessionLocal
    from app.models import utcnow
    from app.schemas import SummarizeRequest
    from app.traffic import idempotency
    from tests.conftest import SAMPLE_TEXT

    body = SummarizeRequest(text=SAMPLE_TEXT, style="bullets", max_words=100)
    with SessionLocal() as db:
        idempotency.claim(
            db,
            tenant_id=_tenant_id(client, api_key),
            key="crashed",
            request_hash=idempotency.hash_request(body),
        )
    later = utcnow() + idempotency.PENDING_TTL + timedelta(seconds=1)
    monkeypatch.setattr(idempotency, "utcnow", lambda: later)
    r = summarize(client, api_key, _headers={"Idempotency-Key": "crashed"})
    assert r.status_code == 200


def test_cleanup_deletes_only_expired(client, api_key, monkeypatch):
    from datetime import timedelta

    from app.db import SessionLocal
    from app.models import utcnow
    from app.traffic import idempotency

    summarize(client, api_key, _headers={"Idempotency-Key": "a"})
    with SessionLocal() as db:
        assert idempotency.cleanup(db) == 0
    later = utcnow() + idempotency.TTL + timedelta(minutes=1)
    monkeypatch.setattr(idempotency, "utcnow", lambda: later)
    with SessionLocal() as db:
        assert idempotency.cleanup(db) == 1
    assert _records() == []


def test_replays_count_against_the_rate_limit_but_are_free(client):
    key = make_tenant(client, plan="free")["api_key"]  # 5 requests/minute
    h = {"Idempotency-Key": "storm"}
    first = summarize(client, key, _headers=h)
    assert first.status_code == 200
    remaining = int(first.headers["X-RateLimit-Remaining"])
    for _ in range(4):
        r = summarize(client, key, _headers=h)
        assert r.status_code == 200 and r.headers["Idempotent-Replayed"] == "true"
        assert int(r.headers["X-RateLimit-Remaining"]) == remaining - 1
        remaining -= 1
    r = summarize(client, key, _headers=h)
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    usage = client.get("/v1/usage", headers={"X-API-Key": key}).json()
    assert usage["spent_usd"] == first.json()["usage"]["cost_usd"]
