"""Deterministic, free, offline provider. Used by tests, CI, local dev and the load test.

Token counts use the same len/4 heuristic as the reservation estimate so ledger numbers are realistic.
Put the literal token [[MOCK_FAIL]] anywhere in the input to simulate an upstream failure.
"""

from __future__ import annotations

import os
import time

from app.llm.base import LLMResult, ProviderError


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class MockProvider:
    name = "mock"

    def __init__(self, latency_ms: int | None = None) -> None:
        self.latency_ms = (
            latency_ms if latency_ms is not None else int(os.getenv("MOCK_LATENCY_MS", "0"))
        )

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> LLMResult:
        if "[[MOCK_FAIL]]" in user:
            raise ProviderError("mock upstream failure", retryable=True)
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        start = user.find("<document")
        body = user[user.find(">", start) + 1 :] if start >= 0 else user
        body = body.replace("</document>", "").strip()
        words = body.split()
        bullets = []
        for i in range(0, min(len(words), 60), 15):
            bullets.append("- " + " ".join(words[i : i + 15]))
        text = "\n".join(bullets) or "- (empty document)"
        return LLMResult(
            text=text,
            model=model,
            input_tokens=estimate_tokens(system) + estimate_tokens(user),
            output_tokens=estimate_tokens(text),
            latency_ms=self.latency_ms,
            stop_reason="end_turn",
            provider=self.name,
        )
