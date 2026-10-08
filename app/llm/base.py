"""The provider protocol every model backend implements, and the two types it trades in.

The pipeline calls `complete()` and reads an `LLMResult`; it never imports a vendor module. `LLMResult.model`
is the configured name that config/prices.yaml prices, and `output_tokens` is what the provider bills,
hidden reasoning included, so the ledger can be computed from the result alone. A failure is a
`ProviderError`; `retryable` decides whether the fallback chain (app/llm/fallback.py) may try another model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResult:
    text: str
    # The model name the caller passed in, i.e. the configured name that config/prices.yaml prices.
    # Not the string the API echoes back, which can be a versioned alias with no price row.
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    stop_reason: str | None = None
    # Hidden reasoning ("thinking") tokens, when the provider reports them. Already counted in
    # output_tokens, which is always what the provider bills for; this is for observability.
    reasoning_tokens: int = 0
    # Name of the provider that answered (e.g. "gemini"); set by every real provider.
    provider: str | None = None
    # Set by FallbackProvider when the secondary answered: the primary model that failed.
    fallback_from: str | None = None


class ProviderError(Exception):
    """A provider call failed. `retryable` covers 429, 5xx, timeouts and network errors.

    `retry_after_s` is the server's own hint (the `Retry-After` header on a 429 or 5xx), when it sent one:
    callers that retry should wait that long rather than guess, because the server knows when its
    window resets. None means no hint.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: str = "upstream_error",
        retry_after_s: float | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.retry_after_s = retry_after_s


class LLMProvider(Protocol):
    name: str

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> LLMResult: ...
