"""Long-document handling (app/feature/longdoc.py, D20). OWNER: Sai."""

from __future__ import annotations

import random
import re

import pytest

from app.feature.longdoc import chunk_spans, omission_marker, split_chunks, truncate_head_tail

MARKER = re.compile(r"\n\n\[… (\d+) characters omitted …\]\n\n")


def _prose(n_words: int, seed: int = 7) -> str:
    rng = random.Random(seed)
    words = ["ledger", "budget", "tenant", "a", "summary", "conclusion", "of", "the", "reserve"]
    return " ".join(rng.choice(words) for _ in range(n_words))


# --- truncate_head_tail ---------------------------------------------------------------------


def test_short_text_is_returned_unchanged():
    assert truncate_head_tail("short text", 100) == ("short text", 0)
    assert truncate_head_tail("x" * 100, 100) == ("x" * 100, 0)


@pytest.mark.parametrize("limit", [60, 500, 2_000, 20_000])
def test_result_never_exceeds_the_limit_and_has_one_marker(limit):
    for seed in range(5):
        text = _prose(8_000, seed)
        out, omitted = truncate_head_tail(text, limit)
        assert len(out) <= limit
        assert len(MARKER.findall(out)) == 1
        assert int(MARKER.search(out).group(1)) == omitted
        head, tail = MARKER.split(out)[0], MARKER.split(out)[2]
        assert len(head) + len(tail) + omitted == len(text)
        assert text.startswith(head) and text.endswith(tail)


def test_prose_fills_the_limit_exactly():
    out, _ = truncate_head_tail("word " * 6000, 20_000)
    assert len(out) == 20_000


def test_cuts_land_on_whitespace():
    text = _prose(5_000)
    out, _ = truncate_head_tail(text, 3_000)
    head, _, tail = MARKER.split(out)
    words = set(text.split())
    assert set(head.split()) <= words and set(tail.split()) <= words  # no half-words


def test_head_takes_about_seventy_percent():
    out, _ = truncate_head_tail(_prose(20_000), 10_000)
    head, _, tail = MARKER.split(out)
    assert 0.65 < len(head) / (len(head) + len(tail)) < 0.75


def test_end_of_the_document_survives():
    text = _prose(10_000) + " In conclusion, the ledger balances."
    out, _ = truncate_head_tail(text, 5_000)
    assert out.endswith("In conclusion, the ledger balances.")


def test_text_without_whitespace_is_cut_hard_within_the_limit():
    out, omitted = truncate_head_tail("x" * 50_000, 1_000)
    assert len(out) == 1_000
    assert omitted == 50_000 - (1_000 - len(omission_marker(omitted)))


def test_marker_digits_change_is_accounted_for():
    # n - keep sits right at a power of ten, where the count's width changes
    for n in range(10_040, 10_090):
        out, omitted = truncate_head_tail("ab " * (n // 3) + "a" * (n % 3), 100)
        assert len(out) <= 100


def test_limit_too_small_for_a_marker_is_a_plain_cut():
    assert truncate_head_tail("hello world, how are you", 5) == ("hello", 19)


# --- chunking -------------------------------------------------------------------------------


def _paragraphs(n: int, words: int = 40, seed: int = 3) -> str:
    rng = random.Random(seed)
    return "\n\n".join(_prose(rng.randint(words // 2, words * 2), seed + i) for i in range(n))


def test_text_that_fits_is_one_chunk():
    assert split_chunks("one paragraph", 100) == ["one paragraph"]


def test_chunks_respect_the_size_and_cover_every_character():
    text = _paragraphs(400)
    spans = chunk_spans(text, 5_000)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    for (a, b), (c, _) in zip(spans, spans[1:], strict=False):
        assert b - a <= 5_000
        assert a < c <= b  # each chunk starts inside the previous one: nothing skipped
    assert spans[-1][1] - spans[-1][0] <= 5_000


def test_chunks_end_on_paragraph_boundaries():
    text = _paragraphs(400)
    for _, b in chunk_spans(text, 5_000)[:-1]:
        assert text[b - 2 : b] == "\n\n"


def test_overlap_is_the_last_paragraph_when_it_is_small():
    text = _paragraphs(400, words=10)  # paragraphs well under 2 % of 20,000 chars
    spans = chunk_spans(text, 20_000)
    for (_, b), (c, _) in zip(spans, spans[1:], strict=False):
        assert 0 < b - c <= 400  # 2 % of 20,000
        assert text[c - 2 : c] == "\n\n"  # starts at a paragraph
        assert "\n\n" not in text[c : b - 2]  # exactly one paragraph repeated


def test_long_paragraph_falls_back_to_word_breaks_and_word_overlap():
    text = _prose(10_000)  # one paragraph, ~70k chars
    spans = chunk_spans(text, 10_000)
    for (_, b), (c, _) in zip(spans, spans[1:], strict=False):
        assert text[b - 1] == " "  # cut after a word
        assert 0 < b - c <= 200 and text[c - 1] == " "


def test_chunk_count_matches_the_length():
    text = _paragraphs(800)
    spans = chunk_spans(text, 10_000)
    assert len(spans) == len(split_chunks(text, 10_000))
    # each chunk advances by at least half the window minus the overlap
    assert len(text) / 10_000 <= len(spans) <= len(text) / (5_000 - 200) + 1


def test_text_without_any_break_is_cut_hard():
    spans = chunk_spans("x" * 25_000, 10_000)
    assert spans == [(0, 10_000), (10_000, 20_000), (20_000, 25_000)]
