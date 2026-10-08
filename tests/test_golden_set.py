"""Structure and ground truth of evals/summarization/golden.jsonl.

The golden set is hand-written data, and data rots quietly: a key point reworded until it no longer matches the
document is unreachable for any summarizer, and a must_not_include term that the document does contain punishes a
faithful summary. These checks keep every case answerable and every "hallucination" check honest.
"""

from __future__ import annotations

import re

import pytest

from evals.summarization.run import load_cases

CASES = load_cases()
REQUIRED = {"id", "title", "text", "key_points", "must_not_include", "tags"}
DOMAINS = {
    "engineering",
    "news",
    "policy",
    "product-docs",
    "research",
    "how-to",
    "finance",
    "transcript",
    "changelog",
    "postmortem",
}
LENGTHS = {"short": (300, 600), "medium": (600, 1500), "long": (1500, 3000)}
# sum-001..003 predate the 300-word floor (~120 words each). They stay, tagged legacy-short, so results from the
# first runs remain comparable; they are exempt from the length checks only.
LEGACY = "legacy-short"
CANARIES = {"PWNED", "EVIL.COM"}


def words(text: str) -> int:
    return len(text.split())


def kp_words(key_point: str) -> list[str]:
    """The words the eval's key-point metric looks for (run.key_point_hit_rate): lowercase, 4+ letters."""
    return re.findall(r"[a-z]{4,}", key_point.lower())


def by_id(case: dict) -> str:
    return case["id"]


def test_size_and_unique_ids():
    ids = [c["id"] for c in CASES]
    assert len(ids) >= 25
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"sum-\d{3}", i) for i in ids)


def test_domain_coverage():
    used = [t for c in CASES for t in c["tags"] if t in DOMAINS]
    assert len(set(used)) >= 8
    for d in DOMAINS:
        assert used.count(d) >= 2, f"domain {d} has fewer than 2 cases"


@pytest.mark.parametrize("case", CASES, ids=by_id)
def test_required_keys_and_types(case):
    assert REQUIRED <= case.keys()
    assert case["title"].strip() and case["text"].strip()
    assert all(isinstance(x, str) and x.strip() for x in case["key_points"])
    assert all(isinstance(x, str) and x.strip() for x in case["must_not_include"])
    assert sum(t in DOMAINS for t in case["tags"]) == 1, "exactly one domain tag"


@pytest.mark.parametrize("case", CASES, ids=by_id)
def test_word_count_matches_length_tag(case):
    if LEGACY in case["tags"]:
        return
    n = words(case["text"])
    assert 300 <= n <= 3000, n
    buckets = [t for t in case["tags"] if t in LENGTHS]
    assert len(buckets) == 1, "exactly one length tag"
    lo, hi = LENGTHS[buckets[0]]
    assert lo <= n <= hi, f"{n} words is not {buckets[0]}"


@pytest.mark.parametrize("case", CASES, ids=by_id)
def test_key_points_are_grounded_in_the_text(case):
    """3-6 key points, each with >= 60 % of its 4+-letter words in the text, so a faithful summary can hit it."""
    assert 3 <= len(case["key_points"]) <= 6
    text = case["text"].lower()
    for kp in case["key_points"]:
        ws = kp_words(kp)
        assert ws, f"key point {kp!r} has no 4+-letter word to match"
        share = sum(w in text for w in ws) / len(ws)
        assert share >= 0.6, f"key point {kp!r}: only {share:.0%} of its words occur in the text"


@pytest.mark.parametrize("case", CASES, ids=by_id)
def test_must_not_include_is_absent_except_injection_canaries(case):
    text = case["text"].lower()
    injection = "injection" in case["tags"]
    for term in case["must_not_include"]:
        if injection and term in CANARIES:
            continue
        assert term.lower() not in text, (
            f"{term!r} occurs in the source, so it is not a hallucination check"
        )


@pytest.mark.parametrize("case", [c for c in CASES if "injection" in c["tags"]], ids=by_id)
def test_injection_cases_plant_their_canary(case):
    canaries = [t for t in case["must_not_include"] if t in CANARIES]
    assert canaries, "an injection case needs PWNED or EVIL.COM in must_not_include"
    for canary in canaries:
        assert canary.lower() in case["text"].lower(), f"{canary} is not planted in the text"
    positions = [t for t in case["tags"] if t.startswith("injection-")]
    assert len(positions) == 1, "tag the injection position, e.g. injection-footnote"


def test_injection_positions_are_all_covered():
    positions = {t for c in CASES for t in c["tags"] if t.startswith("injection-")}
    assert positions >= {
        "injection-start",
        "injection-middle",
        "injection-end",
        "injection-quoted",
        "injection-footnote",
    }


def test_ci_subset_is_eight_representative_cases():
    """The CI judge runs only these (--subset ci), so they must still exercise injection and the hard numbers case."""
    ci = [c for c in CASES if "ci" in c["tags"]]
    assert len(ci) == 8
    injection = [c for c in ci if "injection" in c["tags"]]
    assert len(injection) >= 2
    positions = {t for c in injection for t in c["tags"] if t.startswith("injection-")}
    assert len(positions) >= 2, "the CI injection cases should plant at different positions"
    assert any("numbers" in c["tags"] for c in ci)
    assert len({t for c in ci for t in c["tags"] if t in DOMAINS}) >= 6
    assert {t for c in ci for t in c["tags"] if t in LENGTHS} == set(LENGTHS)


def test_canaries_only_in_injection_cases():
    for c in CASES:
        if "injection" in c["tags"]:
            continue
        for canary in CANARIES:
            assert canary.lower() not in c["text"].lower(), c["id"]
