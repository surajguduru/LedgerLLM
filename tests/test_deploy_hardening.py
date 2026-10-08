"""Deployment: /metrics access, client IPs behind the proxy, the commit on /healthz."""

from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import get_settings
from app.main import create_app

ORIGIN = {"Origin": "http://testserver"}


def code(r):
    return r.json()["error"]["code"]


# --- /metrics ----------------------------------------------------------------------------------


def test_metrics_open_in_dev_without_a_token(client):
    assert client.get("/metrics").status_code == 200


def test_metrics_need_the_token_when_set(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "metrics_token", "s3cret-metrics")
    assert client.get("/metrics").status_code == 401
    r = client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and code(r) == "metrics_unauthorized"
    r = client.get("/metrics", headers={"Authorization": "Bearer s3cret-metrics"})
    assert r.status_code == 200 and "ledgerllm_" in r.text


def test_metrics_hidden_in_production_without_a_token(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "app_env", "prod")
    r = client.get("/metrics")
    assert r.status_code == 404 and code(r) == "not_found"


# --- client IP behind a proxy ------------------------------------------------------------------


def test_signup_throttle_keys_on_the_forwarded_client():
    # What uvicorn --proxy-headers --forwarded-allow-ips='*' puts in front of the app on Render.
    app = ProxyHeadersMiddleware(create_app(), trusted_hosts="*")
    with TestClient(app) as c:

        def attempt(ip):
            return c.post(
                "/app/api/signup",
                json={"email": "not-an-email", "password": "correct horse battery"},
                headers={**ORIGIN, "X-Forwarded-For": f"{ip}, 10.0.0.1"},
            ).status_code

        assert [attempt("203.0.113.7") for _ in range(10)] == [422] * 10
        assert attempt("203.0.113.7") == 429
        assert attempt("198.51.100.2") == 422  # a different client is not throttled


# --- /healthz ----------------------------------------------------------------------------------


def test_healthz_reports_the_deployed_commit(client, monkeypatch):
    assert client.get("/healthz").json()["version"] is None
    monkeypatch.setattr(get_settings(), "git_commit", "0123456789abcdef0123")
    assert client.get("/healthz").json()["version"] == "0123456"
