"""Admin API. OWNER: Loukik."""

from tests.conftest import ADMIN, make_tenant, summarize


def test_list_keys(client):
    t = make_tenant(client)
    r = client.get(f"/admin/tenants/{t['tenant']['id']}/keys", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1
    r2 = client.post(
        f"/admin/tenants/{t['tenant']['id']}/keys", json={"name": "second"}, headers=ADMIN
    )
    assert r2.status_code == 201, r2.text
    r3 = client.get(f"/admin/tenants/{t['tenant']['id']}/keys", headers=ADMIN)
    assert len(r3.json()) == 2


def test_revoke_key_then_401(client):
    t = make_tenant(client)
    key_id = t["key"]["id"]
    r = client.delete(f"/admin/keys/{key_id}", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["revoked_at"] is not None
    bad = summarize(client, t["api_key"])
    assert bad.status_code == 401, bad.text


def test_rotate_key(client):
    t = make_tenant(client)
    r = client.post(f"/admin/keys/{t['key']['id']}/rotate", headers=ADMIN)
    assert r.status_code == 201, r.text
    new_raw = r.json()["api_key"]
    assert new_raw != t["api_key"]
    # old key revoked
    assert summarize(client, t["api_key"]).status_code == 401
    # new key works
    assert summarize(client, new_raw).status_code == 200


def test_patch_tenant_suspend_then_403(client):
    t = make_tenant(client)
    tid = t["tenant"]["id"]
    r = client.patch(f"/admin/tenants/{tid}", json={"status": "suspended"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "suspended"
    denied = summarize(client, t["api_key"])
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "tenant_suspended"
    # plan + budget override
    r2 = client.patch(
        f"/admin/tenants/{tid}",
        json={"plan": "free", "status": "active", "budget_override_usd": 5.0},
        headers=ADMIN,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["plan"] == "free"
    assert summarize(client, t["api_key"]).status_code == 200


def test_patch_tenant_validation(client):
    t = make_tenant(client)
    tid = t["tenant"]["id"]
    assert (
        client.patch(f"/admin/tenants/{tid}", json={"plan": "nope"}, headers=ADMIN).status_code
        == 422
    )
    assert (
        client.patch(f"/admin/tenants/{tid}", json={"status": "gone"}, headers=ADMIN).status_code
        == 422
    )


def test_audit_pagination_and_filters(client):
    t = make_tenant(client)
    # generate a refusal + admin actions
    summarize(client, t["api_key"], model="does-not-exist")
    r = client.get("/admin/audit", headers=ADMIN)
    assert r.status_code == 200, r.text
    events = r.json()
    assert len(events) >= 3
    # newest first
    assert events[0]["created_at"] >= events[-1]["created_at"]
    by_type = client.get("/admin/audit?event_type=request.model_not_allowed", headers=ADMIN)
    assert by_type.status_code == 200
    assert all(e["event_type"] == "request.model_not_allowed" for e in by_type.json())
    assert len(by_type.json()) >= 1
    by_tenant = client.get(f"/admin/audit?tenant_id={t['tenant']['id']}&limit=1", headers=ADMIN)
    assert len(by_tenant.json()) == 1
    last_id = by_tenant.json()[0]["id"]
    nxt = client.get(f"/admin/audit?tenant_id={t['tenant']['id']}&before={last_id}", headers=ADMIN)
    assert nxt.status_code == 200
    assert all(e["id"] != last_id for e in nxt.json())


def test_admin_requires_token(client):
    assert client.get("/admin/tenants").status_code == 401
    assert client.get("/admin/audit").status_code == 401
