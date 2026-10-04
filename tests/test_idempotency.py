from tests.conftest import summarize


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
