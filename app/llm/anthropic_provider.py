"""Anthropic provider via the official SDK. Default model is Claude Haiku 4.5: the cheapest current model, which fits the spend budget."""

from __future__ import annotations

from time import perf_counter

import anthropic

from app.llm.base import LLMResult, ProviderError


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, timeout_s: float = 30.0) -> None:
        # api_key=None lets the SDK resolve ANTHROPIC_API_KEY / an `ant auth login` profile.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=2)

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> LLMResult:
        t0 = perf_counter()
        try:
            msg = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.RateLimitError as exc:
            raise ProviderError(f"provider rate limited: {exc}", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                f"provider error {exc.status_code}", retryable=exc.status_code >= 500
            ) from exc
        except anthropic.APIConnectionError as exc:  # includes APITimeoutError
            raise ProviderError(f"provider unreachable: {exc}", retryable=True) from exc

        latency_ms = int((perf_counter() - t0) * 1000)
        if msg.stop_reason == "refusal":
            raise ProviderError("model refused the request", retryable=False, code="model_refusal")
        text = "".join(block.text for block in msg.content if block.type == "text")
        return LLMResult(
            text=text,
            model=msg.model,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            latency_ms=latency_ms,
            stop_reason=msg.stop_reason,
        )
