"""Provider fallback chain (app/llm/fallback.py). OWNER: Sai. Two OpenAI-compatible providers on MockTransports."""

from __future__ import annotations

import json

import httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select

from app.billing.pricing import compute_cost_microusd
from app.db import SessionLocal
from app.llm.base import ProviderError
from app.llm.fallback import FallbackProvider, primary_only
from app.llm.openai_compat import PRESETS, OpenAICompatibleProvider
from app.models import BudgetPeriod, UsageLedger
from tests.conftest import make_tenant, summarize

PRIMARY, SECONDARY = "gemini-3.8-flash", "gemini-3.5-flash-lite"
USAGE = {"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340}


def _ok(content="- answered", model=None):
    body = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": USAGE}
    if model:
        body["model"] = model
    return httpx.Response(200, json=body)


def _provider(handler, name="gemini"):
    calls: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request.read().decode())
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    p = OpenAICompatibleProvider(name=name, base_url=PRESETS[name], api_key="k", client=client)
    return p, calls


def _raise_timeout(request):
    raise httpx.ReadTimeout("slow")


def _fallbacks(from_model=PRIMARY, to_model=SECONDARY) -> float:
    value = REGISTRY.get_sample_value(
        "ledgerllm_provider_fallbacks_total", {"from_model": from_model, "to_model": to_model}
    )
    return value or 0.0


def _chain(primary_handler, secondary_handler=lambda r: _ok(), secondary_name="gemini"):
    primary, p_calls = _provider(primary_handler)
    secondary, s_calls = _provider(secondary_handler, name=secondary_name)
    chain = FallbackProvider(primary, secondary, secondary_model=SECONDARY)
    return chain, p_calls, s_calls


def _complete(chain, model=PRIMARY):
    return chain.complete(model=model, system="s", user="u", max_tokens=100)


def test_primary_success_does_not_touch_the_secondary():
    chain, p_calls, s_calls = _chain(lambda r: _ok())
    res = _complete(chain)
    assert res.model == PRIMARY and res.provider == "gemini" and res.fallback_from is None
    assert len(p_calls) == 1 and s_calls == []


def test_primary_429_falls_back_once_and_counts_it():
    before = _fallbacks()
    chain, p_calls, s_calls = _chain(lambda r: httpx.Response(429, json={}))
    res = _complete(chain)
    assert res.text == "- answered"
    assert res.model == SECONDARY and res.fallback_from == PRIMARY
    assert len(p_calls) == 1 and len(s_calls) == 1
    assert f'"model":"{SECONDARY}"' in s_calls[0].replace(" ", "")
    assert _fallbacks() == before + 1


def test_result_names_the_configured_model_not_the_echoed_alias():
    # The pricing key must be the configured name; APIs can echo a versioned alias instead.
    chain, _, _ = _chain(
        lambda r: httpx.Response(503, json={}),
        lambda r: _ok(model="models/gemini-3.5-flash-lite-preview-09-2026"),
    )
    assert _complete(chain).model == SECONDARY


