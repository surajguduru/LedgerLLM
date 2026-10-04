"""The actual product feature: turn a page into a summary with one LLM call."""

from __future__ import annotations

from dataclasses import dataclass

from app.feature.fetch import FetchedPage
from app.feature.prompts import PromptSpec
from app.llm.base import LLMProvider


@dataclass
class SummaryResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    prompt_version: str
    prompt_hash: str


def build_user_prompt(
    prompt: PromptSpec, page: FetchedPage, *, style: str, max_words: int, instructions: str | None
) -> str:
    return prompt.render_user(
        title=page.title or "untitled",
        source=page.url or "inline-text",
        text=page.text,
        style=style,
        max_words=max_words,
        instructions=instructions,
    )


def output_token_cap(prompt: PromptSpec, max_words: int) -> int:
    # ~1.5 tokens/word plus headroom, capped by the prompt artifact's ceiling.
    return min(prompt.max_tokens, int(max_words * 2) + 100)


def run_summary(
    provider: LLMProvider,
    *,
    model: str,
    prompt: PromptSpec,
    user_prompt: str,
    max_tokens: int,
) -> SummaryResult:
    res = provider.complete(
        model=model, system=prompt.system, user=user_prompt, max_tokens=max_tokens
    )
    return SummaryResult(
        text=res.text.strip(),
        model=res.model,
        input_tokens=res.input_tokens,
        output_tokens=res.output_tokens,
        latency_ms=res.latency_ms,
        prompt_version=prompt.version,
        prompt_hash=prompt.content_hash,
    )
