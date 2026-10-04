"""LLM providers behind one protocol (app/llm/base.py). OWNER: Sai.

LLM_PROVIDER selects: mock (default; free, offline) · gemini (free tier) · groq · openai · openrouter · ollama ·
openai_compat (custom LLM_BASE_URL) · anthropic (official SDK). LLM_API_KEY is the key for whichever is selected.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.llm.base import LLMProvider, LLMResult, ProviderError
from app.llm.mock import MockProvider, estimate_tokens
from app.llm.openai_compat import PRESETS, OpenAICompatibleProvider

__all__ = ["LLMProvider", "LLMResult", "ProviderError", "estimate_tokens", "get_provider"]


@lru_cache
def get_provider() -> LLMProvider:
    s = get_settings()
    name = s.llm_provider
    if name == "mock":
        return MockProvider()
    if name == "anthropic":
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=s.llm_api_key or s.anthropic_api_key, timeout_s=s.llm_timeout_s
        )
    base_url = s.llm_base_url or PRESETS.get(name)
    if name == "openai_compat" and not s.llm_base_url:
        raise ValueError("LLM_PROVIDER=openai_compat requires LLM_BASE_URL")
    if base_url is None:
        raise ValueError(
            f"unknown LLM_PROVIDER '{name}'; expected one of mock, {', '.join(PRESETS)}, openai_compat, anthropic"
        )
    if name != "ollama" and not s.llm_api_key:
        raise ValueError(f"LLM_PROVIDER={name} requires LLM_API_KEY")
    return OpenAICompatibleProvider(
        name=name, base_url=base_url, api_key=s.llm_api_key, timeout_s=s.llm_timeout_s
    )
