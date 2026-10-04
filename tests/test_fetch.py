"""URL fetching. OWNER: Sai — SSRF guard + extraction tests. Network tests are skipped in CI by default."""

import pytest

from tests.conftest import summarize


@pytest.mark.xfail(
    reason="TODO(Sai): SSRF guard — private/loopback/metadata IPs must be refused with fetch_blocked",
    strict=False,
)
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/healthz",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://localhost/",
    ],
)
def test_ssrf_targets_are_blocked(client, api_key, url):
    r = summarize(client, api_key, text=None, url=url)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "fetch_blocked"


def test_non_http_scheme_rejected(client, api_key):
    # pydantic's HttpUrl already rejects non-http(s) schemes at validation time
    r = summarize(client, api_key, text=None, url="ftp://example.com/file.txt")
    assert r.status_code == 422


def test_text_longer_than_plan_limit_is_truncated_and_flagged(client):
    from tests.conftest import make_tenant

    key = make_tenant(client, plan="free")["api_key"]  # 20k chars
    r = summarize(client, key, text="word " * 6000)
    assert r.status_code == 200
    assert r.json()["source"]["truncated"] is True
    assert r.json()["source"]["chars"] == 20000
