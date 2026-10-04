"""Provider adapter tests. OWNER: Sai. The OpenAI-compatible adapter is exercised with a fake transport."""

import httpx
import pytest

from app.llm.base import ProviderError
from app.llm.openai_compat import PRESETS, OpenAICompatibleProvider


def _provider(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatibleProvider(
        name="gemini", base_url=PRESETS["gemini"], api_key="k", client=client
    )


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
