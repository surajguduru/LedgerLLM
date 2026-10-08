"""Long documents: what to send the model when a page is longer than the plan allows (D20).

Cutting `text[:limit]` loses the end of every long page, and the end is where reports,
articles and papers put their conclusions. So a document over the limit keeps its head and
its tail: 70 % of the budget from the start, 30 % from the end, both cut on whitespace so no
word is split, joined by a marker that tells the model (and a reader of the logs) how much
was left out.
"""

from __future__ import annotations

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
