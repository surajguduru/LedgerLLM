"""The actual product feature: turn a page into a summary with one LLM call.

A document longer than the plan's input limit is either head+tail-truncated before it gets here
or summarised in several of these calls by app/feature/longdoc.py (map-reduce, D20).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.feature.fetch import FetchedPage
from app.feature.prompts import PromptSpec
from app.llm.base import LLMProvider
from app.llm.fallback import primary_only


@dataclass
class SummaryResult:
    text: str
    model: str  # the configured model that answered: the fallback model if the chain was used
    input_tokens: int
    output_tokens: int
    latency_ms: int
    prompt_version: str
    prompt_hash: str
    provider: str | None = None
    fallback_from: str | None = None


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


def output_token_cap(prompt: PromptSpec, max_words: int, *, reasoning_tokens: int = 0) -> int:
    # ~1.5 tokens/word plus headroom, capped by the prompt artifact's ceiling. A reasoning model also
    # spends hidden tokens first; `reasoning_tokens` (the model's prices.yaml allowance) is added on top
    # so the visible summary still fits. The ceiling bounds the summary, not the thinking.
    return min(prompt.max_tokens, int(max_words * 2) + 100) + max(0, reasoning_tokens)


def run_summary(
    provider: LLMProvider,
    *,
    model: str,
    prompt: PromptSpec,
    user_prompt: str,
    max_tokens: int,
    allow_fallback: bool = True,
) -> SummaryResult:
    """`allow_fallback=False` calls only the primary (the fallback model is not on the tenant's plan)."""
    if not allow_fallback:
        provider = primary_only(provider)
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
        provider=res.provider,
        fallback_from=res.fallback_from,
    )
