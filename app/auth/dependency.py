"""Resolves the caller's API key to a tenant + plan. Accepts `X-API-Key: llk_...` or `Authorization: Bearer llk_...`."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.keys import hash_key
from app.db import get_db
from app.errors import ApiError
from app.models import ApiKey, Tenant, utcnow
from app.observability.logging import note_tenant
from app.plans import Plan, get_plan


@dataclass
class AuthContext:
    tenant: Tenant
    api_key: ApiKey
    plan: Plan


def _extract_raw_key(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def get_auth_context(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None),
) -> AuthContext:
    raw = _extract_raw_key(x_api_key, authorization)
    if not raw:
        raise ApiError(
            401, "missing_api_key", "pass your key in X-API-Key or Authorization: Bearer"
        )
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
    if key is None or key.revoked_at is not None:
        raise ApiError(401, "invalid_api_key", "unknown or revoked API key")
    tenant = db.get(Tenant, key.tenant_id)
    if tenant is None or tenant.status != "active":
        raise ApiError(403, "tenant_suspended", "tenant is not active")
    # TODO: this write-per-request is fine at our scale; batch or sample it at 10x.
    key.last_used_at = utcnow()
    # Puts tenant_id on the http_request log line, so a refusal is traceable to a customer
    # from the logs alone (see app/observability/logging.py).
    note_tenant(request, tenant.id)
    return AuthContext(tenant=tenant, api_key=key, plan=get_plan(tenant.plan))
