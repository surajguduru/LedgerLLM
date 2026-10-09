"""Budget headers: X-Budget-Warning on every response past the soft threshold, exact amounts in text."""

from app.config import get_settings
from app.plans import format_usd
from tests.conftest import ADMIN, make_tenant, summarize


def code(r):
    return r.json()["error"]["code"]


def test_format_usd_is_exact_for_small_amounts():
    assert format_usd(4_000) == "0.004"
    assert format_usd(437) == "0.000437"
    assert format_usd(10_000_000) == "10.00"
    assert format_usd(500_000) == "0.50"
    assert format_usd(0) == "0.00"


def _set_budget(client, tenant_id, usd):
    r = client.patch(
        f"/admin/tenants/{tenant_id}", json={"budget_override_usd": usd}, headers=ADMIN
    )
    assert r.status_code == 200, r.text


def test_402_carries_the_warning_and_an_accurate_message(client):
    t = make_tenant(client)
    first = summarize(client, t["api_key"])
    assert first.status_code == 200
    spent = first.json()["budget"]["spent_usd"]
    _set_budget(client, t["tenant"]["id"], spent)  # 100 % committed: the next request cannot fit

    r = summarize(client, t["api_key"], max_words=101)
    assert r.status_code == 402 and code(r) == "budget_exceeded"
    assert "X-Budget-Warning" in r.headers and "X-Budget-Limit-USD" in r.headers
    message = r.json()["error"]["message"]
    assert "estimated cost $" in message and "exceeds remaining $0.00" in message
    assert f"of the ${spent:g} monthly budget" in message, message
    assert "exhausted" not in message


def test_cache_hit_carries_the_warning(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "response_cache_enabled", True)
    t = make_tenant(client)
    first = summarize(client, t["api_key"])
    spent = first.json()["budget"]["spent_usd"]
    _set_budget(client, t["tenant"]["id"], round(spent / 0.9, 6))

    hit = summarize(client, t["api_key"])
    assert hit.json()["usage"]["cached"] is True
    assert hit.json()["budget"]["warning"] is True
    assert "X-Budget-Warning" in hit.headers


def test_warning_header_formats_a_sub_cent_limit_exactly(client):
    key = make_tenant(client, budget_override_usd=0.004)["api_key"]
    warnings = []
    for i in range(10):
        r = summarize(client, key, max_words=100 + i)
        warnings += [r.headers["X-Budget-Warning"]] if "X-Budget-Warning" in r.headers else []
        if r.status_code != 200:
            break
    assert warnings and all(w.endswith("of 0.004 USD committed this period") for w in warnings)
