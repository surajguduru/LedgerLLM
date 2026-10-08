"""Route each model to the provider that serves it (D26).

A deployment used to talk to one provider, chosen by LLM_PROVIDER, and sent every model name there: a plan
could list `qwen/qwen3.8-27b` or `claude-haiku-4-5` while the request still went to Gemini and failed. Now
each row of config/prices.yaml may name its `provider`, and `ModelRouter` sends the call to it:

- a model with no `provider` goes to LLM_PROVIDER, exactly as before;
- a model whose provider is LLM_PROVIDER uses LLM_API_KEY / LLM_BASE_URL;
- any other provider needs its own key, `<PROVIDER>_API_KEY` (GEMINI_API_KEY, GROQ_API_KEY, ...). Without
  it the model is *unavailable*: `model_available()` says so and the pipeline refuses the request before
  reserving any budget, instead of failing at the provider.

With LLM_PROVIDER=mock (tests, CI, load tests) every model goes to the mock: nothing leaves the process.
Providers are built on first use and reused.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from app.billing.pricing import load_prices
from app.config import get_settings
from app.llm.base import LLMProvider, LLMResult, ProviderError

# Providers that need no key (a local Ollama server).
_KEYLESS = frozenset({"ollama", "mock"})


def model_provider(model: str) -> str:
    """The provider that serves `model`: its prices.yaml `provider`, else LLM_PROVIDER."""
    s = get_settings()
    if s.llm_provider == "mock":
        return "mock"
    price = load_prices().models.get(model)
    return (price.provider if price is not None else None) or s.llm_provider


def provider_key(name: str) -> str | None:
    """The API key for provider `name`: LLM_API_KEY for the primary, else `<NAME>_API_KEY`."""
    s = get_settings()
    if name == s.llm_provider:
        return s.llm_api_key
    return getattr(s, f"{name}_api_key", None)


def reasoning_allowance(model: str) -> int:
    """Extra output tokens a reasoning model needs before its visible answer (prices.yaml)."""
    price = load_prices().models.get(model)
    return price.reasoning_tokens if price is not None else 0


def model_available(model: str) -> bool:
    """Whether this deployment can serve `model` (its provider is the primary or has a key)."""
    name = model_provider(model)
    return name in _KEYLESS or name == get_settings().llm_provider or bool(provider_key(name))


class ModelRouter:
    """An LLMProvider that forwards each call to the provider serving the requested model."""

    def __init__(
        self,
        default: LLMProvider,
        *,
        default_name: str,
        build: Callable[[str], LLMProvider],
    ) -> None:
        self.default = default
        self.default_name = default_name
        self.name = default.name
        self.supports_response_format = getattr(default, "supports_response_format", False)
        self._build = build
        self._others: dict[str, LLMProvider] = {}
        self._lock = threading.Lock()

    def _for(self, model: str) -> LLMProvider:
        name = model_provider(model)
        if name == self.default_name:
            return self.default
        if not model_available(model):
            raise ProviderError(
                f"model '{model}' is served by {name}, which is not configured "
                f"({name.upper()}_API_KEY is not set)",
                retryable=False,
                code="model_unavailable",
            )
        with self._lock:
            if name not in self._others:
                self._others[name] = self._build(name)
            return self._others[name]

    def complete(
        self, *, model: str, system: str, user: str, max_tokens: int, **kwargs: Any
    ) -> LLMResult:
        target = self._for(model)
        if "response_format" in kwargs and not getattr(target, "supports_response_format", False):
            kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}
        return target.complete(
            model=model, system=system, user=user, max_tokens=max_tokens, **kwargs
        )
