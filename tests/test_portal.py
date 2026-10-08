"""Tenant portal: sign-up, sign-in, sessions, CSRF guard, self-service keys, per-key usage (D24)."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models import AuditEvent, BudgetPeriod, PortalSession, Tenant, User, utcnow
from app.plans import get_plan
from tests.conftest import ADMIN, summarize

ORIGIN = {"Origin": "http://testserver"}
PASSWORD = "correct horse battery"


def post(client, path, body=None, headers=None):
    return client.post(f"/app/api{path}", json=body or {}, headers={**ORIGIN, **(headers or {})})


def signup(client, email="ada@example.com", company="Analytical Engines"):
    r = post(client, "/signup", {"email": email, "password": PASSWORD, "company": company})
    assert r.status_code == 201, r.text
    return r.json()


def code(r):
    return r.json()["error"]["code"]


# --- sign-up / sign-in ------------------------------------------------------------------------


def test_signup_creates_tenant_user_key_and_session(client):
    j = signup(client)
    assert j["tenant"]["name"] == "Analytical Engines" and j["tenant"]["plan"] == "free"
    assert j["api_key"].startswith("llk_") and j["keys"] == {"live": 1, "limit": 2}
    assert client.get("/app/api/me").json()["user"]["email"] == "ada@example.com"
    assert summarize(client, j["api_key"]).status_code == 200  # the first key works right away
    with SessionLocal() as db:
        user = db.scalars(select(User)).one()
        assert PASSWORD not in user.password_hash and user.password_hash.startswith("scrypt$")
        events = {e.event_type for e in db.scalars(select(AuditEvent))}
        assert {"tenant.created", "user.signup", "key.created"} <= events


def test_signup_cookie_is_httponly_and_strict(client):
    r = post(client, "/signup", {"email": "c@example.com", "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "secure" not in cookie  # APP_ENV=test serves plain http; production sets Secure


def test_signup_validation(client):
    assert (
        code(post(client, "/signup", {"email": "nope", "password": PASSWORD})) == "validation_error"
    )
    assert (
        code(post(client, "/signup", {"email": "a@b.io", "password": "short"}))
        == "validation_error"
    )
    signup(client, "dup@example.com")
    client.cookies.clear()
    r = post(client, "/signup", {"email": "DUP@example.com ", "password": PASSWORD})
    assert r.status_code == 409 and code(r) == "email_taken"


def test_company_defaults_to_email_domain(client):
    j = post(client, "/signup", {"email": "x@globex.io", "password": PASSWORD}).json()
    assert j["tenant"]["name"] == "globex.io"


def test_login_logout_and_generic_error(client):
    signup(client)
    client.cookies.clear()
    assert client.get("/app/api/me").status_code == 401

    wrong = post(client, "/login", {"email": "ada@example.com", "password": "wrong password"})
    unknown = post(client, "/login", {"email": "nobody@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]

    ok = post(client, "/login", {"email": "Ada@Example.com", "password": PASSWORD})
    assert ok.status_code == 200 and client.get("/app/api/me").status_code == 200
    assert post(client, "/logout").status_code == 204
    assert client.get("/app/api/me").status_code == 401
    with SessionLocal() as db:
        types = [e.event_type for e in db.scalars(select(AuditEvent))]
        assert types.count("user.login_failed") == 1  # unknown e-mails have no tenant to audit
        assert "user.login" in types and "user.logout" in types


def test_old_cookie_is_useless_after_logout(client):
    signup(client)
    token = client.cookies.get("ledger_session")
    post(client, "/logout")
    client.cookies.set("ledger_session", token)
    assert code(client.get("/app/api/me")) == "session_expired"


def test_expired_session_is_refused(client):
    signup(client)
    with SessionLocal() as db:
        db.execute(update(PortalSession).values(expires_at=utcnow() - timedelta(minutes=1)))
        db.commit()
    assert code(client.get("/app/api/me")) == "session_expired"


def test_login_is_throttled_per_email(client):
    signup(client)
    client.cookies.clear()
    statuses = [
        post(client, "/login", {"email": "ada@example.com", "password": "bad guess"}).status_code
        for _ in range(6)
    ]
    assert statuses[:5] == [401] * 5 and statuses[5] == 429
    # even the right password waits out the window
    r = post(client, "/login", {"email": "ada@example.com", "password": PASSWORD})
    assert r.status_code == 429 and "Retry-After" in r.headers


# --- CSRF guard -------------------------------------------------------------------------------


def test_state_changes_need_json_and_same_origin(client):
    signup(client)
    no_origin = client.post("/app/api/keys", json={"name": "x"})
    evil = client.post(
        "/app/api/keys", json={"name": "x"}, headers={"Origin": "https://evil.example"}
    )
    form = client.post(
        "/app/api/keys",
        content="name=x",
        headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert code(no_origin) == "cross_origin" and code(evil) == "cross_origin"
    assert form.status_code == 415
    assert client.get("/app/api/keys").json()["live"] == 1  # nothing was created


# --- keys -------------------------------------------------------------------------------------


def test_create_list_revoke_rotate(client):
    first = signup(client)
    r = post(client, "/keys", {"name": "ci-pipeline"})
    assert r.status_code == 201
    second = r.json()
    assert summarize(client, second["api_key"]).status_code == 200

    listed = client.get("/app/api/keys").json()
    assert listed["live"] == 2 and listed["limit"] == 2
    assert first["api_key"] not in str(listed) and second["api_key"] not in str(listed)

    rot = post(client, f"/keys/{second['key']['key_id']}/rotate")
    assert rot.status_code == 201 and rot.json()["rotated_from"] == second["key"]["key_id"]
    assert summarize(client, second["api_key"]).status_code == 401
    assert summarize(client, rot.json()["api_key"]).status_code == 200
    assert code(post(client, f"/keys/{second['key']['key_id']}/rotate")) == "key_revoked"

    rev = post(client, f"/keys/{rot.json()['key']['key_id']}/revoke")
    assert rev.status_code == 200 and rev.json()["revoked_at"]
    assert summarize(client, rot.json()["api_key"]).status_code == 401


def test_key_limit_per_plan(client):
    signup(client)  # free plan: 2 live keys, 1 already created at sign-up
    assert post(client, "/keys", {"name": "second"}).status_code == 201
    r = post(client, "/keys", {"name": "third"})
    assert r.status_code == 409 and code(r) == "key_limit_reached"
    # rotating at the limit is fine: the count does not change
    key_id = client.get("/app/api/keys").json()["keys"][0]["key_id"]
    assert post(client, f"/keys/{key_id}/rotate").status_code == 201
    # revoking frees a slot
    post(client, f"/keys/{key_id}/revoke")  # already revoked by the rotation: no-op
    live = [k for k in client.get("/app/api/keys").json()["keys"] if not k["revoked_at"]]
    post(client, f"/keys/{live[0]['key_id']}/revoke")
    assert post(client, "/keys", {"name": "third"}).status_code == 201


def test_cannot_touch_another_tenants_key(client):
    victim = signup(client, "victim@example.com")
    client.cookies.clear()
    signup(client, "mallory@example.com")
    victim_key = victim["key"]["key_id"]
    assert code(post(client, f"/keys/{victim_key}/revoke")) == "key_not_found"
    assert code(post(client, f"/keys/{victim_key}/rotate")) == "key_not_found"
    assert victim_key not in str(client.get("/app/api/keys").json())
    assert summarize(client, victim["api_key"]).status_code == 200


def test_suspended_tenant_can_sign_in_but_not_create_keys(client):
    j = signup(client)
    client.patch(f"/admin/tenants/{j['tenant']['id']}", json={"status": "suspended"}, headers=ADMIN)
    assert client.get("/app/api/usage").status_code == 200
    assert code(post(client, "/keys", {"name": "x"})) == "tenant_suspended"


def test_admin_can_exceed_the_portal_key_limit(client):
    j = signup(client)
    limit = get_plan("free").max_api_keys
    for i in range(limit + 1):
        r = client.post(
            f"/admin/tenants/{j['tenant']['id']}/keys", json={"name": f"ops-{i}"}, headers=ADMIN
        )
        assert r.status_code == 201
    assert client.get("/app/api/keys").json()["live"] == limit + 2


# --- usage ------------------------------------------------------------------------------------


def test_usage_per_key_adds_up_to_spent(client):
    j = signup(client)
    k1 = j["api_key"]
    k2 = post(client, "/keys", {"name": "batch"}).json()["api_key"]
    for key, n in ((k1, 2), (k2, 3)):
        for i in range(n):
            assert summarize(client, key, max_words=40 + i).status_code == 200

    u = client.get("/app/api/usage").json()
    by_key = {k["name"]: k for k in u["by_key"]}
    assert by_key["default"]["requests"] == 2 and by_key["batch"]["requests"] == 3
    with SessionLocal() as db:
        spent = db.scalars(select(BudgetPeriod)).one().spent_microusd
    assert round(sum(k["cost_usd"] for k in u["by_key"]) * 1e6) == spent
    assert round(sum(d["cost_usd"] for d in u["daily_by_key"]) * 1e6) == spent
    assert u["summary"]["requests"] == 5 and len(u["periods"]) == 6

    keys_view = {k["name"]: k for k in client.get("/app/api/keys").json()["keys"]}
    assert keys_view["batch"]["requests"] == 3 and keys_view["batch"]["last_used_at"]


def test_usage_for_a_past_month_and_statement(client):
    j = signup(client)
    summarize(client, j["api_key"])
    old = client.get("/app/api/usage", params={"period": "2001-01"}).json()
    assert old["summary"]["period"] == "2001-01" and old["summary"]["spent_usd"] == 0
    assert all(k["requests"] == 0 for k in old["by_key"])
    assert client.get("/app/api/usage", params={"period": "2026-13"}).status_code == 422
    csv = client.get("/app/api/statement.csv")
    assert csv.status_code == 200 and csv.text.splitlines()[-1].startswith("TOTAL")


def test_usage_needs_a_session(client):
    assert code(client.get("/app/api/usage")) == "not_signed_in"
    assert code(client.get("/app/api/statement.csv")) == "not_signed_in"


# --- pages ------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/app/login", "/app/signup", "/app", "/app/keys"])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "LedgerLLM" in r.text


def test_root_goes_to_the_portal(client):
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/app"


# --- concurrency (Postgres only) -------------------------------------------------------------


@pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="in-memory SQLite shares one connection across threads; only Postgres proves this",
)
def test_concurrent_creates_respect_the_limit(client):
    from concurrent.futures import ThreadPoolExecutor

    signup(client)  # free plan: 1 of 2 keys used
    with ThreadPoolExecutor(5) as pool:
        statuses = list(
            pool.map(lambda i: post(client, "/keys", {"name": f"k{i}"}).status_code, range(5))
        )
    assert statuses.count(201) == 1 and statuses.count(409) == 4, statuses
    with SessionLocal() as db:
        tenant = db.scalars(select(Tenant)).one()
        assert tenant is not None
    assert client.get("/app/api/keys").json()["live"] == 2
