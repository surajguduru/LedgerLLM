"""PII redaction applied to everything we persist or log.

OWNER: Loukik. Covers email, phone, credit cards (Luhn-validated), Aadhaar,
PAN, IPv4 and API keys/secrets. Patterns run specific -> generic so the
broad phone regex never eats card/Aadhaar/IP digits. Use redact() for
free text and redact_dict() for audit details (strings redacted in place).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
# Not right after '+' or a digit: +919876543210 is an international phone number.
_AADHAAR = re.compile(r"(?<![+\d])\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b")
_PAN = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
_SECRET = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|llk_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{8,})\b"
)
_IPV4 = re.compile(r"\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Starts at '+', '(' or a digit, so "(415) 555-0134" goes whole, but never inside a token such as
# INV-2026-000123. A '.' only rules a match out when a digit is on its other side (IPs, decimals);
# a full stop ending the sentence does not.
_PHONE_CANDIDATE = re.compile(r"(?<![\w(\-/])(?<!\d\.)(?:\+|\()?\d[\d\s().-]{8,16}\d(?!\w|\.\d)")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _sub_credit_card(text: str) -> tuple[str, int]:
    n = 0

    def _repl(m: re.Match[str]) -> str:
        nonlocal n
        digits = re.sub(r"[ -]", "", m.group(0))
        if not digits.isdigit() or not 13 <= len(digits) <= 19:
            return m.group(0)
        if not _luhn_ok(digits):
            return m.group(0)
        n += 1
        return "[CREDIT_CARD]"

    return _CARD_CANDIDATE.sub(_repl, text), n


def _sub_phone(text: str) -> tuple[str, int]:
    n = 0

    def _repl(m: re.Match[str]) -> str:
        nonlocal n
        raw = m.group(0)
        # A decimal (one '.', nothing else) is a number, not a dotted phone like 415.555.0134.
        if raw.count(".") == 1 and raw.replace(".", "").isdigit():
            return raw
        if _ISO_DATE.fullmatch(raw):
            return raw
        # Never eat card/Aadhaar-like digit runs: pure digit runs of 12-19
        # digits (ignoring spaces/dashes) without a leading '+' belong to
        # the specific patterns above (valid ones already replaced; invalid
        # ones are negatives that must be kept).
        if not raw.lstrip().startswith("+"):
            stripped = re.sub(r"[ \-]", "", raw)
            if stripped.isdigit() and 12 <= len(stripped) <= 19:
                return raw
        n += 1
        return "[PHONE]"

    return _PHONE_CANDIDATE.sub(_repl, text), n


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
    out, n = _sub_credit_card(out)
    if n:
        counts["credit_card"] = n
    out, n = _AADHAAR.subn("[AADHAAR]", out)
    if n:
        counts["aadhaar"] = n
    out, n = _PAN.subn("[PAN]", out)
    if n:
        counts["pan"] = n
    out, n = _SECRET.subn("[SECRET]", out)
    if n:
        counts["secret"] = n
    out, n = _IPV4.subn("[IPV4]", out)
    if n:
        counts["ipv4"] = n
    out, n = _EMAIL.subn("[EMAIL]", out)
    if n:
        counts["email"] = n
    out, n = _sub_phone(out)
    if n:
        counts["phone"] = n
    return RedactionResult(text=out, counts=counts)


def redact_dict(obj: Any) -> Any:
    """Redact strings nested in dicts/lists (used for audit details)."""
    if isinstance(obj, str):
        return redact(obj).text
    if isinstance(obj, dict):
        return {k: redact_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        redacted = [redact_dict(v) for v in obj]
        return type(obj)(redacted) if isinstance(obj, tuple) else redacted
    return obj
