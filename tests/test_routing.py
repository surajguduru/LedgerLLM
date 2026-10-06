"""Plan-aware model routing: a request without `model` gets its plan's default_model."""

import pytest

from app.plans import get_plan, parse_plan
from tests.conftest import make_tenant, summarize


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ("free", "gemini-3.5-flash-lite"),
        ("pro", "gemini-3.8-flash"),
        ("enterprise", "gemini-3.8-flash"),
    ],
)
def test_request_without_model_uses_plan_default(client, plan, expected):
    key = make_tenant(client, plan=plan)["api_key"]
    r = summarize(client, key)
    assert r.status_code == 200, r.text
    assert r.json()["usage"]["model"] == expected


def test_explicit_model_overrides_plan_default(client):
    key = make_tenant(client, plan="free")["api_key"]
    r = summarize(client, key, model="gemini-3.8-flash")
    assert r.json()["usage"]["model"] == "gemini-3.8-flash"


def test_explicit_model_still_checked_against_plan(client):
    key = make_tenant(client, plan="free")["api_key"]
    r = summarize(client, key, model="claude-opus-5-5")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "model_not_allowed"


def test_free_default_is_cheaper_than_pro_default(client):
    free = make_tenant(client, plan="free", name="f")["api_key"]
    pro = make_tenant(client, plan="pro", name="p")["api_key"]
    assert (
        summarize(client, free).json()["usage"]["cost_usd"]
        < (summarize(client, pro).json()["usage"]["cost_usd"])
    )


def test_every_plan_default_is_allowed():
    for name in ("free", "pro", "enterprise"):
        plan = get_plan(name)
        assert plan.default_model in plan.allowed_models


def test_default_outside_allowed_models_is_rejected_at_load():
    raw = {
        "rpm": 1,
        "monthly_budget_usd": 1,
        "max_input_chars": 10,
        "allowed_models": ["a"],
        "default_model": "b",
    }
    with pytest.raises(ValueError, match="default_model"):
        parse_plan("broken", raw)


def test_plan_without_default_falls_back_to_settings():
    raw = {"rpm": 1, "monthly_budget_usd": 1, "max_input_chars": 10, "allowed_models": ["a"]}
    assert parse_plan("bare", raw).default_model is None
