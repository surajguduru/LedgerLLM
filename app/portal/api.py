"""Tenant portal JSON API under /app/api: sign-up, sign-in, self-service keys and usage (D24).

Every state-changing route is a JSON POST guarded by require_same_origin (CSRF); everything except
sign-up and sign-in needs a session (current_user). Key ids are always checked against the signed-in
tenant, and someone else's key is reported as not found rather than forbidden.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.summarize import summarize as run_summarize
from app.auth import keys
from app.auth.dependency import AuthContext
from app.billing import budget
from app.billing.pricing import load_prices
from app.billing.usage import daily_by_key, statement_csv, summarize_usage, usage_by_key
from app.compliance import audit as audit_events
from app.compliance.audit import audit
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import ApiError
from app.models import ApiKey, Tenant, User, utcnow
from app.observability import metrics
from app.observability.logging import note_tenant
from app.portal.passwords import DUMMY_HASH, MIN_LENGTH, hash_password, verify_password
from app.portal.sessions import (
    PortalContext,
    current_user,
    end_session,
    require_same_origin,
    start_session,
)
from app.schemas import SummarizeRequest
from app.traffic.ratelimit import hit

router = APIRouter(prefix="/app/api", tags=["portal"])

SIGNUP_PLAN = "free"
ACTOR = "portal"  # audit actor for self-service actions; one user per tenant, id in details
LOGINS_PER_EMAIL_PER_MIN = 5
LOGINS_PER_IP_PER_MIN = 20
SIGNUPS_PER_IP_PER_MIN = 10
PERIODS_SHOWN = 6
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PERIOD = r"^\d{4}-(0[1-9]|1[0-2])$"


class SignupRequest(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=200)
    company: str | None = Field(None, max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=200)


class PlaygroundRequest(SummarizeRequest):
    key_id: str = Field(..., description="one of your live keys; the call is billed to it")
    bypass_cache: bool = False


class KeyRequest(BaseModel):
    name: str = Field("default", min_length=1, max_length=100)


def _bucket(kind: str, value: str) -> str:
    return f"p{kind}:" + hashlib.sha256(value.encode()).hexdigest()[:32]  # fits the 36-char column


def _throttle(db: Session, *buckets: tuple[str, int]) -> None:
    for bucket, limit in buckets:
        result = hit(db, bucket, limit)
        if not result.allowed:
            metrics.PORTAL_LOGINS.labels("throttled").inc()
            raise ApiError(
                429,
                "rate_limited",
                "too many attempts; wait a minute and try again",
                headers={"Retry-After": str(result.retry_after_s)},
            )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _me(ctx_user: User, tenant: Tenant, db: Session) -> dict:
    return {
        "user": {"email": ctx_user.email},
        "tenant": {
            "id": tenant.id,
            "name": tenant.name,
            "plan": tenant.plan,
            "status": tenant.status,
        },
        "keys": {"live": _live_keys(db, tenant.id)},
    }


def _live_keys(db: Session, tenant_id: str) -> int:
    return db.scalar(
        select(func.count())
        .select_from(ApiKey)
        .where(ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None))
    )


def _own_key(db: Session, ctx: PortalContext, key_id: str) -> ApiKey:
    key = db.get(ApiKey, key_id)
    if key is None or key.tenant_id != ctx.tenant.id:
        raise ApiError(404, "key_not_found", "no such key")
    return key


def _key_out(key: ApiKey) -> dict:
    return {
        "key_id": key.id,
        "name": key.name,
        "key_prefix": key.key_prefix,
        "created_at": key.created_at.isoformat(),
    }


def _recent_periods(n: int = PERIODS_SHOWN) -> list[str]:
    now = datetime.now(UTC)
    year, month, out = now.year, now.month, []
    for _ in range(n):
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return out


# --- sign-up / sign-in -----------------------------------------------------------------------


@router.post("/signup", status_code=201, dependencies=[Depends(require_same_origin)])
def signup(
    payload: SignupRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> dict:
    _throttle(db, (_bucket("s", _client_ip(request)), SIGNUPS_PER_IP_PER_MIN))
    email = payload.email.strip().lower()
    if not _EMAIL.match(email):
        raise ApiError(422, "validation_error", "enter a valid e-mail address")
    if len(payload.password) < MIN_LENGTH:
        raise ApiError(
            422, "validation_error", f"password must be at least {MIN_LENGTH} characters"
        )
    if db.scalar(select(User.id).where(User.email == email)):
        raise ApiError(409, "email_taken", "an account with this e-mail already exists")

    tenant = Tenant(name=(payload.company or "").strip() or email.split("@")[1], plan=SIGNUP_PLAN)
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, email=email, password_hash=hash_password(payload.password))
    db.add(user)
    db.flush()
    actor = ACTOR
    audit(
        db,
        audit_events.TENANT_CREATED,
        tenant_id=tenant.id,
        actor=actor,
        details={"plan": tenant.plan, "name": tenant.name, "via": "signup"},
    )
    audit(
        db, audit_events.USER_SIGNUP, tenant_id=tenant.id, actor=actor, details={"user_id": user.id}
    )
    key, raw = keys.create_key(db, tenant.id, "default", actor=actor)
    start_session(db, response, user)
    try:
        db.commit()
    except IntegrityError as exc:  # lost a race for the same e-mail
        db.rollback()
        raise ApiError(409, "email_taken", "an account with this e-mail already exists") from exc
    metrics.PORTAL_LOGINS.labels("signup").inc()
    return {**_me(user, tenant, db), "api_key": raw, "key": _key_out(key)}


@router.post("/login", dependencies=[Depends(require_same_origin)])
def login(
    payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> dict:
    email = payload.email.strip().lower()
    _throttle(
        db,
        (_bucket("e", email), LOGINS_PER_EMAIL_PER_MIN),
        (_bucket("i", _client_ip(request)), LOGINS_PER_IP_PER_MIN),
    )
    user = db.scalar(select(User).where(User.email == email))
    # Verify against a dummy hash for unknown e-mails so both failures take the same time.
    ok = verify_password(payload.password, user.password_hash if user else DUMMY_HASH)
    if not (user and ok):
        metrics.PORTAL_LOGINS.labels("invalid").inc()
        if user:
            audit(
                db,
                audit_events.USER_LOGIN_FAILED,
                tenant_id=user.tenant_id,
                actor=ACTOR,
                details={"user_id": user.id},
            )
            db.commit()
        raise ApiError(401, "invalid_credentials", "invalid e-mail or password")
    user.last_login_at = utcnow()
    tenant = db.get(Tenant, user.tenant_id)
    audit(
        db, audit_events.USER_LOGIN, tenant_id=tenant.id, actor=ACTOR, details={"user_id": user.id}
    )
    start_session(db, response, user)
    db.commit()
    metrics.PORTAL_LOGINS.labels("ok").inc()
    return _me(user, tenant, db)


@router.post("/logout", status_code=204, dependencies=[Depends(require_same_origin)])
def logout(
    response: Response, ctx: PortalContext = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    end_session(db, response, ctx.session_id)
    audit(
        db,
        audit_events.USER_LOGOUT,
        tenant_id=ctx.tenant.id,
        actor=ACTOR,
        details={"user_id": ctx.user.id},
    )
    db.commit()
    response.status_code = 204
    return response


@router.get("/me")
def me(ctx: PortalContext = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return _me(ctx.user, ctx.tenant, db)


# --- keys --------------------------------------------------------------------------------------


@router.get("/keys")
def list_keys(ctx: PortalContext = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    period = budget.current_period()
    return {
        "period": period,
        "live": _live_keys(db, ctx.tenant.id),
        "keys": usage_by_key(db, ctx.tenant, period),
    }


@router.post("/keys", status_code=201, dependencies=[Depends(require_same_origin)])
def create_key(
    payload: KeyRequest, ctx: PortalContext = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    if ctx.tenant.status != "active":
        raise ApiError(403, "tenant_suspended", "this account is suspended")
    key, raw = keys.create_key(db, ctx.tenant.id, payload.name.strip(), actor=ACTOR)
    db.commit()
    return {"key": _key_out(key), "api_key": raw}


@router.post("/keys/{key_id}/revoke", dependencies=[Depends(require_same_origin)])
def revoke_key(
    key_id: str, ctx: PortalContext = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    key = _own_key(db, ctx, key_id)
    keys.revoke_key(db, key, actor=ACTOR)
    db.commit()
    return {"key_id": key.id, "revoked_at": key.revoked_at.isoformat()}


@router.post("/keys/{key_id}/rotate", status_code=201, dependencies=[Depends(require_same_origin)])
def rotate_key(
    key_id: str, ctx: PortalContext = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    if ctx.tenant.status != "active":
        raise ApiError(403, "tenant_suspended", "this account is suspended")
    key = _own_key(db, ctx, key_id)
    new, raw = keys.rotate_key(db, key, actor=ACTOR)
    db.commit()
    return {"key": _key_out(new), "api_key": raw, "rotated_from": key.id}


# --- usage -------------------------------------------------------------------------------------


@router.get("/usage")
def usage(
    period: str | None = Query(None, pattern=_PERIOD),
    ctx: PortalContext = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    period = period or budget.current_period()
    summary = summarize_usage(db, ctx.tenant, ctx.plan, period)
    return {
        "summary": summary.model_dump(),
        "by_key": usage_by_key(db, ctx.tenant, period),
        "daily_by_key": daily_by_key(db, ctx.tenant, period),
        "periods": _recent_periods(),
    }


@router.get("/statement.csv")
def statement(
    period: str | None = Query(None, pattern=_PERIOD),
    ctx: PortalContext = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    period = period or budget.current_period()
    return Response(
        content=statement_csv(db, ctx.tenant, period),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ledgerllm-{period}.csv"'},
    )


# --- playground --------------------------------------------------------------------------------


@router.get("/models")
def models(
    ctx: PortalContext = Depends(current_user), settings: Settings = Depends(get_settings)
) -> dict:
    """Models the tenant's plan may call, with list prices, so the playground can offer a choice."""
    prices = load_prices()
    default = ctx.plan.default_model or settings.default_model
    return {
        "default": default if default in ctx.plan.allowed_models else ctx.plan.allowed_models[0],
        "price_version": prices.version,
        "models": [
            {
                "id": m,
                "input_usd_per_mtok": prices.get(m).input_usd_per_mtok,
                "output_usd_per_mtok": prices.get(m).output_usd_per_mtok,
            }
            for m in ctx.plan.allowed_models
        ],
    }


@router.post("/playground/summarize", dependencies=[Depends(require_same_origin)])
def playground_summarize(
    payload: PlaygroundRequest,
    request: Request,
    response: Response,
    ctx: PortalContext = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Runs the real POST /v1/summarize pipeline as one of the tenant's own keys.

    The session proves ownership, so the raw key (shown only once) is never needed. Rate limit,
    budget, guardrails, cache and ledger all apply exactly as for an API call with that key.
    """
    key = _own_key(db, ctx, payload.key_id)
    if key.revoked_at is not None:
        raise ApiError(401, "invalid_api_key", "that key is revoked; pick a live key")
    if ctx.tenant.status != "active":
        raise ApiError(403, "tenant_suspended", "tenant is not active")
    key.last_used_at = utcnow()
    note_tenant(request, ctx.tenant.id)
    body = SummarizeRequest(**payload.model_dump(exclude={"key_id", "bypass_cache"}))
    return run_summarize(
        body,
        request,
        response,
        db=db,
        auth=AuthContext(tenant=ctx.tenant, api_key=key, plan=ctx.plan),
        settings=settings,
        idempotency_key=None,
        cache_control="no-cache" if payload.bypass_cache else None,
    )
