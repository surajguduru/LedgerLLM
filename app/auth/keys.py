"""API key generation and hashing. Raw key format: llk_<prefix8>_<secret>. Only the sha256 is stored."""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.compliance import audit as audit_events
from app.compliance.audit import audit
from app.errors import ApiError
from app.models import ApiKey, utcnow

KEY_PREFIX = "llk"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """Returns (raw_key, display_prefix, key_hash)."""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(24)
    raw = f"{KEY_PREFIX}_{prefix}_{secret}"
    return raw, f"{KEY_PREFIX}_{prefix}", hash_key(raw)


# --- key lifecycle, shared by the admin API and the tenant portal ---------------------------------


def create_key(db: Session, tenant_id: str, name: str, *, actor: str) -> tuple[ApiKey, str]:
    """Adds a key and its audit event (caller commits). Returns (row, raw key shown once)."""
    raw, prefix, digest = generate_key()
    key = ApiKey(tenant_id=tenant_id, name=name, key_prefix=prefix, key_hash=digest)
    db.add(key)
    db.flush()
    audit(
        db,
        audit_events.KEY_CREATED,
        tenant_id=tenant_id,
        key_id=key.id,
        actor=actor,
        details={"prefix": prefix},
    )
    return key, raw


def revoke_key(db: Session, key: ApiKey, *, actor: str) -> None:
    """Idempotent: revoking a revoked key writes nothing (caller commits)."""
    if key.revoked_at is not None:
        return
    key.revoked_at = utcnow()
    audit(
        db,
        audit_events.KEY_REVOKED,
        tenant_id=key.tenant_id,
        key_id=key.id,
        actor=actor,
        details={"prefix": key.key_prefix},
    )


def rotate_key(db: Session, key: ApiKey, *, actor: str) -> tuple[ApiKey, str]:
    """Revoke-and-replace, at most once per key (caller commits).

    A retried, double-clicked or concurrent rotate of the same key loses the conditional UPDATE and
    gets 409 instead of minting a second live key.
    """
    won = db.execute(
        update(ApiKey)
        .where(ApiKey.id == key.id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    ).rowcount
    if won != 1:
        db.rollback()
        raise ApiError(409, "key_revoked", "key is already revoked or rotated; rotate the live key")
    new, raw = create_key(db, key.tenant_id, key.name, actor=actor)
    audit(
        db,
        audit_events.KEY_ROTATED,
        tenant_id=key.tenant_id,
        key_id=new.id,
        actor=actor,
        details={"prefix": new.key_prefix, "rotated_from": key.id},
    )
    return new, raw
