from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


DEFAULT_ADMIN_TOKEN = "dev-admin-token-change-me"
MIN_ADMIN_TOKEN_CHARS = 24


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
    anthropic_api_key: str | None = None  # key for anthropic models when it is not LLM_PROVIDER
    # Keys for the other providers a deployment serves models from (app/llm/router.py, D26). A model whose
    # provider is LLM_PROVIDER uses LLM_API_KEY; a model whose provider has no key here is unavailable.
    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    openai_api_key: str | None = None
    openrouter_api_key: str | None = None
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
    admin_token: str = DEFAULT_ADMIN_TOKEN
    # /metrics lists every tenant's spend. Set: it needs `Authorization: Bearer <token>`. Unset: open in
    # dev/test (docker-compose's Prometheus scrapes it), 404 anywhere else (see app/observability/metrics.py).
    metrics_token: str | None = None

    # The deployed commit, shown by /healthz. Render sets RENDER_GIT_COMMIT; elsewhere set GIT_COMMIT.
    git_commit: str | None = Field(
        None, validation_alias=AliasChoices("RENDER_GIT_COMMIT", "GIT_COMMIT")
    )

    # Tenant portal (/app): session lifetime, and whether the cookie is HTTPS-only. None = secure
    # everywhere except APP_ENV dev/test, where the app is served over plain http://localhost.
    portal_session_days: int = 7
    portal_cookie_secure: bool | None = None

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
    # Which instructions the classifier sees when it is on: "uncertain" (heuristic score in the band)
    # or "always" (every non-empty instruction; ~$0.0001 each, cached by text). Documents are always
    # banded: they are long and the heuristics are rarely unsure about them.
    guardrail_llm_instructions: str = "always"

    # Online quality sampling: share of successful summaries judged in the background (0 disables).
    # The judge runs on the platform's key and is paced for the Gemini free tier (~10 RPM).
    quality_sample_rate: float = 0.05
    quality_judge_model: str | None = None  # defaults to DEFAULT_MODEL
    quality_judge_min_interval_s: float = 6.0

    # Largest request body accepted, in bytes (413 content_too_large above it). The largest plan
    # summarises up to 600k characters (enterprise map_reduce_max_chars); as JSON that is at most
    # 6 bytes per character (\uXXXX escapes), so 4 MB leaves room without letting 40 MB in.
    max_request_bytes: int = 4_000_000

    # URL fetching
    fetch_timeout_s: float = 10.0
    fetch_max_bytes: int = 2_000_000


def check_production_safety(s: Settings) -> None:
    """Refuse to start outside dev/test with an admin token anyone can read in this repository.

    /admin/* creates tenants and mints API keys; the default token is public, so a deployment that
    forgot ADMIN_TOKEN would hand that to anyone. Render's blueprint generates one (render.yaml).
    """
    if s.app_env in ("dev", "test"):
        return
    if s.admin_token == DEFAULT_ADMIN_TOKEN or len(s.admin_token) < MIN_ADMIN_TOKEN_CHARS:
        raise RuntimeError(
            f"APP_ENV={s.app_env}: ADMIN_TOKEN is the public default or shorter than "
            f"{MIN_ADMIN_TOKEN_CHARS} characters; set a random one (e.g. `openssl rand -hex 32`)"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
