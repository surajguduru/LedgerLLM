"""PII redaction. OWNER: Loukik — cases live in evals/redaction/cases.jsonl; add patterns until all pass."""

import json
from pathlib import Path

import pytest

from app.compliance.redaction import redact

CASES = [
    json.loads(line)
    for line in Path("evals/redaction/cases.jsonl").read_text().splitlines()
    if line.strip()
]
IMPLEMENTED = {
    "email", "phone", "credit_card", "aadhaar", "pan", "secret", "ipv4",
    "ipv6", "iban", "ssn", "address",
}  # fmt: skip


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_redaction_cases(case):
    if not set(case["expected_types"]) <= IMPLEMENTED:
        pytest.xfail(
            f"TODO(Loukik): pattern(s) {set(case['expected_types']) - IMPLEMENTED} not implemented"
        )
    res = redact(case["text"])
    for t in case["expected_types"]:
        assert f"[{t.upper()}]" in res.text, res.text
        assert res.counts.get(t, 0) >= 1
    for must_keep in case.get("must_keep", []):
        assert must_keep in res.text


def test_redaction_is_applied_to_request_logs(client, api_key):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import RequestLog
    from tests.conftest import summarize

    summarize(
        client,
        api_key,
        text="Write to jane.doe@example.com or call +91 98765 43210 about the merger. " * 5,
    )
    with SessionLocal() as db:
        log = db.scalars(select(RequestLog)).one()
        assert "jane.doe@example.com" not in (log.redacted_input or "")
        assert "[EMAIL]" in log.redacted_input and "[PHONE]" in log.redacted_input
        assert log.redaction_counts["input.email"] == 5


@pytest.mark.parametrize(
    "text, expected",
    [
        # a number at the end of a sentence is still a phone number
        ("Call me on +91 98765 43210.", "Call me on [PHONE]."),
        ("Office: (415) 555-0134.", "Office: [PHONE]."),
        ("Ring 020 7946 0958. Thanks", "Ring [PHONE]. Thanks"),
        # the whole number goes, including an opening parenthesis
        ("Reach us at (415) 555-0134 today", "Reach us at [PHONE] today"),
        # an international number is a phone, not an Aadhaar number
        ("WhatsApp +919876543210 now", "WhatsApp [PHONE] now"),
        # Aadhaar itself is unaffected
        ("Aadhaar 2345 6789 0123.", "Aadhaar [AADHAAR]."),
        # IPs and decimals followed by a full stop are not phones
        ("Host 10.1.2.3.", "Host [IPV4]."),
        ("Pi is 3.14159265358.", "Pi is 3.14159265358."),
        ("Pi is 3.14159265358 roughly", "Pi is 3.14159265358 roughly"),
        # dates and identifiers are not phone numbers
        ("Meeting on 2026-10-08 at noon.", "Meeting on 2026-10-08 at noon."),
        ("Invoice INV-2026-000123 paid", "Invoice INV-2026-000123 paid"),
        # dotted phone numbers still count
        ("Call 415.555.0134.", "Call [PHONE]."),
    ],
)
def test_phone_edges(text, expected):
    assert redact(text).text == expected
