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
