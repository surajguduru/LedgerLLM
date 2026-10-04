"""Output moderation on the model's summary.

OWNER: Thrishal. Base ships a pass-through. Candidates: PII leak check (reuse app/compliance/redaction),
refusal / instruction-following-the-document detection, toxicity word list or classifier, secret patterns.
Policy decision to document: redact-and-return vs withhold. Base pipeline withholds when blocked=True.
"""

from __future__ import annotations

from app.guardrails.types import GuardrailVerdict


def moderate_output(text: str) -> GuardrailVerdict:
    return GuardrailVerdict(blocked=False, category=None, score=0.0, method="noop_v0")
