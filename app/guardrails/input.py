"""Input guardrail: prompt-injection / jailbreak detection on user instructions AND fetched content.

OWNER: Thrishal. Base ships heuristic_v1 (regex). Thrishal owns:
- A stronger second layer (small HF classifier such as protectai/deberta-v3-base-prompt-injection-v2
  via ONNX/CPU, or an LLM classifier on Haiku) with a tuned threshold. Mind free-tier RAM (512MB on Render).
- The red-team eval set (evals/redteam/cases.jsonl, ~50 attacks + ~50 benign) and its CI gate.
- Catch-rate / false-positive numbers for the README, and the added p50/p99 latency overhead.
- tests/test_guardrails.py (two xfail cases to turn green).
Verdicts for `source="document"` are indirect-injection signals; the prompt also treats the document as data.
"""

from __future__ import annotations

import re
from time import perf_counter

from app.guardrails.types import GuardrailVerdict

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "prompt_injection",
        re.compile(
            r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|rules)",
            re.I,
        ),
    ),
    (
        "prompt_injection",
        re.compile(
            r"disregard\s+(the\s+|your\s+|all\s+)?(previous|prior|above|system)\s+(instructions|prompt|rules)",
            re.I,
        ),
    ),
    (
        "prompt_injection",
        re.compile(
            r"(reveal|print|show|repeat|leak)\s+(me\s+)?(your\s+|the\s+)?(system\s+prompt|hidden\s+instructions|initial\s+prompt)",
            re.I,
        ),
    ),
    (
        "prompt_injection",
        re.compile(r"\byou\s+are\s+now\s+(DAN|in\s+developer\s+mode|unrestricted)", re.I),
    ),
    ("jailbreak", re.compile(r"\bjailbreak\b", re.I)),
    (
        "jailbreak",
        re.compile(
            r"(pretend|act)\s+(that\s+)?you\s+(have|are)\s+(no|without)\s+(rules|restrictions|guidelines)",
            re.I,
        ),
    ),
    ("prompt_injection", re.compile(r"\bsystem\s*:\s*you\s+are\b", re.I)),
]


def classify_input(text: str, *, source: str = "instructions") -> GuardrailVerdict:
    t0 = perf_counter()
    for category, pattern in _PATTERNS:
        m = pattern.search(text)
        if m:
            return GuardrailVerdict(
                blocked=True,
                category=category,
                score=0.95,
                method="heuristic_v1",
                latency_ms=int((perf_counter() - t0) * 1000),
                details={"source": source, "match": m.group(0)[:80]},
            )
    return GuardrailVerdict(
        blocked=False,
        category=None,
        score=0.0,
        method="heuristic_v1",
        latency_ms=int((perf_counter() - t0) * 1000),
        details={"source": source},
    )
