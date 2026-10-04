"""/admin/* — tenant and key management. Protected by ADMIN_TOKEN (Authorization: Bearer <token>).

OWNER: Loukik for hardening: key revoke/rotate endpoints, list keys, suspend tenant, change plan/budget.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.keys import generate_key
from app.compliance import audit as audit_events
from app.compliance.audit import audit
from app.config import get_settings
from app.db import get_db
from app.errors import ApiError
from app.models import ApiKey, Tenant
from app.plans import load_plans, microusd_to_usd, usd_to_microusd
from app.schemas import (
    ApiKeyOut,
    CreateKeyRequest,
    CreateKeyResponse,
    CreateTenantRequest,
    CreateTenantResponse,
    TenantOut,
)


def require_admin(authorization: str | None = Header(None)) -> None:
    token = (
        authorization[7:].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else ""
    )
    if not token or not secrets.compare_digest(token, get_settings().admin_token):
        raise ApiError(401, "admin_unauthorized", "admin token required")


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _tenant_out(t: Tenant) -> TenantOut:
    return TenantOut(
        id=t.id,
        name=t.name,
        plan=t.plan,
        status=t.status,
        budget_override_usd=microusd_to_usd(t.budget_override_microusd)
        if t.budget_override_microusd is not None
        else None,
        created_at=t.created_at,
    )


def _key_out(k: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=k.id,
        tenant_id=k.tenant_id,
        name=k.name,
        key_prefix=k.key_prefix,
        created_at=k.created_at,
        revoked_at=k.revoked_at,
    )


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(db: Session = Depends(get_db)) -> list[TenantOut]:
    return [_tenant_out(t) for t in db.scalars(select(Tenant).order_by(Tenant.created_at)).all()]


@router.post("/tenants", response_model=CreateTenantResponse, status_code=201)
def create_tenant(
    payload: CreateTenantRequest, db: Session = Depends(get_db)
) -> CreateTenantResponse:
    if payload.plan not in load_plans().plans:
        raise ApiError(422, "validation_error", f"unknown plan '{payload.plan}'")
    tenant = Tenant(
        name=payload.name,
        plan=payload.plan,
        budget_override_microusd=usd_to_microusd(payload.budget_override_usd)
        if payload.budget_override_usd is not None
        else None,
    )
    db.add(tenant)
    db.flush()
    raw, prefix, digest = generate_key()
    key = ApiKey(tenant_id=tenant.id, name=payload.key_name, key_prefix=prefix, key_hash=digest)
    db.add(key)
    db.flush()
    audit(
        db,
        audit_events.TENANT_CREATED,
        tenant_id=tenant.id,
        actor="admin",
        details={"plan": tenant.plan, "name": tenant.name},
    )
    audit(
        db,
        audit_events.KEY_CREATED,
        tenant_id=tenant.id,
        key_id=key.id,
        actor="admin",
        details={"prefix": prefix},
    )
    db.commit()
    return CreateTenantResponse(tenant=_tenant_out(tenant), key=_key_out(key), api_key=raw)


@router.post("/tenants/{tenant_id}/keys", response_model=CreateKeyResponse, status_code=201)
def create_key(
    tenant_id: str, payload: CreateKeyRequest, db: Session = Depends(get_db)
) -> CreateKeyResponse:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise ApiError(404, "tenant_not_found", "no such tenant")
    raw, prefix, digest = generate_key()
    key = ApiKey(tenant_id=tenant.id, name=payload.name, key_prefix=prefix, key_hash=digest)
    db.add(key)
    db.flush()
    audit(
        db,
        audit_events.KEY_CREATED,
        tenant_id=tenant.id,
        key_id=key.id,
        actor="admin",
        details={"prefix": prefix},
    )
    db.commit()
    return CreateKeyResponse(key=_key_out(key), api_key=raw)
