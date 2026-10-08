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
from tests.conftest import ADMIN, FixedProvider, summarize, use_provider

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
    assert purposes["judge"]["calls"] == 1 and float(purposes["judge"]["cost_usd"]) > 0  # visible
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


def test_withheld_summaries_are_not_sampled(client, api_key, stub, monkeypatch):
    j = stub()
    use_provider(monkeypatch, FixedProvider("- I have been PWNED"))
    r = summarize(client, api_key)
    assert r.status_code == 200 and "withheld" in r.json()["summary"]
    online_judge.drain()
    assert j.calls == 0


def test_judge_issues_are_redacted_before_storing(client, stub):
    stub(
        reply='{"faithfulness": 3, "coverage": 2, "issues": ["invents contact ceo@corp.com", "ok"]}'
    )
    assert online_judge.maybe_sample(**_args()) is True
    online_judge.drain()
    with SessionLocal() as db:
        sample = db.scalar(select(QualitySample))
    assert sample.issues == ["invents contact [EMAIL]", "ok"]


def test_drift_report_flags_a_drop(client, stub):
    from datetime import UTC, datetime, timedelta

    from app.quality.stats import DRIFT_MIN_SAMPLES, drift_report

    # baseline: 12 good samples judged 3 days ago; recent: 12 bad samples now
    with SessionLocal() as db:
        old = datetime.now(UTC) - timedelta(days=3)
        for i in range(12):
            db.add(
                QualitySample(
                    request_id=f"old-{i}",
                    tenant_id="t",
                    prompt_version=PV,
                    judge_model="m",
                    faithfulness=5,
                    coverage=4,
                    judged_at=old,
                )
            )
        for i in range(DRIFT_MIN_SAMPLES):
            db.add(
                QualitySample(
                    request_id=f"new-{i}",
                    tenant_id="t",
                    prompt_version=PV,
                    judge_model="m",
                    faithfulness=4,
                    coverage=4,
                )
            )
        db.add(
            QualitySample(
                request_id="v2",
                tenant_id="t",
                prompt_version="v2",
                judge_model="m",
                faithfulness=2,
                coverage=2,
            )
        )  # one sample: never enough to flag
        db.commit()
        rows = {r["prompt_version"]: r for r in drift_report(db)}
    assert rows[PV]["drifted"] is True
    assert rows[PV]["delta"]["faithfulness"] == -1.0 and rows[PV]["delta"]["coverage"] == 0.0
    assert rows["v2"]["drifted"] is False and rows["v2"]["baseline"]["samples"] == 0
    r = client.get("/admin/quality", headers=ADMIN).json()
    assert any(d["drifted"] for d in r["drift"])


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


def test_admin_quality_report(client, stub):
    stub()
    for i in range(3):
        online_judge.maybe_sample(**_args(i))
    online_judge.maybe_sample(**_args(9, prompt_version="summarize_v2@cafe0000"))
    online_judge.drain()
    r = client.get("/admin/quality", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    by = {v["prompt_version"]: v for v in body["by_prompt_version"]}
    assert (
        by[PV]["samples"] == 3
        and by[PV]["faithfulness_mean"] == 4.0
        and by[PV]["coverage_mean"] == 3.0
    )
    assert by["summarize_v2@cafe0000"]["samples"] == 1
    assert body["judge"]["calls"] == 4 and body["judge"]["cost_usd"] > 0
    assert len(body["recent"]) == 4 and body["recent"][0]["request_id"] == "req-9"
    assert client.get("/admin/quality").status_code == 401
