"""Exact-match response cache per tenant.

OWNER: Suraj. The base ships no-op hooks so the pipeline is already wired (stage 4½, before the budget reserve,
so a hit costs the tenant nothing and never touches the budget). To implement: a `response_cache` table
(tenant_id, cache_key, response_json, model, prompt_version, created_at, hits) with a TTL; `lookup` returns the
stored SummaryResult-like payload; `store` writes it after a successful completion. Decide and document the
billing policy for hits (free / flat fee) and expose `ledgerllm_cache_total{result}` in metrics.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.orm import Session


@dataclass
class CachedSummary:
    text: str
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int


def cache_key(
    *,
    tenant_id: str,
    model: str,
    prompt_hash: str,
    style: str,
    max_words: int,
    instructions: str | None,
    text: str,
) -> str:
    raw = "\x1f".join(
        [tenant_id, model, prompt_hash, style, str(max_words), instructions or "", text]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lookup(db: Session, tenant_id: str, key: str) -> CachedSummary | None:
    return None  # STUB — Suraj


def store(db: Session, tenant_id: str, key: str, summary: CachedSummary) -> None:
    return None  # STUB — Suraj
