"""Summarization eval runner: judge parsing and retry, the judge gate. No network: providers are fakes."""

from __future__ import annotations

import pytest

from app.llm.base import LLMResult
from evals.summarization import run

TH = {
    "min_faithfulness": 4.0,
    "min_coverage": 3.5,
    "max_judge_error_rate": 0.10,
}


class ScriptedProvider:
    """Replies with the given texts in order and records every call."""

    name = "scripted"

    def __init__(self, replies: list[str], *, supports_response_format: bool = False) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.supports_response_format = supports_response_format

    def complete(self, **kwargs) -> LLMResult:
        self.calls.append(kwargs)
        return LLMResult(
            text=self.replies.pop(0),
            model=kwargs["model"],
            input_tokens=10,
            output_tokens=5,
            latency_ms=0,
        )


GOOD = '{"faithfulness": 5, "coverage": 4, "issues": []}'


@pytest.mark.parametrize(
    "text",
    [
        GOOD,
        f"```json\n{GOOD}\n```",
        f"Here is my evaluation:\n{GOOD}\nHope that helps.",
        f"Sure.\n```\n{GOOD}\n```",
    ],
)
def test_parse_accepts_fenced_and_wrapped_json(text):
    assert run.parse_judgement(text) == {"faithfulness": 5, "coverage": 4, "issues": []}


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("no json here", "no JSON object"),
        ('{"faithfulness": 5, "coverage": 4,}', "invalid JSON"),
        ('{"faithfulness": 6, "coverage": 4, "issues": []}', "outside 1-5"),
        ('{"faithfulness": 0, "coverage": 4, "issues": []}', "outside 1-5"),
        ('{"faithfulness": 4.5, "coverage": 4, "issues": []}', "not an integer"),
        ('{"faithfulness": true, "coverage": 4, "issues": []}', "not an integer"),
        ('{"coverage": 4, "issues": []}', "faithfulness missing"),
        ('{"faithfulness": 4, "coverage": 4}', "issues missing"),
        ('{"faithfulness": 4, "coverage": 4, "issues": [1]}', "not a list of strings"),
    ],
)
def test_parse_rejects_invalid_replies(text, reason):
    with pytest.raises(run.JudgeParseError, match=reason):
        run.parse_judgement(text)


def test_judge_retries_once_then_succeeds():
    p = ScriptedProvider(['{"faithfulness": 9, "coverage": 4, "issues": []}', GOOD])
    out = run.judge(p, "source", "summary", "judge-model")
    assert out["faithfulness"] == 5 and out["judge_attempts"] == 2
    assert out["judge_tokens"] == 30
    assert "previous reply was rejected (faithfulness=9 outside 1-5)" in p.calls[1]["user"]
    assert all(c["max_tokens"] >= 1024 for c in p.calls)


def test_judge_records_error_after_second_failure():
    p = ScriptedProvider(["not json", '{"faithfulness": 4}'])
    out = run.judge(p, "source", "summary", "judge-model")
    assert out["judge_error"] == "coverage missing or not an integer"
    assert "faithfulness" not in out and len(p.calls) == 2


def test_judge_asks_for_json_mode_only_when_supported():
    plain = ScriptedProvider([GOOD])
    run.judge(plain, "s", "x", "m")
    assert "response_format" not in plain.calls[0]
    json_mode = ScriptedProvider([GOOD], supports_response_format=True)
    run.judge(json_mode, "s", "x", "m")
    assert json_mode.calls[0]["response_format"] == {"type": "json_object"}


def _rows(scores: list[tuple[int, int] | None]) -> list[dict]:
    return [
        {"judge": {"faithfulness": s[0], "coverage": s[1], "issues": []}}
        if s
        else {"judge": {"judge_error": "no JSON object in reply"}}
        for s in scores
    ]


def test_judge_errors_are_excluded_from_means():
    assert run.judge_gate(_rows([(5, 5)] * 9 + [None]), TH) is True  # 10 % errors, means 5.0


def test_judge_error_rate_above_ten_percent_fails_the_gate():
    assert run.judge_gate(_rows([(5, 5)] * 8 + [None, None]), TH) is False  # 20 %


def test_all_judge_errors_fail_the_gate():
    assert run.judge_gate(_rows([None, None, None]), TH) is False
