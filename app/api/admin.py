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
from app.models import ApiKey, AuditEvent, Tenant, utcnow
from app.plans import load_plans, microusd_to_usd, usd_to_microusd
from app.quality.stats import quality_report
from app.schemas import (
    ApiKeyOut,
    AuditOut,
    CreateKeyRequest,
    CreateKeyResponse,
    CreateTenantRequest,
    CreateTenantResponse,
    TenantOut,
    UpdateTenantRequest,
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


@router.get("/quality")
def quality(days: int = 7, recent: int = 20, db: Session = Depends(get_db)) -> dict:
    """Online quality sampling: judge means per prompt version, judge cost, recent samples (owner: Thrishal)."""
    return quality_report(db, days=max(1, min(days, 90)), recent=max(1, min(recent, 200)))


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


def _audit_out(e: AuditEvent) -> AuditOut:
    return AuditOut(
        id=e.id,
        created_at=e.created_at,
        tenant_id=e.tenant_id,
        key_id=e.key_id,
        request_id=e.request_id,
        actor=e.actor,
        event_type=e.event_type,
        details=e.details,
    )


@router.get("/tenants/{tenant_id}/keys", response_model=list[ApiKeyOut])
def list_keys(tenant_id: str, db: Session = Depends(get_db)) -> list[ApiKeyOut]:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise ApiError(404, "tenant_not_found", "no such tenant")
    rows = db.scalars(
        select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at)
    ).all()
    return [_key_out(k) for k in rows]


@router.delete("/keys/{key_id}", response_model=ApiKeyOut)
def revoke_key(key_id: str, db: Session = Depends(get_db)) -> ApiKeyOut:
    key = db.get(ApiKey, key_id)
    if key is None:
        raise ApiError(404, "key_not_found", "no such key")
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        audit(
            db,
            audit_events.KEY_REVOKED,
            tenant_id=key.tenant_id,
            key_id=key.id,
            actor="admin",
            details={"prefix": key.key_prefix},
        )
        db.commit()
    return _key_out(key)


@router.post("/keys/{key_id}/rotate", response_model=CreateKeyResponse, status_code=201)
def rotate_key(key_id: str, db: Session = Depends(get_db)) -> CreateKeyResponse:
    old = db.get(ApiKey, key_id)
    if old is None:
        raise ApiError(404, "key_not_found", "no such key")
    if old.revoked_at is None:
        old.revoked_at = utcnow()
    raw, prefix, digest = generate_key()
    new = ApiKey(tenant_id=old.tenant_id, name=old.name, key_prefix=prefix, key_hash=digest)
    db.add(new)
    db.flush()
    audit(
        db,
        audit_events.KEY_ROTATED,
        tenant_id=old.tenant_id,
        key_id=new.id,
        actor="admin",
        details={"prefix": prefix, "rotated_from": old.id},
    )
    audit(
        db,
        audit_events.KEY_CREATED,
        tenant_id=old.tenant_id,
        key_id=new.id,
        actor="admin",
        details={"prefix": prefix},
    )
    db.commit()
    return CreateKeyResponse(key=_key_out(new), api_key=raw)


@router.patch("/tenants/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: str, payload: UpdateTenantRequest, db: Session = Depends(get_db)
) -> TenantOut:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise ApiError(404, "tenant_not_found", "no such tenant")
    before = {
        "plan": tenant.plan,
        "status": tenant.status,
        "budget_override_microusd": tenant.budget_override_microusd,
    }
    if payload.plan is not None:
        if payload.plan not in load_plans().plans:
            raise ApiError(422, "validation_error", f"unknown plan '{payload.plan}'")
        tenant.plan = payload.plan
    if payload.status is not None:
        if payload.status not in ("active", "suspended"):
            raise ApiError(422, "validation_error", "status must be active|suspended")
        tenant.status = payload.status
    if payload.budget_override_usd is not None:
        tenant.budget_override_microusd = usd_to_microusd(payload.budget_override_usd)
    after = {
        "plan": tenant.plan,
        "status": tenant.status,
        "budget_override_microusd": tenant.budget_override_microusd,
    }
    audit(
        db,
        audit_events.TENANT_UPDATED,
        tenant_id=tenant.id,
        actor="admin",
        details={"before": before, "after": after},
    )
    db.commit()
    return _tenant_out(tenant)


@router.get("/audit", response_model=list[AuditOut])
def list_audit(
    tenant_id: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    before: str | None = None,
    db: Session = Depends(get_db),
) -> list[AuditOut]:
    """Newest-first audit trail — the demo of "why was I refused"."""
    limit = max(1, min(limit, 200))
    stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    if tenant_id:
        stmt = stmt.where(AuditEvent.tenant_id == tenant_id)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if before:
        anchor = db.get(AuditEvent, before)
        if anchor is not None:
            stmt = stmt.where(
                (AuditEvent.created_at < anchor.created_at)
                | ((AuditEvent.created_at == anchor.created_at) & (AuditEvent.id < anchor.id))
            )
    return [_audit_out(e) for e in db.scalars(stmt.limit(limit)).all()]
