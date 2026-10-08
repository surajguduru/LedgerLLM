"""Idempotency keys: same (tenant, key, body) -> replay the stored response; same key, different body -> 409.

OWNER: Suraj. Records live for TTL (24 h); expired records are ignored on lookup and deleted
opportunistically on about 1 % of claims.

In-flight policy: before the pipeline runs, `claim` inserts a *pending* record (status_code 0). The primary
key on (tenant_id, idempotency_key) makes the claim atomic, so of two identical requests arriving at the same
instant exactly one runs and the other gets 409 `idempotency_in_progress`. On success `store` overwrites the
pending record with the response; on any failure `release` deletes it so the client can retry. Only 200
responses are stored. A pending record left behind by a crashed process stops blocking after PENDING_TTL.

Ownership: a pending record carries the claiming request's id (in `response_json`, which a pending record
does not otherwise use). `release` and `store` only touch the record while it still belongs to that
request, so a request that outlived PENDING_TTL cannot delete or overwrite the claim of the retry that
took the key over after it.
"""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import IdempotencyRecord, utcnow

TTL = timedelta(hours=24)
# Must outlive the slowest request: a map-reduce summary on the enterprise plan makes up to ~20 model
# calls, each up to LLM_TIMEOUT_S (30 s) plus one fallback. Two minutes let a retry take over a key whose
# first request was still running and bill the work twice.
PENDING_TTL = timedelta(minutes=15)
_OWNER_PREFIX = "pending:"

PENDING_STATUS = 0
CLEANUP_PROBABILITY = 0.01


def hash_request(payload: BaseModel) -> str:
    return hashlib.sha256(payload.model_dump_json(exclude_none=True).encode()).hexdigest()


def is_pending(record: IdempotencyRecord) -> bool:
    return record.status_code == PENDING_STATUS


def _aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes even for timezone=True columns; every value we write is UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _expired(record: IdempotencyRecord, now: datetime) -> bool:
    ttl = PENDING_TTL if is_pending(record) else TTL
    return now - _aware(record.created_at) > ttl


def lookup(db: Session, tenant_id: str, key: str) -> IdempotencyRecord | None:
    record = db.get(IdempotencyRecord, (tenant_id, key))
    if record is not None and _expired(record, utcnow()):
        db.delete(record)
        db.commit()
        return None
    return record


def _owner_marker(owner: str | None) -> str:
    return f"{_OWNER_PREFIX}{owner}" if owner else ""


def claim(
    db: Session, *, tenant_id: str, key: str, request_hash: str, owner: str | None = None
) -> bool:
    """Insert the pending record. False means another request holds the key right now."""
    if random.random() < CLEANUP_PROBABILITY:
        cleanup(db)
    db.add(
        IdempotencyRecord(
            tenant_id=tenant_id,
            idempotency_key=key,
            request_hash=request_hash,
            status_code=PENDING_STATUS,
            response_json=_owner_marker(owner),
            created_at=utcnow(),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def release(db: Session, tenant_id: str, key: str, owner: str | None = None) -> None:
    """Drop our pending record after a failed request so a retry can run.

    With `owner`, only a pending record claimed by that request is deleted: if our claim expired and
    a retry took the key over, the retry's claim is left alone.
    """
    stmt = delete(IdempotencyRecord).where(
        IdempotencyRecord.tenant_id == tenant_id,
        IdempotencyRecord.idempotency_key == key,
        IdempotencyRecord.status_code == PENDING_STATUS,
    )
    if owner:
        stmt = stmt.where(IdempotencyRecord.response_json == _owner_marker(owner))
    db.execute(stmt)
    db.commit()


def cleanup(db: Session) -> int:
    """Delete expired records (completed after TTL, pending after PENDING_TTL). Returns rows removed."""
    now = utcnow()
    completed = db.execute(
        delete(IdempotencyRecord).where(
            IdempotencyRecord.status_code != PENDING_STATUS,
            IdempotencyRecord.created_at < now - TTL,
        )
    ).rowcount
    pending = db.execute(
        delete(IdempotencyRecord).where(
            IdempotencyRecord.status_code == PENDING_STATUS,
            IdempotencyRecord.created_at < now - PENDING_TTL,
        )
    ).rowcount
    db.commit()
    return (completed or 0) + (pending or 0)


def store(
    db: Session,
    *,
    tenant_id: str,
    key: str,
    request_hash: str,
    status_code: int,
    response_json: str,
    owner: str | None = None,
) -> bool:
    """Replace our pending record with the response. Returns False, storing nothing, when `owner` is
    given and the key now belongs to another request (our claim expired and was taken over)."""
    if owner:
        current = db.get(IdempotencyRecord, (tenant_id, key))
        if (
            current is not None
            and is_pending(current)
            and current.response_json != _owner_marker(owner)
        ):
            return False
    db.merge(
        IdempotencyRecord(
            tenant_id=tenant_id,
            idempotency_key=key,
            request_hash=request_hash,
            status_code=status_code,
            response_json=response_json,
            created_at=utcnow(),
        )
    )
    return True
