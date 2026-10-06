"""Per-key rate limiting by plan tier (requests per minute).

OWNER: Suraj.
Contract (do not change signatures without a PR comment to Suraj):
    check_rate_limit(db, key_id, plan) -> RateLimitResult

Fixed 60 s window per key (docs/DESIGN.md D3): one atomic
INSERT ... ON CONFLICT DO UPDATE SET count = count + 1 RETURNING count on rate_limit_windows, so two
concurrent requests can never both read the same count. Rejected requests still increment the counter;
a client hammering past its limit stays limited until the window resets. Windows older than
PRUNE_AFTER_SECONDS are deleted opportunistically on about 1 % of calls, so no cron job is needed.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import RateLimitWindow
from app.plans import Plan

WINDOW_SECONDS = 60
PRUNE_AFTER_SECONDS = 180
PRUNE_PROBABILITY = 0.01


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_epoch: int

    @property
    def retry_after_s(self) -> int:
        return max(1, self.reset_epoch - int(time.time()))

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(self.reset_epoch),
        }


def _increment(db: Session, key_id: str, window_start: int) -> int:
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    stmt = (
        insert(RateLimitWindow)
        .values(key_id=key_id, window_start=window_start, count=1)
        .on_conflict_do_update(
            index_elements=[RateLimitWindow.key_id, RateLimitWindow.window_start],
            set_={"count": RateLimitWindow.count + 1},
        )
        .returning(RateLimitWindow.count)
    )
    return db.execute(stmt).scalar_one()


def prune_windows(db: Session, now: int) -> int:
    """Delete windows that can no longer affect a decision. Returns the number of rows removed."""
    result = db.execute(
        delete(RateLimitWindow).where(RateLimitWindow.window_start < now - PRUNE_AFTER_SECONDS)
    )
    return result.rowcount or 0


def check_rate_limit(db: Session, key_id: str, plan: Plan) -> RateLimitResult:
    now = int(time.time())
    window_start = now - (now % WINDOW_SECONDS)
    count = _increment(db, key_id, window_start)
    if random.random() < PRUNE_PROBABILITY:
        prune_windows(db, now)
    db.commit()
    return RateLimitResult(
        allowed=count <= plan.rpm,
        limit=plan.rpm,
        remaining=max(0, plan.rpm - count),
        reset_epoch=window_start + WINDOW_SECONDS,
    )
