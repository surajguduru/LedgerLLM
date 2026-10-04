"""Per-key rate limiting by plan tier (requests per minute).

OWNER: Suraj. The base ships a pass-through stub so the pipeline runs end to end.
Contract (do not change signatures without a PR comment to Suraj):
    check_rate_limit(db, key_id, plan) -> RateLimitResult
Expected implementation: fixed 60s window, one atomic upsert on rate_limit_windows
(INSERT ... ON CONFLICT DO UPDATE SET count = count + 1 RETURNING count) — see docs/DESIGN.md D3.
Also: prune old windows (cron or on-write), and tests in tests/test_ratelimit.py (currently xfail).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.plans import Plan

WINDOW_SECONDS = 60


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


def check_rate_limit(db: Session, key_id: str, plan: Plan) -> RateLimitResult:
    now = int(time.time())
    window_start = now - (now % WINDOW_SECONDS)
    # STUB: always allow. Suraj replaces this with the atomic counter.
    return RateLimitResult(
        allowed=True,
        limit=plan.rpm,
        remaining=plan.rpm,
        reset_epoch=window_start + WINDOW_SECONDS,
    )
