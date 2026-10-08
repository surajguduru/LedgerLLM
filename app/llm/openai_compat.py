"""OpenAI-compatible chat-completions provider over plain httpx.

Works for Gemini (default; free tier at https://aistudio.google.com/apikey), Groq, OpenAI, OpenRouter and Ollama —
anything that speaks POST {base_url}/chat/completions. No vendor SDK, one dependency we already have.

Reasoning models (Gemini 3, OpenAI o-series) spend hidden "thinking" tokens out of the same `max_tokens`
budget as the visible answer. Left at the provider default, gemini-3.8-flash used 240 of a 250-token
budget on reasoning and returned 29 characters. `reasoning_effort` is therefore sent for presets that need
it; the default per preset is the lowest value every model on that endpoint accepts (measured
2026-10-08: Gemini rejects "none" on 3.5-flash-lite and "minimal" on 3.8-flash, so "low").
"""

from __future__ import annotations

from time import perf_counter

import httpx

from app.llm.base import LLMResult, ProviderError
from app.llm.mock import estimate_tokens

PRESETS: dict[str, str] = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
}

# reasoning_effort sent when the caller does not choose one. Presets not listed send nothing, because
# their non-reasoning models reject the parameter.
REASONING_EFFORT_DEFAULTS: dict[str, str] = {"gemini": "low"}

_PRESET_DEFAULT = object()


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | None,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
        reasoning_effort: str | None | object = _PRESET_DEFAULT,
    ) -> None:
        """`reasoning_effort` left unset uses the preset default; None or "" sends nothing."""
        self.name = name
        if reasoning_effort is _PRESET_DEFAULT:
            reasoning_effort = REASONING_EFFORT_DEFAULTS.get(name)
        self.reasoning_effort = str(reasoning_effort) if reasoning_effort else None
        self._base_url = base_url.rstrip("/")
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.Client(timeout=timeout_s)

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> LLMResult:
        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        t0 = perf_counter()
        try:
            r = self._client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=self._headers
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name} timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name} unreachable: {exc.__class__.__name__}", retryable=True
            ) from exc
        latency_ms = int((perf_counter() - t0) * 1000)

        if r.status_code == 429:
            raise ProviderError(f"{self.name} rate limited", retryable=True)
        if r.status_code >= 500:
            raise ProviderError(f"{self.name} error {r.status_code}", retryable=True)
        if r.status_code >= 400:
            detail = r.text[:200].replace("\n", " ")
            raise ProviderError(
                f"{self.name} rejected the request ({r.status_code}): {detail}", retryable=False
            )

        try:
            data = r.json()
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name} returned an unexpected body", retryable=True) from exc

        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            model=data.get("model") or model,
            input_tokens=int(usage.get("prompt_tokens") or estimate_tokens(system + user)),
            output_tokens=int(usage.get("completion_tokens") or estimate_tokens(text)),
            latency_ms=latency_ms,
            stop_reason=choice.get("finish_reason"),
        )
