"""Long documents through POST /v1/summarize (D20): strategy, reservation, ledger, failure.

Mock provider throughout; plan limits come from config/plans.yaml (free 20k chars, no
map-reduce; pro 60k chars per call, map-reduce up to 240k; enterprise 120k / 600k).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.billing.pricing import compute_cost_microusd, estimate_cost_microusd
from app.db import SessionLocal
from app.llm.base import ProviderError
from app.llm.fallback import FallbackProvider
from app.llm.mock import MockProvider
from app.models import BudgetPeriod, UsageLedger
from tests.conftest import make_tenant, summarize
from tests.test_longdoc import _paragraphs, _Recording

PRO_LONG = _paragraphs(450)  # ~143k chars: 3 chunks of <= 60k on the pro plan
ABOVE_CEILING = _paragraphs(1_000)  # ~321k chars: above pro's 240k map-reduce ceiling


def _call_order(row: UsageLedger) -> tuple[bool, int]:
    """map:1/n .. map:n/n, then reduce (ids are random and timestamps can tie)."""
    stage = row.prompt_version.rpartition("#")[2]
    if not stage.startswith("map:"):
        return (True, 0)
    return (False, int(stage[4:].split("/")[0]))


def _rows() -> list[UsageLedger]:
    with SessionLocal() as db:
        return sorted(db.scalars(select(UsageLedger)), key=_call_order)


def _budget() -> tuple[int, int]:
    with SessionLocal() as db:
        bp = db.scalars(select(BudgetPeriod)).one()
        return bp.reserved_microusd, bp.spent_microusd


@pytest.fixture
def provider(monkeypatch):
    p = _Recording()
    monkeypatch.setattr("app.api.summarize.get_provider", lambda: p)
    return p


@pytest.fixture
def reserved(monkeypatch):
    """Every amount the pipeline reserves."""
    import app.api.summarize as pipeline

    amounts: list[int] = []
    real = pipeline.budget.reserve

    def spy(db, tenant, plan, est):
        amounts.append(est)
        return real(db, tenant, plan, est)

    monkeypatch.setattr(pipeline.budget, "reserve", spy)
    return amounts


def test_free_plan_keeps_head_and_tail_in_one_call(client, provider):
    key = make_tenant(client, plan="free")["api_key"]
    r = summarize(client, key, text=PRO_LONG)
    assert r.status_code == 200, r.text
    source = r.json()["source"]
    assert (source["strategy"], source["truncated"], source["chars"]) == ("head_tail", True, 20_000)
    assert len(provider.prompts) == 1 and "characters omitted" in provider.prompts[0]
    rows = _rows()
    assert len(rows) == 1 and rows[0].prompt_version == r.json()["usage"]["prompt_version"]


def test_pro_plan_map_reduces_and_books_one_row_per_call(client, api_key, provider, reserved):
    r = summarize(client, api_key, text=PRO_LONG, style="paragraph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == {
        "url": None,
        "title": "",
        "chars": len(PRO_LONG),
        "truncated": False,
        "strategy": "map_reduce",
    }
    rows = _rows()
    n = len(rows) - 1
    assert n >= 2 and len(provider.prompts) == n + 1
    base = body["usage"]["prompt_version"]
    assert [row.prompt_version for row in rows] == [
        f"{base}#map:{i}/{n}" for i in range(1, n + 1)
    ] + [f"{base}#reduce"]
    assert {row.purpose for row in rows} == {"completion"}
    assert {row.request_id for row in rows} == {body["request_id"]}
    total = sum(row.cost_microusd for row in rows)
    for row in rows:
        assert row.cost_microusd == compute_cost_microusd(
            row.model, row.input_tokens, row.output_tokens
        )
    usage = body["usage"]
    assert usage["cost_usd"] == total / 1_000_000
    assert usage["input_tokens"] == sum(row.input_tokens for row in rows)
    assert usage["output_tokens"] == sum(row.output_tokens for row in rows)
    assert (usage["model"], usage["fallback_from"]) == ("gemini-3.8-flash", None)
    assert body["budget"]["spent_usd"] == usage["cost_usd"]
    assert _budget() == (0, total)  # the reservation is released in full at settle
    assert len(reserved) == 1 and total <= reserved[0]


def test_map_reduce_records_one_booking_per_call(client, api_key, provider, monkeypatch):
    from app.observability import metrics

    booked: list[tuple[str, str, int]] = []
    real = metrics.record_booking

    def spy(**kw):
        booked.append((kw["model"], kw["purpose"], kw["cost_microusd"]))
        real(**kw)

    monkeypatch.setattr(metrics, "record_booking", spy)
    assert summarize(client, api_key, text=PRO_LONG).status_code == 200
    assert booked == [(row.model, "completion", row.cost_microusd) for row in _rows()]


def test_failure_in_the_second_map_call_bills_nothing(client, api_key, provider, capsys):
    paras = PRO_LONG.split("\n\n")
    paras[len(paras) // 2] += " [[MOCK_FAIL]]"  # the middle of a 3-chunk text: chunk 2 only
    r = summarize(client, api_key, text="\n\n".join(paras))
    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "upstream_error"
    assert "map:2/3" in r.json()["error"]["message"]
    assert len(provider.prompts) == 2  # map 1 answered, map 2 failed, nothing after it
    assert _rows() == []
    assert _budget() == (0, 0)
    # the tokens map 1 consumed are absorbed by the platform, and logged as such
    absorbed = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if "map_reduce_failed_absorbed" in line
    ]
    assert len(absorbed) == 1
    assert absorbed[0]["stage"] == "map:2/3" and absorbed[0]["calls"] == 1
    assert absorbed[0]["tokens_in"] > 0 and absorbed[0]["cost_microusd"] > 0


def test_text_above_the_ceiling_is_truncated_then_map_reduced(client, api_key, provider):
    r = summarize(client, api_key, text=ABOVE_CEILING)
    assert r.status_code == 200, r.text
    source = r.json()["source"]
    assert source["strategy"] == "map_reduce" and source["truncated"] is True
    assert source["chars"] <= 240_000
    assert (
        sum("characters omitted" in p for p in provider.prompts[:-1]) == 1
    )  # one marker, one chunk
    assert len(_rows()) == len(provider.prompts)


def test_budget_that_covers_one_call_but_not_the_whole_map_reduce_is_402(client, provider):
    one_call = estimate_cost_microusd("gemini-3.8-flash", 60_000 // 4 + 200, 400)
    key = make_tenant(client, plan="pro", budget_override_usd=2 * one_call / 1_000_000)["api_key"]
    r = summarize(client, key, text=PRO_LONG)
    assert r.status_code == 402 and r.json()["error"]["code"] == "budget_exceeded"
    assert provider.prompts == []  # refused before any model call
    assert _rows() == [] and _budget() == (0, 0)
    # the same budget does admit a single call of the largest size the plan sends at once
    r = summarize(client, key, text=PRO_LONG[:59_000])
    assert r.status_code == 200, r.text
    assert r.json()["source"]["strategy"] == "full"


# --- map-reduce with the fallback chain (D19) -------------------------------------------------

PRIMARY, SECONDARY = "gemini-3.8-flash", "gemini-3.5-flash-lite"


class _FailsOn(MockProvider):
    """The mock, failing with a retryable error whenever `marker` is in the user prompt."""

    def __init__(self, marker: str) -> None:
        super().__init__(latency_ms=0)
        self.marker = marker

    def complete(self, **kwargs):
        if self.marker in kwargs["user"]:
            raise ProviderError("503 high demand", retryable=True)
        return super().complete(**kwargs)


def _chain(monkeypatch, marker: str, secondary_model: str = SECONDARY) -> None:
    chain = FallbackProvider(_FailsOn(marker), MockProvider(0), secondary_model=secondary_model)
    monkeypatch.setattr("app.api.summarize.get_provider", lambda: chain)
    monkeypatch.setattr("app.api.summarize.fallback_model", lambda: secondary_model)


def test_one_call_falling_back_is_billed_at_its_own_model(client, api_key, monkeypatch):
    _chain(monkeypatch, "(part 2 of 3)")
    r = summarize(client, api_key, text=PRO_LONG)
    assert r.status_code == 200, r.text
    models = [row.model for row in _rows()]
    assert models == [PRIMARY, SECONDARY, PRIMARY, PRIMARY]
    usage = r.json()["usage"]
    # usage names the requested model unless every call fell back; fallback_from flags any fallback
    assert (usage["model"], usage["fallback_from"]) == (PRIMARY, PRIMARY)
    assert usage["cost_usd"] * 1_000_000 == pytest.approx(sum(r.cost_microusd for r in _rows()))
    assert _budget()[0] == 0


def test_every_call_falling_back_reports_the_fallback_model(client, api_key, monkeypatch):
    _chain(monkeypatch, "<document")  # every prompt
    r = summarize(client, api_key, text=PRO_LONG)
    assert r.status_code == 200, r.text
    assert {row.model for row in _rows()} == {SECONDARY}
    usage = r.json()["usage"]
    assert (usage["model"], usage["fallback_from"]) == (SECONDARY, PRIMARY)


def test_reservation_is_the_sum_of_per_call_chain_maxima(client, monkeypatch, reserved):
    pricey = "claude-sonnet-5-5"  # enterprise only, dearer than gemini-3.8-flash per token
    key = make_tenant(client, plan="enterprise")["api_key"]
    _chain(monkeypatch, "(part 1 of", secondary_model=pricey)
    text = _paragraphs(800)  # ~255k chars: 3 chunks of <= 120k
    r = summarize(client, key, text=text, model=PRIMARY)
    assert r.status_code == 200, r.text

    from app.feature.fetch import FetchedPage
    from app.feature.longdoc import plan_map_reduce
    from app.feature.prompts import load_prompt

    page = FetchedPage(None, None, "", text, "text/plain", 0)
    plan = plan_map_reduce(
        load_prompt(), page, chunk_chars=120_000, style="bullets", max_words=100, instructions=None
    )
    expected = sum(
        max(estimate_cost_microusd(m, c.est_input_tokens, c.max_tokens) for m in (PRIMARY, pricey))
        for c in plan.calls
    )
    assert reserved == [expected]
    assert [row.model for row in _rows()] == [pricey] + [PRIMARY] * (len(plan.calls) - 1)
    assert sum(row.cost_microusd for row in _rows()) <= expected
