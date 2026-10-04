"""Rate limiting. OWNER: Suraj — remove the xfail markers once app/traffic/ratelimit.py is implemented."""

import pytest

from tests.conftest import make_tenant, summarize


def test_rate_limit_headers_present(client, api_key):
    r = summarize(client, api_key)
    assert r.headers["X-RateLimit-Limit"] == "60"
    assert "X-RateLimit-Remaining" in r.headers and "X-RateLimit-Reset" in r.headers


@pytest.mark.xfail(reason="TODO(Suraj): implement fixed-window limiter", strict=False)
def test_free_plan_sixth_request_in_a_minute_is_429(client):
    key = make_tenant(client, plan="free")["api_key"]
    statuses = [summarize(client, key).status_code for _ in range(6)]
    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429
    r = summarize(client, key)
    assert r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) >= 1
    assert r.headers["X-RateLimit-Remaining"] == "0"


@pytest.mark.xfail(reason="TODO(Suraj): remaining must decrement per request", strict=False)
def test_remaining_decrements(client):
    key = make_tenant(client, plan="free")["api_key"]
    r1 = summarize(client, key)
    r2 = summarize(client, key)
    assert int(r1.headers["X-RateLimit-Remaining"]) == int(r2.headers["X-RateLimit-Remaining"]) + 1


@pytest.mark.xfail(
    reason="TODO(Suraj): limits are per key; two keys of one tenant have independent windows",
    strict=False,
)
def test_limit_is_per_key(client):
    raise NotImplementedError
