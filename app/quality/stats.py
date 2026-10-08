"""Read side of online quality sampling: recent samples and means per prompt version.

Consumed by `GET /admin/quality` (app/api/admin.py) and the dashboard. Judge cost comes from the
`purpose="judge"` ledger rows so the number is the same one the billing views see.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import QualitySample, UsageLedger
from app.plans import microusd_to_usd

DRIFT_RECENT_HOURS = 24
DRIFT_MIN_SAMPLES = 10
DRIFT_DROP = 0.5  # points on the 1-5 scale


def drift_report(db: Session, *, days: int = 7) -> list[dict]:
    """Per prompt version: mean score in the last 24 h against the mean of the days before it.

    `drifted` is set when the recent window has at least DRIFT_MIN_SAMPLES scored samples and either
    mean dropped by DRIFT_DROP or more. With a 5 % sample rate, 10 samples is ~200 requests, enough to
    notice a prompt or model change without paging on a single bad summary.
    """
    now = datetime.now(UTC)
    recent_from = now - timedelta(hours=DRIFT_RECENT_HOURS)
    baseline_from = now - timedelta(days=days)

    def means(
        lo: datetime, hi: datetime | None
    ) -> dict[str, tuple[int, float | None, float | None]]:
        q = (
            select(
                QualitySample.prompt_version,
                func.count(),
                func.avg(QualitySample.faithfulness),
                func.avg(QualitySample.coverage),
            )
            .where(QualitySample.judged_at >= lo)
            .where(QualitySample.faithfulness.is_not(None))
            .group_by(QualitySample.prompt_version)
        )
        if hi is not None:
            q = q.where(QualitySample.judged_at < hi)
        return {
            pv: (int(n), float(f) if f is not None else None, float(c) if c is not None else None)
            for pv, n, f, c in db.execute(q).all()
        }

    recent = means(recent_from, None)
    baseline = means(baseline_from, recent_from)
    out = []
    for pv in sorted(set(recent) | set(baseline)):
        rn, rf, rc = recent.get(pv, (0, None, None))
        bn, bf, bc = baseline.get(pv, (0, None, None))
        d_f = round(rf - bf, 2) if rf is not None and bf is not None else None
        d_c = round(rc - bc, 2) if rc is not None and bc is not None else None
        drifted = rn >= DRIFT_MIN_SAMPLES and (
            (d_f is not None and d_f <= -DRIFT_DROP) or (d_c is not None and d_c <= -DRIFT_DROP)
        )
        out.append(
            {
                "prompt_version": pv,
                "recent": {"samples": rn, "faithfulness_mean": _r(rf), "coverage_mean": _r(rc)},
                "baseline": {"samples": bn, "faithfulness_mean": _r(bf), "coverage_mean": _r(bc)},
                "delta": {"faithfulness": d_f, "coverage": d_c},
                "drifted": drifted,
            }
        )
    return out


def _r(x: float | None) -> float | None:
    return round(x, 2) if x is not None else None


def quality_report(db: Session, *, days: int = 7, recent: int = 20) -> dict:
    since = datetime.now(UTC) - timedelta(days=days)

    # avg() skips NULLs, so unparsable judge replies do not drag the means down.
    per_version = db.execute(
        select(
            QualitySample.prompt_version,
            func.count(),
            func.avg(QualitySample.faithfulness),
            func.avg(QualitySample.coverage),
            func.min(QualitySample.judged_at),
            func.max(QualitySample.judged_at),
        )
        .where(QualitySample.judged_at >= since)
        .group_by(QualitySample.prompt_version)
        .order_by(QualitySample.prompt_version)
    ).all()
    unscored = dict(
        db.execute(
            select(QualitySample.prompt_version, func.count())
            .where(QualitySample.judged_at >= since)
            .where(QualitySample.faithfulness.is_(None))
            .group_by(QualitySample.prompt_version)
        ).all()
    )
    judge_cost = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(UsageLedger.cost_microusd), 0),
            func.coalesce(func.sum(UsageLedger.input_tokens + UsageLedger.output_tokens), 0),
        )
        .where(UsageLedger.purpose == "judge")
        .where(UsageLedger.created_at >= since)
    ).one()
    samples = db.scalars(
        select(QualitySample).order_by(QualitySample.judged_at.desc()).limit(recent)
    ).all()

    calls = int(judge_cost[0])
    return {
        "window_days": days,
        "since": since.isoformat(timespec="seconds"),
        "by_prompt_version": [
            {
                "prompt_version": pv,
                "samples": int(n),
                "scored": int(n) - int(unscored.get(pv, 0)),
                "faithfulness_mean": round(float(f), 2) if f is not None else None,
                "coverage_mean": round(float(c), 2) if c is not None else None,
                "first": first.isoformat(timespec="seconds") if first else None,
                "last": last.isoformat(timespec="seconds") if last else None,
            }
            for pv, n, f, c, first, last in per_version
        ],
        "judge": {
            "calls": calls,
            "tokens": int(judge_cost[2]),
            "cost_usd": microusd_to_usd(int(judge_cost[1])),
            "cost_usd_per_1k_samples": round(microusd_to_usd(int(judge_cost[1])) * 1000 / calls, 4)
            if calls
            else 0.0,
        },
        "drift": drift_report(db, days=days),
        "recent": [
            {
                "request_id": s.request_id,
                "tenant_id": s.tenant_id,
                "prompt_version": s.prompt_version,
                "judge_model": s.judge_model,
                "faithfulness": s.faithfulness,
                "coverage": s.coverage,
                "issues": s.issues or [],
                "judge_latency_ms": s.judge_latency_ms,
                "judged_at": s.judged_at.isoformat(timespec="seconds") if s.judged_at else None,
            }
            for s in samples
        ],
    }
