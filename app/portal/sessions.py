"""Portal sessions and the request guards around them (D24).

- The cookie holds a random token; the database stores only its sha256, like API keys.
- HttpOnly + SameSite=Strict, Secure outside dev/test.
- Every state-changing call must be JSON with an Origin matching the host. A cross-site HTML form can
  send neither, and SameSite=Strict keeps the cookie off cross-site requests anyway (CSRF).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import ApiError
from app.models import PortalSession, Tenant, User, utcnow
from app.plans import Plan, get_plan

COOKIE = "ledger_session"


@dataclass
class PortalContext:
    user: User
    tenant: Tenant
    plan: Plan
    session_id: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _cookie_secure() -> bool:
    s = get_settings()
    if s.portal_cookie_secure is not None:
        return s.portal_cookie_secure
    return s.app_env not in ("dev", "test")


def start_session(db: Session, response: Response, user: User) -> None:
    """Creates the session row (caller commits) and sets the cookie."""
    token = secrets.token_urlsafe(32)
    days = get_settings().portal_session_days
    db.add(
        PortalSession(
            id=_token_hash(token), user_id=user.id, expires_at=utcnow() + timedelta(days=days)
        )
    )
    response.set_cookie(
        COOKIE,
        token,
        max_age=days * 86400,
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
        path="/",
    )


def end_session(db: Session, response: Response, session_id: str) -> None:
    db.execute(
        update(PortalSession)
        .where(PortalSession.id == session_id, PortalSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    response.delete_cookie(COOKIE, path="/")


def require_same_origin(request: Request) -> None:
    """CSRF guard for state-changing portal calls."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise ApiError(415, "unsupported_media_type", "send application/json")
    origin = request.headers.get("origin")
    if not origin or urlsplit(origin).netloc != request.headers.get("host"):
        raise ApiError(403, "cross_origin", "cross-origin request refused")


def current_user(request: Request, db: Session = Depends(get_db)) -> PortalContext:
    token = request.cookies.get(COOKIE)
    if not token:
        raise ApiError(401, "not_signed_in", "sign in first")
    row = db.execute(
        select(PortalSession, User, Tenant)
        .join(User, User.id == PortalSession.user_id)
        .join(Tenant, Tenant.id == User.tenant_id)
        .where(PortalSession.id == _token_hash(token))
    ).first()
    if row is None:
        raise ApiError(401, "not_signed_in", "sign in first")
    session, user, tenant = row
    expires = session.expires_at
    if expires.tzinfo is None:  # SQLite returns naive UTC
        expires = expires.replace(tzinfo=utcnow().tzinfo)
    if session.revoked_at is not None or expires <= utcnow():
        raise ApiError(401, "session_expired", "your session has ended; sign in again")
    return PortalContext(
        user=user, tenant=tenant, plan=get_plan(tenant.plan), session_id=session.id
    )
