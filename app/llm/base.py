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
    def __init__(self, message: str, *, retryable: bool = False, code: str = "upstream_error"):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class LLMProvider(Protocol):
    name: str

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> LLMResult: ...
