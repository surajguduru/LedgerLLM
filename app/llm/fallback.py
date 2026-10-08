"""Provider fallback chain: one retry on a secondary model when the primary fails transiently.

Free-tier quotas are per model, and 429s and 503s ("high demand") come in bursts, so a second
model is often available while the first is not. Falling back turns an outage into a slower answer.
The rules (docs/DESIGN.md, D19):

- only a retryable `ProviderError` (429, 5xx, timeout, network error) triggers the fallback. A 400
  or an exhausted output budget would fail the same way on any model, so it is re-raised as is;
- the secondary is called exactly once. If it fails too, its error is raised with the primary's
  error as `__cause__`, so the pipeline returns 502 and bills nothing;
- a request for the secondary model itself never falls back to itself;
- the result names the model that answered (`model`, the configured name, which is the key into
  config/prices.yaml) and the one that failed (`fallback_from`). The pipeline bills the answering
  model and reserves the most expensive model of the chain up front, so no request can settle
  above its reservation.

`ledgerllm_provider_fallbacks_total{from_model,to_model}` lives here, in the default Prometheus
registry, so `/metrics` exposes it without a change to app/observability.
"""

from __future__ import annotations

from typing import Any

import structlog
from prometheus_client import Counter

from app.llm.base import LLMProvider, LLMResult, ProviderError

log = structlog.get_logger()

FALLBACKS = Counter(
    "ledgerllm_provider_fallbacks_total",
    "Requests answered by the fallback model after a retryable primary failure",
    ["from_model", "to_model"],
)


class FallbackProvider:
    """Wraps a primary provider; on a retryable error calls `secondary` once with `secondary_model`."""

    def __init__(
        self, primary: LLMProvider, secondary: LLMProvider, *, secondary_model: str
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.secondary_model = secondary_model
        self.name = primary.name
        # JSON mode is passed through only when both ends of the chain accept it.
        self.supports_response_format = bool(
            getattr(primary, "supports_response_format", False)
            and getattr(secondary, "supports_response_format", False)
        )

    def complete(
        self, *, model: str, system: str, user: str, max_tokens: int, **kwargs: Any
    ) -> LLMResult:
        try:
            return self.primary.complete(
                model=model, system=system, user=user, max_tokens=max_tokens, **kwargs
            )
        except ProviderError as exc:
            if not exc.retryable or model == self.secondary_model:
                raise
            primary_error = exc
        FALLBACKS.labels(model, self.secondary_model).inc()
        log.warning(
            "llm_fallback",
            from_model=model,
            to_model=self.secondary_model,
            from_provider=self.primary.name,
            to_provider=self.secondary.name,
            reason=str(primary_error),
        )
        try:
            result = self.secondary.complete(
                model=self.secondary_model,
                system=system,
                user=user,
                max_tokens=max_tokens,
                **kwargs,
            )
        except ProviderError as exc:
            raise exc from primary_error
        result.model = self.secondary_model
        result.provider = result.provider or self.secondary.name
        result.fallback_from = model
        return result


def primary_only(provider: LLMProvider) -> LLMProvider:
    """The provider without its fallback, for requests that may not use the secondary model."""
    return provider.primary if isinstance(provider, FallbackProvider) else provider
