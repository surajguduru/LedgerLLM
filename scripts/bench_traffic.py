"""Micro-benchmarks for the traffic stage, run against whatever DATABASE_URL points at.

    python -m scripts.bench_traffic                       # SQLite file DB
    DATABASE_URL=postgresql+psycopg://ledger:ledger@localhost:5432/ledger python -m scripts.bench_traffic

Prints the cost of one rate-limit check (the atomic upsert + commit) and how many requests a fixed
window admits when a client bursts on both sides of a window boundary (decision D3's accepted cost).
"""

from __future__ import annotations

import statistics
import time
from unittest import mock
from uuid import uuid4

from app.db import SessionLocal, init_db
from app.plans import get_plan
from app.traffic import ratelimit

CALLS = 1000


def bench_check(plan_name: str = "enterprise") -> None:
    plan = get_plan(plan_name)
    key_id = str(uuid4())
    samples: list[float] = []
    with SessionLocal() as db:
        for _ in range(CALLS):
            t0 = time.perf_counter()
            ratelimit.check_rate_limit(db, key_id, plan)
            samples.append((time.perf_counter() - t0) * 1e6)
    samples.sort()
    print(
        f"rate-limit check, {CALLS} calls: p50 {statistics.median(samples):.0f} µs, "
        f"p99 {samples[int(CALLS * 0.99) - 1]:.0f} µs, mean {statistics.mean(samples):.0f} µs"
    )


def bench_window_edge(plan_name: str = "free") -> None:
    plan = get_plan(plan_name)
    key_id = str(uuid4())
    boundary = (int(time.time()) // 60 + 1) * 60
    admitted = 0
    with SessionLocal() as db:
        for now in (boundary - 1, boundary):
            with mock.patch.object(ratelimit.time, "time", lambda now=now: float(now)):
                admitted += sum(
                    ratelimit.check_rate_limit(db, key_id, plan).allowed
                    for _ in range(plan.rpm * 2)
                )
    print(
        f"window-edge burst, plan '{plan.name}' (rpm {plan.rpm}): {admitted} admitted within 2 s "
        f"({admitted / plan.rpm:.1f}x rpm)"
    )


if __name__ == "__main__":
    init_db()
    print(f"database: {SessionLocal.kw['bind'].dialect.name}")
    bench_check()
    bench_window_edge()
