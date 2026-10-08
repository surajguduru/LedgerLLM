"""Provider adapter tests. OWNER: Sai. The OpenAI-compatible adapter is exercised with a fake transport."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.llm import get_provider
from app.llm.base import ProviderError
from app.llm.openai_compat import PRESETS, OpenAICompatibleProvider, parse_retry_after
from app.models import AuditEvent, BudgetPeriod, UsageLedger
from tests.conftest import summarize


def _provider(handler, name="gemini", **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatibleProvider(
        name=name, base_url=PRESETS[name], api_key="k", client=client, **kwargs
    )


def _ok(content="- a", finish_reason="stop", usage=None):
    body = {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def _sent_payload(name="gemini", **kwargs) -> dict:
    """Run one completion and return the JSON body the provider sent."""
    seen: dict = {}

    def handler(request: httpx.Request):
        seen.update(json.loads(request.content))
        return _ok()

    _provider(handler, name=name, **kwargs).complete(model="m", system="s", user="u", max_tokens=50)
    return seen


def test_success_parses_text_and_usage():
    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer k"
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={
                "model": "gemini-3.8-flash",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "- a\n- b"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128},
            },
        )

    res = _provider(handler).complete(
        model="gemini-3.8-flash", system="s", user="u", max_tokens=100
    )
    assert res.text == "- a\n- b" and res.input_tokens == 120 and res.output_tokens == 8
    assert res.stop_reason == "stop" and res.model == "gemini-3.8-flash"


def test_rate_limit_is_retryable():
    p = _provider(lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}))
    with pytest.raises(ProviderError) as e:
        p.complete(model="m", system="s", user="u", max_tokens=10)
    assert e.value.retryable is True


def test_rate_limit_carries_the_retry_after_header():
    p = _provider(lambda r: httpx.Response(429, headers={"retry-after": "7"}, json={}), name="groq")
    with pytest.raises(ProviderError) as e:
        p.complete(model="m", system="s", user="u", max_tokens=10)
    assert e.value.retryable is True and e.value.retry_after_s == 7.0


def test_server_error_without_retry_after_has_no_hint():
    p = _provider(lambda r: httpx.Response(503, json={}))
    with pytest.raises(ProviderError) as e:
        p.complete(model="m", system="s", user="u", max_tokens=10)
    assert e.value.retryable is True and e.value.retry_after_s is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        (" 2.5 ", 2.5),
        ("0", 0.0),
        ("-3", 0.0),
        ("Thu, 08 Oct 2026 12:00:30 GMT", 30.0),
        ("Thu, 08 Oct 2026 11:59:00 GMT", 0.0),  # already past: no wait
        (None, None),
        ("", None),
        ("soon", None),
    ],
)
def test_parse_retry_after(value, expected):
    now = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after(value, now=now) == expected


def test_bad_request_is_not_retryable():
    p = _provider(lambda r: httpx.Response(400, json={"error": {"message": "bad model"}}))
    with pytest.raises(ProviderError) as e:
        p.complete(model="m", system="s", user="u", max_tokens=10)
    assert e.value.retryable is False


def test_timeout_is_retryable():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ProviderError) as e:
        _provider(handler).complete(model="m", system="s", user="u", max_tokens=10)
    assert e.value.retryable is True


def test_missing_usage_falls_back_to_estimate():
    p = _provider(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "hello world"}}]})
    )
    res = p.complete(model="m", system="sys", user="user text", max_tokens=10)
    assert res.input_tokens > 0 and res.output_tokens > 0


def test_gemini_preset_sends_low_reasoning_effort_by_default():
    assert _sent_payload("gemini")["reasoning_effort"] == "low"


def test_configured_reasoning_effort_is_sent():
    assert _sent_payload("gemini", reasoning_effort="high")["reasoning_effort"] == "high"


def test_groq_preset_sends_no_reasoning_effort():
    assert "reasoning_effort" not in _sent_payload("groq")


@pytest.mark.parametrize("value", ["", None])
def test_empty_reasoning_effort_sends_nothing(value):
    assert "reasoning_effort" not in _sent_payload("gemini", reasoning_effort=value)


@pytest.mark.parametrize(("env", "expected"), [(None, "low"), ("", None), ("medium", "medium")])
def test_get_provider_threads_reasoning_effort_setting(monkeypatch, env, expected):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_API_KEY", "k")
    if env is None:
        monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("LLM_REASONING_EFFORT", env)
    get_settings.cache_clear()
    get_provider.cache_clear()
    try:
        assert get_provider().reasoning_effort == expected
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        get_provider.cache_clear()


def test_reasoning_tokens_default_to_zero_when_not_reported():
    usage = {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128}
    res = _provider(lambda r: _ok(usage=usage)).complete(
        model="m", system="s", user="u", max_tokens=100
    )
    assert res.reasoning_tokens == 0 and res.output_tokens == 8


def test_reasoning_tokens_parsed_from_completion_details():
    # OpenAI style: reasoning is itemised and already included in completion_tokens.
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 90,
        "total_tokens": 190,
        "completion_tokens_details": {"reasoning_tokens": 64},
    }
    res = _provider(lambda r: _ok(usage=usage)).complete(
        model="m", system="s", user="u", max_tokens=100
    )
    assert res.reasoning_tokens == 64 and res.output_tokens == 90


def test_gemini_hidden_reasoning_is_billed_as_output():
    # Gemini style, observed 2026-10-08: reasoning only shows up in total_tokens.
    usage = {"prompt_tokens": 355, "completion_tokens": 6, "total_tokens": 601}
    res = _provider(lambda r: _ok(content="- The council voted", usage=usage)).complete(
        model="gemini-3.8-flash", system="s", user="u", max_tokens=250
    )
    assert res.reasoning_tokens == 240
    assert res.output_tokens == 246 <= 250  # still within the reserved max_tokens


@pytest.mark.parametrize("content", ["", "  \n", None])
def test_length_stop_with_no_text_is_a_non_retryable_error(content):
    usage = {"prompt_tokens": 355, "completion_tokens": 0, "total_tokens": 375}
    p = _provider(lambda r: _ok(content=content, finish_reason="length", usage=usage))
    with pytest.raises(ProviderError, match="exhausted the output budget") as e:
        p.complete(model="gemini-3.8-flash", system="s", user="u", max_tokens=20)
    assert e.value.retryable is False and e.value.code == "upstream_error"


def test_length_stop_with_text_returns_the_truncated_text():
    p = _provider(lambda r: _ok(content="- partial", finish_reason="length"))
    res = p.complete(model="m", system="s", user="u", max_tokens=20)
    assert res.text == "- partial" and res.stop_reason == "length"


def test_exhausted_budget_returns_502_and_bills_nothing(client, api_key, monkeypatch):
    empty = _provider(
        lambda r: _ok(
            content="",
            finish_reason="length",
            usage={"prompt_tokens": 400, "completion_tokens": 0, "total_tokens": 700},
        )
    )
    monkeypatch.setattr("app.api.summarize.get_provider", lambda: empty)
    r = summarize(client, api_key)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"
    assert "Retry-After" not in r.headers  # not retryable: same budget, same outcome
    with SessionLocal() as db:
        bp = db.scalars(select(BudgetPeriod)).one()
        assert bp.reserved_microusd == 0 and bp.spent_microusd == 0
        assert db.scalar(select(func.count()).select_from(UsageLedger)) == 0
        assert db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "request.upstream_error")
        ).one()


def _capture(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    return handler


def test_response_format_is_off_by_default():
    seen: dict = {}
    _provider(_capture(seen)).complete(model="m", system="s", user="u", max_tokens=10)
    assert "response_format" not in seen


def test_response_format_is_passed_through():
    seen: dict = {}
    p = _provider(_capture(seen))
    assert p.supports_response_format is True
    p.complete(
        model="m", system="s", user="u", max_tokens=10, response_format={"type": "json_object"}
    )
    assert seen["response_format"] == {"type": "json_object"}
