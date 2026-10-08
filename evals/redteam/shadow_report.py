"""Shadow-mode report: what the guardrails WOULD have blocked.

    python -m evals.redteam.shadow_report              # last 7 days
    python -m evals.redteam.shadow_report --days 30 --recent 20
    python -m evals.redteam.shadow_report --json

With GUARDRAILS_MODE=shadow the pipeline records every blocking verdict as a `guardrail.shadow_block`
audit event and serves the request anyway (DESIGN.md, D9). This report aggregates those events so the
false-positive rate can be read off real traffic before switching to `enforce`: counts by stage, category,
method and source, the share of all requests, and the most recent matched snippets. Snippets are the
regex matches stored on the verdict, which the guardrail redacted before auditing — the report never
touches request bodies.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.compliance import audit as audit_events
from app.models import AuditEvent, RequestLog


def build_report(db: Session, *, days: int = 7, recent: int = 10) -> dict:
    since = datetime.now(UTC) - timedelta(days=days)
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.event_type == audit_events.GUARDRAIL_SHADOW)
        .where(AuditEvent.created_at >= since)
        .order_by(AuditEvent.created_at.desc())
    ).all()
    total_requests = db.scalar(
        select(func.count())
        .select_from(RequestLog)
        .where(RequestLog.endpoint == "/v1/summarize")
        .where(RequestLog.created_at >= since)
    )

    by_stage, by_category, by_method, by_source, by_tenant = (Counter() for _ in range(5))
    snippets: list[dict] = []
    for e in events:
        d = e.details or {}
        stage = d.get("stage", "?")
        category = d.get("category") or "none"
        method = d.get("method", "?")
        source = (d.get("details") or {}).get("source", "output" if stage == "output" else "?")
        by_stage[stage] += 1
        by_category[category] += 1
        by_method[method] += 1
        by_source[source] += 1
        by_tenant[e.tenant_id or "?"] += 1
        if len(snippets) < recent:
            inner = d.get("details") or {}
            matches = [s.get("match", "") for s in inner.get("signals", [])]
            if stage == "output":
                matches = [
                    str(inner.get(k)) for k in ("canary", "foreign_urls", "match") if inner.get(k)
                ]
            snippets.append(
                {
                    "at": e.created_at.isoformat(timespec="seconds") if e.created_at else None,
                    "tenant_id": e.tenant_id,
                    "request_id": e.request_id,
                    "stage": stage,
                    "category": category,
                    "method": method,
                    "score": d.get("score"),
                    "matches": matches[:5],
                }
            )

    n = len(events)
    return {
        "window_days": days,
        "since": since.isoformat(timespec="seconds"),
        "shadow_blocks": n,
        "summarize_requests": int(total_requests or 0),
        "share_of_requests": round(n / total_requests, 4) if total_requests else 0.0,
        "by_stage": dict(by_stage),
        "by_category": dict(by_category),
        "by_method": dict(by_method),
        "by_source": dict(by_source),
        "top_tenants": by_tenant.most_common(5),
        "recent": snippets,
    }


def _print(r: dict) -> None:
    print(
        f"shadow report: {r['shadow_blocks']} would-be blocks in the last {r['window_days']} days "
        f"({r['share_of_requests']:.2%} of {r['summarize_requests']} summarize requests)"
    )
    for key in ("by_stage", "by_category", "by_method", "by_source"):
        if r[key]:
            print(f"  {key[3:]:<10}: " + "  ".join(f"{k} {v}" for k, v in r[key].items()))
    if r["top_tenants"]:
        print("  tenants   : " + "  ".join(f"{t[:8]} {n}" for t, n in r["top_tenants"]))
    if r["recent"]:
        print(f"  most recent {len(r['recent'])} (snippets are redacted matches):")
        for s in r["recent"]:
            print(
                f"    {s['at']}  {s['stage']:<6} {s['category']:<20} {s['method']:<16} "
                f"score={s['score']}  {s['matches']}"
            )
    if not r["shadow_blocks"]:
        print("  nothing recorded — is GUARDRAILS_MODE=shadow and has traffic flowed?")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--recent", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from app.db import SessionLocal

    with SessionLocal() as db:
        report = build_report(db, days=args.days, recent=args.recent)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
