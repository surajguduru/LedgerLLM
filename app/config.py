from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """All runtime configuration. Values come from env vars, then .env, then these defaults."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./ledgerllm.db"

    # LLM — see app/llm/__init__.py for the provider list. Gemini has a permanent free tier.
    llm_provider: str = (
        "mock"  # mock | gemini | groq | openai | openrouter | ollama | openai_compat | anthropic
    )
    llm_api_key: str | None = (
        None  # key for the selected provider (Gemini: https://aistudio.google.com/apikey)
    )
    llm_base_url: str | None = None  # only for openai_compat, or to override a preset
    default_model: str = "gemini-3.8-flash"
    anthropic_api_key: str | None = None  # legacy alias for the anthropic provider
    llm_timeout_s: float = 30.0
    # reasoning_effort for OpenAI-compatible providers. None: preset default (Gemini "low"); "": don't send.
    llm_reasoning_effort: str | None = None
    # Fallback chain (D19): on a retryable error, call this provider/model once. Empty provider = off.
    llm_fallback_provider: str = ""  # same names as llm_provider, e.g. gemini or groq
    llm_fallback_model: str = ""  # must have a row in prices.yaml, e.g. gemini-3.5-flash-lite
    llm_fallback_api_key: str | None = None  # empty: reuse llm_api_key if the provider is the same
    llm_fallback_base_url: str | None = None
    llm_fallback_timeout_s: float | None = None  # empty: llm_timeout_s

    # Admin
    admin_token: str = "dev-admin-token-change-me"

    # Versioned config artifacts
    plans_path: Path = ROOT / "config" / "plans.yaml"
    prices_path: Path = ROOT / "config" / "prices.yaml"
    prompts_dir: Path = ROOT / "prompts"
    summarize_prompt_version: str = "summarize_v1"

    # Exact-match response cache; per-plan TTL in plans.yaml. False switches it off everywhere.
    response_cache_enabled: bool = True

    # Guardrails: enforce | shadow | off
    guardrails_mode: str = "enforce"
    # Second detection layer: on | off. Off for tests and load tests; the model must be in prices.yaml.
    guardrail_llm: str = "off"
    guardrail_llm_model: str = "gemini-3.5-flash-lite"
    guardrail_llm_cache_size: int = 4096

    # URL fetching
    fetch_timeout_s: float = 10.0
    fetch_max_bytes: int = 2_000_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
