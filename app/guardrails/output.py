"""Output moderation on the model's summary.

Three checks, in order of severity, each producing a `GuardrailVerdict`:

1. **instruction_followed** — the summary contains a canary phrase that injection payloads ask for
   ("PWNED", "AI injection succeeded", …) or a URL that does not appear in the source document. A
   summarizer has no reason to invent a link; one that appears came from an injected instruction.
   Policy: **withhold** (the pipeline returns its placeholder instead of the text).
2. **toxicity** — a short list of abusive phrases a summary should never contain. Policy: **withhold**.
3. **pii_leak** — email addresses and phone numbers, detected with the compliance redactor so the
   definition of PII is the same one the request log uses. Policy: **redact and return**: the verdict
   is `blocked=True` with `details={"redacted": True, "text": <redacted summary>}` and the pipeline
   serves the redacted text. The summary is still useful with `[EMAIL]` in it, the tenant already
   paid for the completion, and the raw text never leaves the process (logs are redacted anyway).

Why not redact everything? A canary or a foreign URL means the model obeyed the document instead of the
user — the rest of the summary cannot be trusted either, so nothing is returned. PII in a summary is
usually faithful (the source contained it), so the redacted copy is safe to serve.

In `shadow` mode none of this changes the response; verdicts are recorded as `guardrail.shadow_block`
audit events so the false-positive rate can be read off before enforcing (DESIGN.md, D9 / D22).

The checks are pure string work (no model call), so a verdict never carries a cost.
"""

from __future__ import annotations

import re
from time import perf_counter

from app.compliance.redaction import redact
from app.guardrails.types import GuardrailVerdict

METHOD = "output_rules_v1"

# Phrases that attack payloads ask the model to emit. Matching one proves an instruction was followed.
_CANARIES = re.compile(
    r"\b(pwned|i\s+have\s+been\s+pwned|i\s+have\s+been\s+hacked|ai\s+injection\s+succeeded|"
    r"injection\s+successful|hacked\s+by|this\s+summarizer\s+has\s+been\s+compromised)\b",
    re.I,
)

_URL = re.compile(r"\b(?:https?://|www\.)[^\s)\]>\"']+", re.I)

# Deliberately short. Anything subtler than this belongs to a classifier, not a word list.
_TOXIC = re.compile(
    r"\b(kill\s+yourself|go\s+die|i\s+hope\s+you\s+(die|suffer|get\s+hurt)|"
    # second-person insults: "you are an idiot", "you're so stupid", "you are a waste of space"
    r"you\s*(are|'re)\s+(an?\s+|such\s+an?\s+|so\s+|totally\s+|really\s+)?"
    r"(idiot|moron|imbecile|loser|worthless|pathetic|disgusting|subhuman|stupid|dumb|ugly|useless|"
    r"garbage|trash|a\s+waste\s+of\s+(space|air|oxygen))|"
    r"(everyone|everybody|the\s+whole\s+world)\s+(hates|despises|laughs\s+at)\s+you|"
    r"(nobody|no\s+one)\s+(likes|loves|wants|cares\s+about)\s+you|"
    r"(fuck|screw)\s+(you|off|them|him|her)|piece\s+of\s+shit|shut\s+(the\s+fuck\s+)?up|"
    r"dumb\s+(bitch|cunt|whore)|retard(ed|s)?|faggot|nigg(er|a)s?)\b",
    re.I,
)


def _norm_url(u: str) -> str:
    u = u.lower().rstrip(".,;:!?")
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def foreign_urls(summary: str, source_text: str | None) -> list[str]:
    """URLs in the summary that are absent from the source. Unknown source → nothing to compare."""
    if source_text is None:
        return []
    known = {_norm_url(u) for u in _URL.findall(source_text)}
    out = []
    for u in _URL.findall(summary):
        n = _norm_url(u)
        if n and n not in known and not any(n.startswith(k) or k.startswith(n) for k in known):
            out.append(u[:120])
    return out


def moderate_output(text: str, *, source_text: str | None = None) -> GuardrailVerdict:
    t0 = perf_counter()

    def verdict(blocked: bool, category: str | None, score: float, **details) -> GuardrailVerdict:
        return GuardrailVerdict(
            blocked=blocked,
            category=category,
            score=score,
            method=METHOD,
            latency_ms=int((perf_counter() - t0) * 1000),
            details=details,
        )

    canary_in_source = None
    m = _CANARIES.search(text)
    if m:
        # A canary that the SOURCE also contains is not proof of anything: an honest summary of a
        # security article about "I have been PWNED" must contain the phrase. It is not an
        # exemption either: the checks below still run, and only if none of them fires is the
        # summary served, flagged so the verdict counter and the request log still show it. A
        # canary absent from the source can only have come from an instruction the model obeyed.
        if not (source_text and _CANARIES.search(source_text)):
            return verdict(True, "instruction_followed", 0.99, policy="withhold", canary=m.group(0))
        canary_in_source = m.group(0)
    urls = foreign_urls(text, source_text)
    if urls:
        return verdict(True, "instruction_followed", 0.9, policy="withhold", foreign_urls=urls)

    m = _TOXIC.search(text)
    if m:
        return verdict(True, "toxicity", 0.95, policy="withhold", match=m.group(0))

    r = redact(text)
    if r.total:
        return verdict(
            True, "pii_leak", 0.9, policy="redact", redacted=True, text=r.text, counts=r.counts
        )

    if canary_in_source:
        return verdict(False, "canary_in_source", 0.4, canary=canary_in_source)
    return verdict(False, None, 0.0)
