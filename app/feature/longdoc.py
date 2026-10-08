"""Long documents: what to send the model when a page is longer than the plan allows (D20).

Cutting `text[:limit]` loses the end of every long page, and the end is where reports,
articles and papers put their conclusions. Two strategies replace it:

- head_tail (every plan): keep 70 % of the budget from the start and 30 % from the end, both
  cut on whitespace so no word is split, joined by a marker that tells the model (and anyone
  reading the request log) how much was left out. One model call, as before.
- map_reduce (plans with `map_reduce_max_chars` above `max_input_chars`): split the whole
  text into paragraph-aligned chunks that each fit the input limit, summarise every chunk as
  bullets with the same versioned prompt (map), then summarise those bullets into the
  requested style (reduce). Text beyond the plan's ceiling is head+tail-truncated to it
  first, so the number of calls, and with it the cost, is bounded per plan.

Everything here is pure or takes the provider as an argument; budget, ledger and metrics
stay in the pipeline (app/api/summarize.py), which reserves the sum of `MapReducePlan.calls`
up front and books one ledger row per `StageResult`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from app.feature.fetch import FetchedPage
from app.feature.prompts import PromptSpec
from app.feature.summarize import SummaryResult, build_user_prompt, output_token_cap, run_summary
from app.llm import estimate_tokens
from app.llm.base import LLMProvider, ProviderError
from app.schemas import Style

HEAD_SHARE = 0.7
# How far a cut may move from its target to land on whitespace. Text without whitespace in
# this window (base64, minified code) is cut hard rather than searched any further.
BOUNDARY_WINDOW = 200


def omission_marker(omitted: int) -> str:
    return f"\n\n[… {omitted} characters omitted …]\n\n"


def _is_boundary(text: str, i: int) -> bool:
    """True when cutting between text[i-1] and text[i] splits no word."""
    return i <= 0 or i >= len(text) or text[i - 1].isspace() or text[i].isspace()


def _kept_budget(n: int, limit: int) -> int:
    """Characters of the original text that fit beside the marker. The marker's length
    depends on the count it reports, so grow the budget while the shorter count still fits."""
    keep = limit - len(omission_marker(n))  # omitted < n, so this always fits
    while keep + 1 + len(omission_marker(n - keep - 1)) <= limit:
        keep += 1
    return keep


def _exact_cut(text: str, keep: int, target: int) -> int | None:
    """A head length h near `target` such that both h and the matching tail start
    (n - (keep - h)) fall on whitespace, so the result fills the limit exactly."""
    n = len(text)
    for d in range(BOUNDARY_WINDOW + 1):
        for h in {target - d, target + d}:
            if 0 < h < keep and _is_boundary(text, h) and _is_boundary(text, n - (keep - h)):
                return h
    return None


def truncate_head_tail(text: str, limit: int) -> tuple[str, int]:
    """Shorten `text` to at most `limit` characters, keeping its head and tail.

    Returns the new text and the number of characters omitted (0 when it already fits).
    The result, marker included, is never longer than `limit`; it is exactly `limit` long
    whenever a pair of whitespace cuts allows it, otherwise up to a word shorter per side.
    """
    n = len(text)
    if n <= limit:
        return text, 0
    keep = _kept_budget(n, limit)
    if keep <= 1:  # a limit too small to hold the marker: plain cut
        return text[:limit], n - limit
    target = round(keep * HEAD_SHARE)
    head = _exact_cut(text, keep, target)
    if head is not None:
        tail_start = n - (keep - head)
    else:
        lo = max(1, target - BOUNDARY_WINDOW)
        head = next((h for h in range(target, lo - 1, -1) if _is_boundary(text, h)), target)
        want = n - (keep - head)
        hi = min(n, want + BOUNDARY_WINDOW)
        tail_start = next((t for t in range(want, hi + 1) if _is_boundary(text, t)), want)
    omitted = tail_start - head
    return text[:head] + omission_marker(omitted) + text[tail_start:], omitted


# --- chunking for map-reduce ------------------------------------------------------------------

OVERLAP_SHARE = 0.02  # each chunk repeats about this much of the previous one, for context
_BREAKS = ("\n\n", "\n", ". ", " ")  # preferred cut points, best first


def _break_before(text: str, start: int, limit: int) -> int:
    """End of a chunk that starts at `start` and may run to `limit`: just after the last
    paragraph break in the second half of the window, else a line, sentence or word break,
    else a hard cut at `limit`."""
    floor = start + (limit - start) // 2
    for sep in _BREAKS:
        i = text.rfind(sep, floor, limit)
        if i != -1:
            return i + len(sep)
    return limit


def _overlap_start(text: str, start: int, end: int, overlap: int) -> int:
    """Where the next chunk starts: at the last paragraph of the previous chunk when it fits
    in `overlap` characters, else at the first word break in the last `overlap` characters."""
    if overlap <= 0:
        return end
    lo = max(start + 1, end - overlap)
    para = text.rfind("\n\n", lo, end - 1)  # excludes the break the chunk ends on
    if para != -1:
        return para + 2
    word = text.find(" ", lo, end - 1)
    return word + 1 if word != -1 else end


def chunk_spans(text: str, max_chars: int, overlap: int | None = None) -> list[tuple[int, int]]:
    """(start, end) offsets of chunks of at most `max_chars`, cut on paragraph boundaries
    where possible. Consecutive chunks overlap by up to `overlap` characters (default 2 % of
    `max_chars`, at most a quarter of it) and together cover every character of `text`."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    n = len(text)
    if n <= max_chars:
        return [(0, n)]
    overlap = int(max_chars * OVERLAP_SHARE) if overlap is None else overlap
    overlap = min(overlap, max_chars // 4)
    spans: list[tuple[int, int]] = []
    start = 0
    while n - start > max_chars:
        end = _break_before(text, start, start + max_chars)
        spans.append((start, end))
        start = _overlap_start(text, start, end, overlap)
    spans.append((start, n))
    return spans


def split_chunks(text: str, max_chars: int, overlap: int | None = None) -> list[str]:
    return [text[a:b] for a, b in chunk_spans(text, max_chars, overlap)]


# --- strategy and map-reduce ------------------------------------------------------------------

Strategy = Literal["full", "head_tail", "map_reduce"]


@dataclass(frozen=True)
class Prepared:
    text: str
    strategy: Strategy
    omitted: int  # characters left out; 0 means the summary covers the whole document


def prepare_text(text: str, *, max_input_chars: int, map_reduce_max_chars: int = 0) -> Prepared:
    """Choose how a document is summarised. A ceiling at or below the input limit means the
    plan has no map-reduce; text above the ceiling is head+tail-truncated to it first."""
    if len(text) <= max_input_chars:
        return Prepared(text, "full", 0)
    if map_reduce_max_chars > max_input_chars:
        text, omitted = truncate_head_tail(text, map_reduce_max_chars)
        return Prepared(text, "map_reduce", omitted)
    text, omitted = truncate_head_tail(text, max_input_chars)
    return Prepared(text, "head_tail", omitted)


def map_word_budget(max_words: int, n_chunks: int) -> int:
    """Words per chunk summary: twice the final budget split across the chunks, so the reduce
    step has room to choose, but at least 40 (a few useful bullets) and at most `max_words`."""
    return min(max_words, max(40, 2 * max_words // n_chunks))


@dataclass(frozen=True)
class PlannedCall:
    stage: str  # "map:2/5" or "reduce"; recorded in the ledger's prompt_version
    est_input_tokens: int  # worst case; the reduce input is bounded by the map output caps
    max_tokens: int


def build_reduce_prompt(
    prompt: PromptSpec,
    page: FetchedPage,
    summaries: list[str],
    *,
    style: Style,
    max_words: int,
    instructions: str | None,
) -> str:
    """The chunk summaries become the reduce step's document: inside <document> like any other
    text, so an instruction smuggled into a chunk summary is still data."""
    n = len(summaries)
    text = "\n\n".join(f"Part {i} of {n}:\n{s}" for i, s in enumerate(summaries, 1))
    title = f"{page.title or 'untitled'} (summaries of {n} consecutive parts)"
    return build_user_prompt(
        prompt,
        replace(page, title=title, text=text),
        style=style,
        max_words=max_words,
        instructions=instructions,
    )


@dataclass(frozen=True)
class MapReducePlan:
    page: FetchedPage
    chunks: list[str]
    map_prompts: list[str]
    style: Style
    max_words: int
    instructions: str | None
    calls: list[PlannedCall]  # one per map, then the reduce

    def reduce_prompt(self, prompt: PromptSpec, summaries: list[str]) -> str:
        return build_reduce_prompt(
            prompt,
            self.page,
            summaries,
            style=self.style,
            max_words=self.max_words,
            instructions=self.instructions,
        )


def plan_map_reduce(
    prompt: PromptSpec,
    page: FetchedPage,
    *,
    chunk_chars: int,
    style: Style,
    max_words: int,
    instructions: str | None,
) -> MapReducePlan:
    """Split the page and bound every call before any is made, so the pipeline can reserve the
    worst case of the whole request at once."""
    chunks = split_chunks(page.text, chunk_chars)
    n = len(chunks)
    map_words = map_word_budget(max_words, n)
    map_cap = output_token_cap(prompt, map_words)
    title = page.title or "untitled"
    map_prompts = [
        build_user_prompt(
            prompt,
            replace(page, title=f"{title} (part {i} of {n})", text=chunk),
            style="bullets",
            max_words=map_words,
            instructions=instructions,
        )
        for i, chunk in enumerate(chunks, 1)
    ]
    system = estimate_tokens(prompt.system)
    calls = [
        PlannedCall(f"map:{i}/{n}", system + estimate_tokens(p), map_cap)
        for i, p in enumerate(map_prompts, 1)
    ]
    # The reduce input is its frame plus the map outputs, each at most `map_cap` tokens.
    frame = build_reduce_prompt(
        prompt, page, [""] * n, style=style, max_words=max_words, instructions=instructions
    )
    reduce_in = system + estimate_tokens(frame) + n * map_cap
    calls.append(PlannedCall("reduce", reduce_in, output_token_cap(prompt, max_words)))
    return MapReducePlan(page, chunks, map_prompts, style, max_words, instructions, calls)


@dataclass
class StageResult:
    stage: str | None  # None for a single-call summary
    result: SummaryResult

    @property
    def ledger_suffix(self) -> str:
        return f"#{self.stage}" if self.stage else ""


class MapReduceFailed(ProviderError):
    """A map or reduce call failed. Keeps the calls that had already answered, whose tokens the
    platform absorbs: the request bills nothing (D20)."""

    def __init__(self, stage: str, cause: ProviderError, completed: list[StageResult]) -> None:
        super().__init__(f"{stage}: {cause}", retryable=cause.retryable, code=cause.code)
        self.stage = stage
        self.completed = completed


def run_map_reduce(
    provider: LLMProvider,
    plan: MapReducePlan,
    *,
    model: str,
    prompt: PromptSpec,
    allow_fallback: bool,
) -> list[StageResult]:
    """Summarise each chunk as bullets, then the bullets into the requested style. Calls run one
    after another (free-tier rpm); each may fall back on its own. Returns map results first and
    the reduce result last."""
    done: list[StageResult] = []

    def call(stage: PlannedCall, user_prompt: str) -> SummaryResult:
        try:
            return run_summary(
                provider,
                model=model,
                prompt=prompt,
                user_prompt=user_prompt,
                max_tokens=stage.max_tokens,
                allow_fallback=allow_fallback,
            )
        except ProviderError as exc:
            raise MapReduceFailed(stage.stage, exc, list(done)) from exc

    for stage, user_prompt in zip(plan.calls[:-1], plan.map_prompts, strict=True):
        done.append(StageResult(stage.stage, call(stage, user_prompt)))
    reduce = plan.calls[-1]
    reduce_prompt = plan.reduce_prompt(prompt, [s.result.text for s in done])
    done.append(StageResult(reduce.stage, call(reduce, reduce_prompt)))
    return done


@dataclass(frozen=True)
class Combined:
    """The response's view of one or more calls: tokens and latency summed."""

    text: str
    model: str
    fallback_from: str | None
    provider: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int


def combine(stages: list[StageResult], *, requested_model: str) -> Combined:
    """`model` is the requested model unless every call fell back, then the model that answered;
    `fallback_from` names the requested model when any call fell back. Each ledger row still
    names the model that answered that call. With one call this is the single-call rule (D19)."""
    results = [s.result for s in stages]
    final = results[-1]
    all_fell_back = all(r.fallback_from for r in results)
    return Combined(
        text=final.text,
        model=final.model if all_fell_back else requested_model,
        fallback_from=requested_model if any(r.fallback_from for r in results) else None,
        provider=final.provider,
        input_tokens=sum(r.input_tokens for r in results),
        output_tokens=sum(r.output_tokens for r in results),
        latency_ms=sum(r.latency_ms for r in results),
    )
