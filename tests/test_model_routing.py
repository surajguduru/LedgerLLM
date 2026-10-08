"""Model routing (app/llm/router.py, D26): each model goes to the provider that serves it."""

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.llm.base import LLMResult, ProviderError
from app.models import BudgetPeriod, UsageLedger
from tests.conftest import make_tenant, summarize

GROQ_MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]


@pytest.fixture
def env(monkeypatch):
    """Set env vars for settings/get_provider(); caches cleared before and after."""
    from app.config import get_settings
    from app.llm import get_provider

    def apply(**values):
        for k, v in values.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        get_provider.cache_clear()

    yield apply
    monkeypatch.undo()
    get_settings.cache_clear()
    get_provider.cache_clear()


def test_every_groq_model_is_priced_and_routed_to_groq():
    from app.billing.pricing import load_prices

    for m in GROQ_MODELS:
        assert load_prices().models[m].provider == "groq"


def test_models_route_to_their_provider_with_its_own_key(env):
    from app.llm import get_provider, model_available, model_provider

    env(LLM_PROVIDER="gemini", LLM_API_KEY="gem-key", GROQ_API_KEY="groq-key")
    router = get_provider()
    assert model_provider("gemini-3.8-flash") == "gemini"
    assert router._for("gemini-3.8-flash") is router.default
    groq = router._for("qwen/qwen3.8-27b")
    assert groq.name == "groq" and groq._headers["Authorization"] == "Bearer groq-key"
    assert groq.reasoning_effort is None  # the Gemini preset's effort is not sent to Groq
    assert router._for("openai/gpt-oss-20b") is groq  # built once, reused
    assert all(model_available(m) for m in GROQ_MODELS)


def test_groq_as_the_primary_uses_llm_api_key(env):
    from app.llm import get_provider, model_available

    env(LLM_PROVIDER="groq", LLM_API_KEY="primary-groq", GROQ_API_KEY="", GEMINI_API_KEY="")
    router = get_provider()
    assert router._for("openai/gpt-oss-120b") is router.default
    assert router.default._headers["Authorization"] == "Bearer primary-groq"
    assert not model_available("gemini-3.8-flash")  # no GEMINI_API_KEY: Gemini models unavailable


def test_a_model_whose_provider_has_no_key_is_unavailable(env):
    from app.llm import get_provider, model_available

    env(LLM_PROVIDER="gemini", LLM_API_KEY="k", GROQ_API_KEY="")  # "" overrides a local .env
    assert not model_available("qwen/qwen3.8-27b")
    with pytest.raises(ProviderError) as exc:
        get_provider().complete(model="qwen/qwen3.8-27b", system="s", user="u", max_tokens=5)
    assert exc.value.code == "model_unavailable" and exc.value.retryable is False
    assert "GROQ_API_KEY" in str(exc.value)


def test_unpriced_or_unlabelled_model_goes_to_the_primary(env):
    from app.llm import model_available, model_provider

    env(LLM_PROVIDER="gemini", LLM_API_KEY="k")
    assert model_provider("some-unknown-model") == "gemini"
    assert model_available("some-unknown-model")


def test_response_format_is_dropped_for_a_provider_without_json_mode():
    from app.llm.router import ModelRouter

    seen = {}

    class NoJson:
        name = "x"
        supports_response_format = False

        def complete(self, **kw):
            seen.update(kw)
            return LLMResult(
                text="ok", model=kw["model"], input_tokens=1, output_tokens=1, latency_ms=0
            )

    r = ModelRouter(NoJson(), default_name="mock", build=lambda n: NoJson())
    r.complete(
        model="m", system="s", user="u", max_tokens=5, response_format={"type": "json_object"}
    )
    assert "response_format" not in seen


