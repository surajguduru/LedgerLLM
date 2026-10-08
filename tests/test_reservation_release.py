"""A request that crashes between reserve and settle must not keep its reservation (review item 6)."""

import pytest
from sqlalchemy import select

import app.api.summarize as pipeline
from app.db import SessionLocal
from app.models import BudgetPeriod
from tests.conftest import summarize


def _reserved(tenant_key_client) -> int:
    with SessionLocal() as db:
        rows = db.scalars(select(BudgetPeriod)).all()
    return sum(r.reserved_microusd for r in rows)


@pytest.mark.parametrize(
    "stage, attr",
    [
        ("input guardrail", "classify_input"),
        ("output moderation", "moderate_output"),
        ("ledger", "compute_cost_microusd"),
    ],
)
def test_crash_after_reserve_releases_the_reservation(client, api_key, monkeypatch, stage, attr):
    def boom(*a, **kw):
        raise RuntimeError(f"{stage} crashed")

    monkeypatch.setattr(pipeline, attr, boom)
    with pytest.raises(RuntimeError):  # TestClient re-raises what would be a 500
        summarize(client, api_key)
    assert _reserved(client) == 0, f"reservation leaked after a crash in the {stage}"
    # and the tenant can still spend the whole budget afterwards
    monkeypatch.undo()
    assert summarize(client, api_key).status_code == 200


def test_crash_with_idempotency_key_releases_reservation_and_claim(client, api_key, monkeypatch):
    monkeypatch.setattr(pipeline, "moderate_output", lambda *a, **kw: 1 / 0)
    headers = {"Idempotency-Key": "k-crash"}
    with pytest.raises(ZeroDivisionError):
        summarize(client, api_key, _headers=headers)
    assert _reserved(client) == 0
    monkeypatch.undo()
    r = summarize(client, api_key, _headers=headers)  # retry is admitted, not 409
    assert r.status_code == 200, r.text
    assert _reserved(client) == 0


def test_happy_path_leaves_nothing_reserved(client, api_key):
    assert summarize(client, api_key).status_code == 200
    assert _reserved(client) == 0
