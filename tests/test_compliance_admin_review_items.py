"""Compliance/admin review items: production refuses the public admin token; an override can be removed."""

import pytest

from app.config import DEFAULT_ADMIN_TOKEN, Settings, check_production_safety
from tests.conftest import ADMIN, make_tenant


@pytest.mark.parametrize("env", ["prod", "staging"])
@pytest.mark.parametrize("token", [DEFAULT_ADMIN_TOKEN, "short-token"])
def test_production_refuses_a_public_or_short_admin_token(env, token):
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
        check_production_safety(Settings(app_env=env, admin_token=token))


def test_production_accepts_a_real_token_and_dev_accepts_the_default():
    check_production_safety(Settings(app_env="prod", admin_token="x" * 64))
    check_production_safety(Settings(app_env="dev", admin_token=DEFAULT_ADMIN_TOKEN))
    check_production_safety(Settings(app_env="test", admin_token=DEFAULT_ADMIN_TOKEN))


def test_budget_override_can_be_removed(client):
    t = make_tenant(client, budget_override_usd=3.0)
    tid = t["tenant"]["id"]
    r = client.patch(f"/admin/tenants/{tid}", json={"plan": "pro"}, headers=ADMIN)
    assert r.json()["budget_override_usd"] == 3.0  # absent field: unchanged
    r = client.patch(f"/admin/tenants/{tid}", json={"budget_override_usd": None}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["budget_override_usd"] is None  # explicit null: removed
    usage = client.get("/v1/usage", headers={"X-API-Key": t["api_key"]}).json()
    assert float(usage["limit_usd"]) != 3.0
