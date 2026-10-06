"""Idempotency keys: same (tenant, key, body) -> replay the stored response; same key, different body -> 409.

OWNER: Suraj. Records live for TTL (24 h); expired records are ignored on lookup and deleted
opportunistically on about 1 % of claims.

In-flight policy: before the pipeline runs, `claim` inserts a *pending* record (status_code 0). The primary
key on (tenant_id, idempotency_key) makes the claim atomic, so of two identical requests arriving at the same
instant exactly one runs and the other gets 409 `idempotency_in_progress`. On success `store` overwrites the
pending record with the response; on any failure `release` deletes it so the client can retry. Only 200
responses are stored. A pending record left behind by a crashed process stops blocking after PENDING_TTL.
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
PENDING_TTL = timedelta(minutes=2)  # longer than fetch timeout + LLM timeout
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


def claim(db: Session, *, tenant_id: str, key: str, request_hash: str) -> bool:
    """Insert the pending record. False means another request holds the key right now."""
    if random.random() < CLEANUP_PROBABILITY:
        cleanup(db)
    db.add(
        IdempotencyRecord(
            tenant_id=tenant_id,
            idempotency_key=key,
            request_hash=request_hash,
            status_code=PENDING_STATUS,
            response_json="",
            created_at=utcnow(),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def release(db: Session, tenant_id: str, key: str) -> None:
    """Drop our pending record after a failed request so a retry can run."""
    db.execute(
        delete(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == tenant_id,
            IdempotencyRecord.idempotency_key == key,
            IdempotencyRecord.status_code == PENDING_STATUS,
        )
    )
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
) -> None:
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
