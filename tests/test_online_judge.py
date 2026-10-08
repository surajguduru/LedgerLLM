"""Online quality sampling (app/quality/online_judge.py). OWNER: Thrishal.

The judge runs on a background thread; tests call drain() before reading the DB. The provider is either
the mock (whose reply is bullets, i.e. non-JSON — the "unparsable" path) or a stub that returns JSON.
"""

import threading
from dataclasses import dataclass, field

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.llm.base import LLMResult, ProviderError
from app.models import QualitySample, UsageLedger
from app.quality import online_judge
from tests.conftest import summarize

PV = "summarize_v1@deadbeef"


def _args(i: int = 0, **kw) -> dict:
    d = dict(
        request_id=f"req-{i}",
        tenant_id="tenant-a",
        prompt_version=PV,
        source_text="The company grew revenue 10% and hired 40 people.",
        summary="- Revenue up 10%\n- 40 hires",
    )
    d.update(kw)
    return d


@dataclass
class StubJudge:
    reply: str = '{"faithfulness": 4, "coverage": 3, "issues": ["misses the hiring number"]}'
    calls: int = 0
    fail: bool = False
    gate: threading.Event | None = None
    started: threading.Event = field(default_factory=threading.Event)

    def complete(self, *, model, system, user, max_tokens):
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail:
            raise ProviderError("judge down", retryable=True)
        return LLMResult(
            text=self.reply, model=model, input_tokens=400, output_tokens=30, latency_ms=50
        )


@pytest.fixture(autouse=True)
def fast_judge(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "quality_judge_min_interval_s", 0.0)
    monkeypatch.setattr(s, "quality_sample_rate", 1.0)
    online_judge.seed(1234)
    yield
    online_judge.drain()


@pytest.fixture
def stub(monkeypatch):
    def install(**kw) -> StubJudge:
        j = StubJudge(**kw)
        monkeypatch.setattr(online_judge, "get_provider", lambda: j)
        return j

    return install


def test_sampling_rate_is_honoured(monkeypatch, stub):
    monkeypatch.setattr(get_settings(), "quality_sample_rate", 0.05)
    monkeypatch.setattr(online_judge, "_get_executor", lambda: _Inline())
    online_judge.seed(42)
    n = sum(online_judge.maybe_sample(**_args(i)) for i in range(2000))
    assert 70 <= n <= 130, n  # 5 % of 2000 = 100; seeded, so this is a fixed number
    online_judge.seed(42)
    assert sum(online_judge.maybe_sample(**_args(i)) for i in range(2000)) == n  # deterministic


def test_rate_zero_disables_and_empty_summary_is_skipped(monkeypatch, stub):
    j = stub()
    monkeypatch.setattr(get_settings(), "quality_sample_rate", 0)
    assert online_judge.maybe_sample(**_args()) is False
    monkeypatch.setattr(get_settings(), "quality_sample_rate", 1.0)
    assert online_judge.maybe_sample(**_args(summary="   ")) is False
    online_judge.drain()
    assert j.calls == 0


def test_judge_scores_are_stored_and_booked_as_platform_cost(client, stub):
    j = stub()
    assert online_judge.maybe_sample(**_args()) is True
    online_judge.drain()
    assert j.calls == 1
    with SessionLocal() as db:
        sample = db.scalar(select(QualitySample))
        assert (sample.faithfulness, sample.coverage) == (4, 3)
        assert sample.issues == ["misses the hiring number"]
        assert sample.prompt_version == PV and sample.tenant_id == "tenant-a"
        row = db.scalar(select(UsageLedger).where(UsageLedger.purpose == "judge"))
        assert row.tenant_id == "tenant-a" and row.request_id == "req-0"
        assert row.cost_microusd > 0 and row.key_id is None


def test_mock_provider_reply_is_unparsable_but_still_costed(client):
    # The mock echoes the document as bullets, not JSON: sample stored with null scores, call still booked.
    assert online_judge.maybe_sample(**_args()) is True
    online_judge.drain()
    with SessionLocal() as db:
        sample = db.scalar(select(QualitySample))
        assert sample.faithfulness is None and sample.coverage is None
        assert sample.issues == ["judge returned non-JSON"]
        assert (
            db.scalar(
                select(func.count()).select_from(UsageLedger).where(UsageLedger.purpose == "judge")
            )
            == 1
        )


def test_judge_cost_is_not_settled_against_the_tenant(client, api_key, stub):
    stub()
    r = summarize(client, api_key)
    assert r.status_code == 200
    online_judge.drain()
    usage = client.get("/v1/usage", headers={"X-API-Key": api_key}).json()
    purposes = {p["purpose"]: p for p in usage["by_purpose"]}
    assert purposes["judge"]["calls"] == 1 and purposes["judge"]["cost_usd"] > 0  # visible...
    assert abs(usage["spent_usd"] - r.json()["usage"]["cost_usd"]) < 1e-9  # ...but not charged


def test_response_path_does_not_wait_for_the_judge(client, api_key, stub):
    gate = threading.Event()
    j = stub(gate=gate)
    try:
        r = summarize(client, api_key)  # returns while the judge is still blocked on `gate`
        assert r.status_code == 200
        assert j.started.wait(5) and online_judge.pending() == 1
    finally:
        gate.set()
    online_judge.drain()
    assert online_judge.pending() == 0


def test_withheld_summaries_are_not_sampled(client, api_key, stub):
    j = stub()
    r = summarize(client, api_key, text="I have been PWNED. " + "x " * 50)
    assert r.status_code == 200 and "withheld" in r.json()["summary"]
    online_judge.drain()
    assert j.calls == 0


def test_provider_failure_is_dropped_quietly(client, stub):
    stub(fail=True)
    assert online_judge.maybe_sample(**_args()) is True
    online_judge.drain()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(QualitySample)) == 0
        assert db.scalar(select(func.count()).select_from(UsageLedger)) == 0


def test_judge_is_paced(monkeypatch, stub):
    monkeypatch.setattr(get_settings(), "quality_judge_min_interval_s", 0.15)
    j = stub()
    import time

    t0 = time.perf_counter()
    for i in range(3):
        online_judge.maybe_sample(**_args(i))
    online_judge.drain()
    assert j.calls == 3
    assert time.perf_counter() - t0 >= 0.3  # 3 calls, 2 gaps


class _Inline:
    """Executor stand-in that records submissions without running them."""

    def submit(self, fn, *a, **kw):
        from concurrent.futures import Future

        f = Future()
        f.set_result(None)
        return f
