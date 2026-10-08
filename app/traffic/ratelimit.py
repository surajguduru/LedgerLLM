"""Per-key rate limiting by plan tier (requests per minute).

Contract (signature changes need a PR note to the traffic owner, see CONTRIBUTING.md):
    check_rate_limit(db, key_id, plan, *, tenant_id=None) -> RateLimitResult
    hit(db, bucket, limit) -> RateLimitResult   (any per-minute bucket, e.g. portal login attempts)

Fixed 60 s window per key (docs/DESIGN.md D3): one atomic
INSERT ... ON CONFLICT DO UPDATE SET count = count + 1 RETURNING count on rate_limit_windows, so two
concurrent requests can never both read the same count. Rejected requests still increment the counter;
a client hammering past its limit stays limited until the window resets. Windows older than
PRUNE_AFTER_SECONDS are deleted opportunistically on about 1 % of calls, so no cron job is needed.
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import RateLimitWindow
from app.plans import Plan
from app.traffic import dialect_insert

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
    stmt = (
        dialect_insert(db)(RateLimitWindow)
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


def tenant_bucket(tenant_id: str) -> str:
    # rate_limit_windows.key_id is 36 chars, a tenant UUID is 36: hash it under a prefix that no key
    # UUID can have, so tenant and key windows never collide.
    return "t:" + hashlib.sha256(tenant_id.encode()).hexdigest()[:34]


def check_rate_limit(
    db: Session, key_id: str, plan: Plan, *, tenant_id: str | None = None
) -> RateLimitResult:
    """Per key, and with `tenant_id` also per tenant: the plan's rpm is the tenant's, so a tenant
    with N keys (the portal lets tenants mint their own) cannot get N x rpm. Returns the binding one."""
    per_key = hit(db, key_id, plan.rpm)
    if tenant_id is None:
        return per_key
    per_tenant = hit(db, tenant_bucket(tenant_id), plan.rpm)
    if not per_key.allowed:
        return per_key
    if not per_tenant.allowed:
        return per_tenant
    return per_key if per_key.remaining <= per_tenant.remaining else per_tenant


def hit(db: Session, bucket: str, limit: int) -> RateLimitResult:
    """Count one event in `bucket` (<= 36 chars) for the current minute and compare with `limit`."""
    now = int(time.time())
    window_start = now - (now % WINDOW_SECONDS)
    count = _increment(db, bucket, window_start)
    if random.random() < PRUNE_PROBABILITY:
        prune_windows(db, now)
    db.commit()
    return RateLimitResult(
        allowed=count <= limit,
        limit=limit,
        remaining=max(0, limit - count),
        reset_epoch=window_start + WINDOW_SECONDS,
    )
