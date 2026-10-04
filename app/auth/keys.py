"""API key generation and hashing. Raw key format: llk_<prefix8>_<secret>. Only the sha256 is stored."""

from __future__ import annotations

import hashlib
import secrets

KEY_PREFIX = "llk"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """Returns (raw_key, display_prefix, key_hash)."""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(24)
    raw = f"{KEY_PREFIX}_{prefix}_{secret}"
    return raw, f"{KEY_PREFIX}_{prefix}", hash_key(raw)
