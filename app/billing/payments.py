"""Mock payment processor for plan checkout (D27).

Checks the card's format the way a real processor's client library would (Luhn, expiry, CVC), then
always succeeds. The full card number and CVC never leave this module: callers get the brand and the
last four digits only. Swapping in a real processor means replacing `charge` and nothing else.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from app.errors import ApiError

PROVIDER = "mock"
_BRANDS = (("4", "visa"), ("5", "mastercard"), ("2", "mastercard"), ("34", "amex"), ("37", "amex"))


@dataclass(frozen=True)
class Card:
    name: str
    number: str
    exp_month: int
    exp_year: int
    cvc: str


@dataclass(frozen=True)
class Charge:
    provider: str
    ref: str
    status: str
    brand: str
    last4: str


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def brand_of(digits: str) -> str:
    return next((b for prefix, b in _BRANDS if digits.startswith(prefix)), "card")


def validate(card: Card, now: datetime | None = None) -> str:
    """Returns the card's digits, or raises 422 card_invalid naming the field."""
    digits = re.sub(r"[\s-]", "", card.number)
    if not card.name.strip():
        raise ApiError(422, "card_invalid", "enter the name on the card")
    if not (digits.isdigit() and 12 <= len(digits) <= 19 and _luhn_ok(digits)):
        raise ApiError(422, "card_invalid", "card number is not valid")
    now = now or datetime.now(UTC)
    year = card.exp_year + 2000 if card.exp_year < 100 else card.exp_year
    if not 1 <= card.exp_month <= 12 or (year, card.exp_month) < (now.year, now.month):
        raise ApiError(422, "card_invalid", "card has expired")
    if not re.fullmatch(r"\d{3,4}", card.cvc):
        raise ApiError(422, "card_invalid", "security code must be 3 or 4 digits")
    return digits


def charge(card: Card, amount_microusd: int) -> Charge:
    """Mock charge: validates the card, then always succeeds."""
    digits = validate(card)
    return Charge(
        provider=PROVIDER,
        ref="ch_mock_" + secrets.token_hex(12),
        status="succeeded",
        brand=brand_of(digits),
        last4=digits[-4:],
    )