def test_tenant_can_summarize_with_a_groq_model_on_its_plan(client):
    # LLM_PROVIDER=mock in tests: every model is served by the mock, nothing leaves the process.
    key = make_tenant(client, plan="free")["api_key"]
    r = summarize(client, key, model="openai/gpt-oss-20b")
    assert r.status_code == 200, r.text
    assert r.json()["usage"]["model"] == "openai/gpt-oss-20b" and r.json()["usage"]["cost_usd"] > 0
    pro = make_tenant(client, plan="pro", name="pro")["api_key"]
    for m in GROQ_MODELS:
        assert summarize(client, pro, model=m).status_code == 200, m
    with SessionLocal() as db:
        models = set(db.scalars(select(UsageLedger.model)))
    assert set(GROQ_MODELS) <= models


def test_groq_models_not_on_the_free_plan_are_refused(client):
    key = make_tenant(client, plan="free")["api_key"]
    r = summarize(client, key, model="qwen/qwen3.8-27b")
    assert r.status_code == 403 and r.json()["error"]["code"] == "model_not_allowed"


def test_unavailable_model_is_refused_before_any_budget_is_reserved(client, api_key, monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "gemini")
    monkeypatch.setattr(s, "llm_api_key", "k")
    monkeypatch.setattr(s, "groq_api_key", None)
    r = summarize(client, api_key, model="qwen/qwen3.8-27b")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "model_not_allowed"
    assert "not available on this deployment" in r.json()["error"]["message"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(BudgetPeriod)) == 0
        assert db.scalar(select(func.count()).select_from(UsageLedger)) == 0


class _Capture:
    def __init__(self):
        self.payloads = []

    def post(self, url, json, headers):
        import httpx

        self.payloads.append(json)
        body = {
            "model": json["model"],
            "choices": [{"message": {"content": "- ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))


def _groq(**kw):
    from app.llm.openai_compat import OpenAICompatibleProvider

    cap = _Capture()
    p = OpenAICompatibleProvider(
        name="groq", base_url="https://api.groq.com/openai/v1", api_key="k", client=cap, **kw
    )
    return p, cap


def test_gpt_oss_reasons_low_by_default_and_other_models_send_nothing():
    p, cap = _groq()
    p.complete(model="openai/gpt-oss-20b", system="s", user="u", max_tokens=50)
    p.complete(model="qwen/qwen3.8-27b", system="s", user="u", max_tokens=50)
    assert cap.payloads[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in cap.payloads[1]


def test_an_explicit_reasoning_effort_overrides_the_model_default():
    p, cap = _groq(reasoning_effort="high")
    p.complete(model="openai/gpt-oss-120b", system="s", user="u", max_tokens=50)
    assert cap.payloads[0]["reasoning_effort"] == "high"
    p2, cap2 = _groq(reasoning_effort="")  # "" = send nothing, also for gpt-oss
    p2.complete(model="openai/gpt-oss-120b", system="s", user="u", max_tokens=50)
    assert "reasoning_effort" not in cap2.payloads[0]


def test_reasoning_models_get_output_headroom_and_others_do_not():
    from app.feature.prompts import load_prompt
    from app.feature.summarize import output_token_cap
    from app.llm import reasoning_allowance

    prompt = load_prompt()
    assert reasoning_allowance("openai/gpt-oss-120b") == 1024
    assert reasoning_allowance("qwen/qwen3.8-27b") == 0
    assert reasoning_allowance("not-in-the-table") == 0
    base = output_token_cap(prompt, 40)
    assert output_token_cap(prompt, 40, reasoning_tokens=1024) == base + 1024


def test_reservation_covers_the_reasoning_headroom(client, api_key, monkeypatch):
    import app.api.summarize as pipeline

    caps = []
    real = pipeline.estimate_cost_microusd

    def spy(model, est_in, cap):
        caps.append((model, cap))
        return real(model, est_in, cap)

    monkeypatch.setattr(pipeline, "estimate_cost_microusd", spy)
    assert summarize(client, api_key, model="qwen/qwen3.8-27b", max_words=40).status_code == 200
    assert summarize(client, api_key, model="openai/gpt-oss-120b", max_words=40).status_code == 200
    (_, plain), (_, reasoning) = caps
    assert reasoning == plain + 1024
