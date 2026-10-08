"""Long-document handling (app/feature/longdoc.py, D20). OWNER: Sai."""

from __future__ import annotations

import random
import re

import pytest

from app.feature.longdoc import omission_marker, truncate_head_tail

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
