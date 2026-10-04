"""PII redaction applied to everything we persist or log.

OWNER: Loukik. Base covers email + phone. Add: credit cards (Luhn-validated), Aadhaar (12 digits),
PAN (ABCDE1234F), IPv4, API keys/secrets (sk-..., llk_...), and (stretch) names via a small NER model.
Each pattern needs cases in evals/redaction/cases.jsonl; tests/test_redaction.py parametrizes over them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,16}\d(?![\w.])"),
}


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def redact(text: str | None) -> RedactionResult:
    if not text:
        return RedactionResult(text=text or "", counts={})
    counts: dict[str, int] = {}
    out = text
    for name, pattern in _PATTERNS.items():
        out, n = pattern.subn(f"[{name.upper()}]", out)
        if n:
            counts[name] = n
    return RedactionResult(text=out, counts=counts)
