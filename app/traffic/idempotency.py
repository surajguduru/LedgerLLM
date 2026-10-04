"""Idempotency keys: same (tenant, key, body) -> replay the stored response; same key, different body -> 409.

OWNER: Suraj. Base implements lookup/store; to do: TTL + cleanup of old records, and the
in-flight-duplicate policy (two identical requests at the same instant). Only 200 responses are stored.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import IdempotencyRecord


def hash_request(payload: BaseModel) -> str:
    return hashlib.sha256(payload.model_dump_json(exclude_none=True).encode()).hexdigest()


def lookup(db: Session, tenant_id: str, key: str) -> IdempotencyRecord | None:
    return db.get(IdempotencyRecord, (tenant_id, key))


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
        )
    )
