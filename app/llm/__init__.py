"""LLM providers behind one protocol (app/llm/base.py), built from settings once per process.

LLM_PROVIDER selects: mock (default; free, offline) · gemini (free tier) · groq · openai · openrouter · ollama ·
openai_compat (custom LLM_BASE_URL) · anthropic (official SDK). LLM_API_KEY is the key for whichever is selected.
LLM_REASONING_EFFORT overrides the preset's reasoning_effort (unset: preset default; empty: do not send).

LLM_FALLBACK_PROVIDER + LLM_FALLBACK_MODEL turn on the fallback chain (app/llm/fallback.py, D19): the primary
is wrapped in a FallbackProvider that retries a retryable failure once on the fallback model. The fallback
takes the same provider names; LLM_FALLBACK_API_KEY defaults to LLM_API_KEY when both providers are the same,
and LLM_FALLBACK_TIMEOUT_S to LLM_TIMEOUT_S. `fallback_model()` tells the pipeline which model it may have to
reserve and bill for.

`build_provider(reasoning_effort=...)` is the configured primary on its own, with its own reasoning_effort: the
summary eval uses it so its judge can reason at a different level from the summarizer on the same key.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.llm.base import LLMProvider, LLMResult, ProviderError
from app.llm.fallback import FallbackProvider, primary_only
from app.llm.mock import MockProvider, estimate_tokens
from app.llm.openai_compat import PRESETS, OpenAICompatibleProvider
from app.llm.router import (
    ModelRouter,
    model_available,
    model_provider,
    provider_key,
    reasoning_allowance,
)

__all__ = [
    "FallbackProvider",
    "LLMProvider",
    "LLMResult",
    "ProviderError",
    "build_provider",
    "estimate_tokens",
    "fallback_model",
    "get_provider",
    "model_available",
    "model_provider",
    "primary_only",
    "reasoning_allowance",
]


def _build(
    name: str,
    *,
    api_key: str | None,
    base_url: str | None,
    timeout_s: float,
    reasoning_effort: str | None,
    prefix: str = "LLM",
) -> LLMProvider:
    """One provider from its settings. `prefix` names the settings in error messages."""
    s = get_settings()
    if name == "mock":
        return MockProvider()
    if name == "anthropic":
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=api_key or s.anthropic_api_key, timeout_s=timeout_s)
    resolved_url = base_url or PRESETS.get(name)
    if name == "openai_compat" and not base_url:
        raise ValueError(f"{prefix}_PROVIDER=openai_compat requires {prefix}_BASE_URL")
    if resolved_url is None:
        raise ValueError(
            f"unknown {prefix}_PROVIDER '{name}'; expected one of mock, {', '.join(PRESETS)}, openai_compat, anthropic"
        )
    if name != "ollama" and not api_key:
        raise ValueError(f"{prefix}_PROVIDER={name} requires {prefix}_API_KEY")
    kwargs = {}
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    return OpenAICompatibleProvider(
        name=name, base_url=resolved_url, api_key=api_key, timeout_s=timeout_s, **kwargs
    )


def fallback_model() -> str | None:
    """The configured fallback model, or None when the chain is off."""
    s = get_settings()
    if not s.llm_fallback_provider:
        return None
    if not s.llm_fallback_model:
        raise ValueError("LLM_FALLBACK_PROVIDER is set but LLM_FALLBACK_MODEL is empty")
    return s.llm_fallback_model


def build_provider(*, reasoning_effort: str | None = None) -> LLMProvider:
    """A new instance of the configured primary provider, without the fallback chain.

    `reasoning_effort` None keeps LLM_REASONING_EFFORT (or the preset default); "" sends nothing; any other
    value is sent as is. Not cached: every call returns a separate instance.
    """
    s = get_settings()
    return _build(
        s.llm_provider,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        timeout_s=s.llm_timeout_s,
        reasoning_effort=s.llm_reasoning_effort if reasoning_effort is None else reasoning_effort,
    )


def _build_other(name: str) -> LLMProvider:
    """A provider other than LLM_PROVIDER, for a model routed to it (its preset URL and own key)."""
    s = get_settings()
    return _build(
        name,
        api_key=provider_key(name),
        base_url=None,
        timeout_s=s.llm_timeout_s,
        reasoning_effort=None,  # LLM_REASONING_EFFORT is tuned for the primary; others use their preset
        prefix=name.upper(),
    )


@lru_cache
def get_provider() -> LLMProvider:
    s = get_settings()
    primary = _build(
        s.llm_provider,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        timeout_s=s.llm_timeout_s,
        reasoning_effort=s.llm_reasoning_effort,
    )
    if s.llm_provider != "mock":
        # Each model goes to the provider that serves it (D26); LLM_PROVIDER stays the default.
        primary = ModelRouter(primary, default_name=s.llm_provider, build=_build_other)
    model = fallback_model()
    if model is None:
        return primary
    name = s.llm_fallback_provider
    same = name == s.llm_provider
    secondary = _build(
        name,
        api_key=s.llm_fallback_api_key or (s.llm_api_key if same else None),
        base_url=s.llm_fallback_base_url or (s.llm_base_url if same else None),
        timeout_s=s.llm_fallback_timeout_s or s.llm_timeout_s,
        # LLM_REASONING_EFFORT is tuned for the primary's endpoint; another provider uses its preset.
        reasoning_effort=s.llm_reasoning_effort if same else None,
        prefix="LLM_FALLBACK",
    )
    return FallbackProvider(primary, secondary, secondary_model=model)