def test_primary_400_is_not_retried_on_the_secondary():
    before = _fallbacks()
    chain, _, s_calls = _chain(lambda r: httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(ProviderError) as e:
        _complete(chain)
    assert e.value.retryable is False and s_calls == []
    assert _fallbacks() == before


def test_primary_timeout_falls_back():
    chain, _, s_calls = _chain(_raise_timeout)
    res = _complete(chain)
    assert res.fallback_from == PRIMARY and len(s_calls) == 1


def test_both_failing_raises_the_secondary_error_caused_by_the_primary():
    chain, p_calls, s_calls = _chain(
        lambda r: httpx.Response(429, json={}), lambda r: httpx.Response(503, json={})
    )
    with pytest.raises(ProviderError, match="error 503") as e:
        _complete(chain)
    assert isinstance(e.value.__cause__, ProviderError)
    assert "rate limited" in str(e.value.__cause__)
    assert len(p_calls) == 1 and len(s_calls) == 1  # exactly one fallback attempt


def test_requesting_the_fallback_model_never_falls_back_to_itself():
    before = _fallbacks(SECONDARY, SECONDARY)
    chain, p_calls, s_calls = _chain(lambda r: httpx.Response(429, json={}))
    with pytest.raises(ProviderError, match="rate limited"):
        _complete(chain, model=SECONDARY)
    assert len(p_calls) == 1 and s_calls == []
    assert _fallbacks(SECONDARY, SECONDARY) == before


def test_a_different_secondary_provider_reports_its_own_name():
    chain, _, _ = _chain(lambda r: httpx.Response(429, json={}), secondary_name="groq")
    assert _complete(chain).provider == "groq"


def test_extra_kwargs_reach_both_ends_and_json_mode_needs_both():
    seen: list[dict] = []

    def capture(request):
        seen.append(json.loads(request.content))
        return httpx.Response(429, json={}) if len(seen) == 1 else _ok("{}")

    chain, _, _ = _chain(capture, capture)
    assert chain.supports_response_format is True
    res = chain.complete(
        model=PRIMARY, system="s", user="u", max_tokens=10, response_format={"type": "json_object"}
    )
    assert res.fallback_from == PRIMARY
    assert [p["response_format"] for p in seen] == [{"type": "json_object"}] * 2


def test_primary_only_unwraps_the_chain():
    chain, _, _ = _chain(lambda r: _ok())
    assert primary_only(chain) is chain.primary
    assert primary_only(chain.primary) is chain.primary


# --- configuration (LLM_FALLBACK_*) -------------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    """Set env vars for get_provider(); caches are cleared before and after."""
    from app.config import get_settings
    from app.llm import get_provider

    def apply(**values):
        for k, v in values.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        get_provider.cache_clear()

    yield apply
    monkeypatch.undo()
    get_settings.cache_clear()
    get_provider.cache_clear()


def test_fallback_is_off_by_default(env):
    from app.llm import fallback_model, get_provider

    env(LLM_PROVIDER="gemini", LLM_API_KEY="k")
    assert fallback_model() is None
    assert isinstance(get_provider(), OpenAICompatibleProvider)


def test_same_provider_fallback_reuses_the_primary_key(env):
    from app.llm import fallback_model, get_provider

    env(
        LLM_PROVIDER="gemini",
        LLM_API_KEY="k",
        LLM_TIMEOUT_S="0.5",
        LLM_FALLBACK_PROVIDER="gemini",
        LLM_FALLBACK_MODEL=SECONDARY,
        LLM_FALLBACK_TIMEOUT_S="20",
    )
    chain = get_provider()
    assert isinstance(chain, FallbackProvider) and fallback_model() == SECONDARY
    assert chain.secondary._headers["Authorization"] == "Bearer k"
    assert chain.primary._client.timeout.read == 0.5
    assert chain.secondary._client.timeout.read == 20


def test_other_provider_fallback_needs_its_own_key(env):
    from app.llm import get_provider

    env(
        LLM_PROVIDER="gemini",
        LLM_API_KEY="k",
        LLM_REASONING_EFFORT="medium",
        LLM_FALLBACK_PROVIDER="groq",
        LLM_FALLBACK_MODEL="llama-3.1-8b-instant",
    )
    with pytest.raises(ValueError, match="LLM_FALLBACK_API_KEY"):
        get_provider()
    env(LLM_FALLBACK_API_KEY="g")
    chain = get_provider()
    assert chain.secondary.name == "groq"
    assert chain.secondary._headers["Authorization"] == "Bearer g"
    assert chain.secondary.reasoning_effort is None  # the Gemini override is not sent to Groq
    assert chain.primary.reasoning_effort == "medium"


def test_fallback_provider_without_a_model_is_a_config_error(env):
    from app.llm import get_provider

    env(LLM_PROVIDER="mock", LLM_FALLBACK_PROVIDER="mock")
    with pytest.raises(ValueError, match="LLM_FALLBACK_MODEL"):
        get_provider()


# --- pipeline: reserve, entitlement, settle and book by the answering model -------------------


@pytest.fixture
def spy_reserve(monkeypatch):
    """Records every estimate the pipeline computes and the amount it reserves."""
    import app.api.summarize as pipeline

    seen: dict = {"estimates": {}, "reserved": []}
    real_estimate, real_reserve = pipeline.estimate_cost_microusd, pipeline.budget.reserve

    def estimate(model, *args):
        seen["estimates"][model] = real_estimate(model, *args)
        return seen["estimates"][model]

    def reserve(db, tenant, plan, est):
        seen["reserved"].append(est)
        return real_reserve(db, tenant, plan, est)

    monkeypatch.setattr(pipeline, "estimate_cost_microusd", estimate)
    monkeypatch.setattr(pipeline.budget, "reserve", reserve)
    return seen


def _serve(monkeypatch, env, chain, fallback=SECONDARY):
    """Turn the chain on in settings and make the pipeline use `chain`."""
    chain.secondary_model = fallback
    env(LLM_FALLBACK_PROVIDER="gemini", LLM_FALLBACK_MODEL=fallback)
    monkeypatch.setattr("app.api.summarize.get_provider", lambda: chain)


def _ledger_rows() -> list[tuple[str, int]]:
    with SessionLocal() as db:
        return [(r.model, r.cost_microusd) for r in db.scalars(select(UsageLedger))]


def _budget() -> tuple[int, int]:
    with SessionLocal() as db:
        bp = db.scalars(select(BudgetPeriod)).one()
        return bp.reserved_microusd, bp.spent_microusd


def test_fallback_answer_is_billed_at_the_fallback_price(
    client, api_key, monkeypatch, env, spy_reserve
):
    chain, _, s_calls = _chain(lambda r: httpx.Response(429, json={}))
    _serve(monkeypatch, env, chain)
    r = summarize(client, api_key)  # pro plan: default gemini-3.8-flash, flash-lite allowed
    assert r.status_code == 200, r.text
    usage = r.json()["usage"]
    assert usage["model"] == SECONDARY and usage["fallback_from"] == PRIMARY
    assert usage["provider"] == "gemini" and len(s_calls) == 1
    cost = compute_cost_microusd(SECONDARY, 300, 40)
    assert usage["cost_usd"] == cost / 1_000_000
    assert _ledger_rows() == [(SECONDARY, cost)]
    assert _budget() == (0, cost)  # the reservation is fully released at settle
    assert set(spy_reserve["estimates"]) == {PRIMARY, SECONDARY}


def test_fallback_not_on_the_plan_is_never_called(client, monkeypatch, env, spy_reserve):
    key = make_tenant(client, plan="free")["api_key"]  # free plan: no gemini-3.1-flash-lite
    chain, p_calls, s_calls = _chain(lambda r: httpx.Response(429, json={}))
    _serve(monkeypatch, env, chain, fallback="gemini-3.1-flash-lite")
    r = summarize(client, key, model=PRIMARY)
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_error"
    assert len(p_calls) == 1 and s_calls == []
    assert _ledger_rows() == [] and _budget() == (0, 0)
    assert set(spy_reserve["estimates"]) == {PRIMARY}  # nothing reserved for a model it can't use


def test_reservation_covers_a_pricier_fallback(client, monkeypatch, env, spy_reserve):
    pricey = "claude-sonnet-5-5"  # enterprise plan only; dearer than gemini-3.8-flash per token
    key = make_tenant(client, plan="enterprise")["api_key"]
    chain, _, _ = _chain(lambda r: httpx.Response(503, json={}))
    _serve(monkeypatch, env, chain, fallback=pricey)
    r = summarize(client, key, model=PRIMARY)
    assert r.status_code == 200, r.text
    est = spy_reserve["estimates"]
    assert est[pricey] > est[PRIMARY]
    assert spy_reserve["reserved"] == [est[pricey]]
    cost = compute_cost_microusd(pricey, 300, 40)
    assert cost <= est[pricey] and _ledger_rows() == [(pricey, cost)]


def test_both_models_failing_returns_502_and_bills_nothing(client, api_key, monkeypatch, env):
    chain, p_calls, s_calls = _chain(
        lambda r: httpx.Response(429, json={}), lambda r: httpx.Response(503, json={})
    )
    _serve(monkeypatch, env, chain)
    r = summarize(client, api_key)
    assert r.status_code == 502 and r.headers["Retry-After"] == "2"
    assert len(p_calls) == 1 and len(s_calls) == 1
    assert _ledger_rows() == [] and _budget() == (0, 0)


def test_without_fallback_settings_the_pipeline_is_unchanged(client, api_key, spy_reserve):
    r = summarize(client, api_key)
    assert r.status_code == 200
    usage = r.json()["usage"]
    assert usage["model"] == PRIMARY and usage["fallback_from"] is None
    assert usage["provider"] == "mock"
    assert list(spy_reserve["estimates"]) == [PRIMARY]
    assert spy_reserve["reserved"] == [spy_reserve["estimates"][PRIMARY]]
