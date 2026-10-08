"""Tenant isolation. OWNER: Loukik — usage, feedback, idempotency and admin routes."""

from tests.conftest import ADMIN, make_tenant, summarize


def test_usage_is_per_tenant(client):
    a = make_tenant(client, name="a")
    b = make_tenant(client, name="b")
    assert summarize(client, a["api_key"]).status_code == 200
    ua = client.get("/v1/usage", headers={"X-API-Key": a["api_key"]}).json()
    ub = client.get("/v1/usage", headers={"X-API-Key": b["api_key"]}).json()
    assert ua["tenant_id"] == a["tenant"]["id"]
    assert ua["requests"] == 1
    assert ub["requests"] == 0


def test_feedback_cannot_target_other_tenants_request(client):
    a = make_tenant(client, name="a")
    b = make_tenant(client, name="b")
    rid = summarize(client, a["api_key"]).json()["request_id"]
    r = client.post(
        "/v1/feedback",
        json={"request_id": rid, "rating": "up"},
        headers={"X-API-Key": b["api_key"]},
    )
    assert r.status_code == 404, r.text


def test_idempotency_keys_do_not_collide_across_tenants(client):
    from tests.conftest import SAMPLE_TEXT

    a = make_tenant(client, name="a")
    b = make_tenant(client, name="b")
    headers_a = {"X-API-Key": a["api_key"], "Idempotency-Key": "shared-key"}
    headers_b = {"X-API-Key": b["api_key"], "Idempotency-Key": "shared-key"}
    body = {"text": SAMPLE_TEXT, "style": "bullets", "max_words": 100}
    ra = client.post("/v1/summarize", json=body, headers=headers_a)
    rb = client.post("/v1/summarize", json=body, headers=headers_b)
    assert ra.status_code == 200, ra.text
    assert rb.status_code == 200, rb.text
    # different tenants ran independently — no replay header, distinct ids
    assert "Idempotent-Replayed" not in ra.headers
    assert "Idempotent-Replayed" not in rb.headers
    assert ra.json()["request_id"] != rb.json()["request_id"]


def test_revoked_key_cannot_replay_idempotent_request(client):
    from tests.conftest import SAMPLE_TEXT

    t = make_tenant(client)
    headers = {"X-API-Key": t["api_key"], "Idempotency-Key": "once-only"}
    body = {"text": SAMPLE_TEXT, "style": "bullets", "max_words": 100}
    assert client.post("/v1/summarize", json=body, headers=headers).status_code == 200
    client.delete(f"/admin/keys/{t['key']['id']}", headers=ADMIN)
    retry = client.post("/v1/summarize", json=body, headers=headers)
    assert retry.status_code == 401, retry.text


def test_admin_routes_need_token(client):
    t = make_tenant(client)
    tid = t["tenant"]["id"]
    assert client.get("/admin/tenants").status_code == 401
    assert client.get(f"/admin/tenants/{tid}/keys").status_code == 401
    assert client.get("/admin/audit").status_code == 401
    assert client.delete(f"/admin/keys/{t['key']['id']}").status_code == 401
