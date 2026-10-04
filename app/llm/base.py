from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    stop_reason: str | None = None


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, code: str = "upstream_error"):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class LLMProvider(Protocol):
    name: str

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> LLMResult: ...
