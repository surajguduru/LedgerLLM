"""Single error contract for the API.

Every error response has the shape:
    {"error": {"code": "<stable_snake_case>", "message": "<human text>", "request_id": "<id>"}}

Codes and statuses (keep this table in sync with docs/DESIGN.md):
    401 missing_api_key / invalid_api_key      403 tenant_suspended / model_not_allowed
    429 rate_limited (+Retry-After)            402 budget_exceeded (monthly hard cutoff)
    400 blocked_input (guardrail)              400 fetch_blocked (SSRF guard)
    422 fetch_failed / validation_error        413 content_too_large
    409 idempotency_conflict                   502 upstream_error
    409 idempotency_in_progress (+Retry-After)
    401 admin_unauthorized                     500 internal_error
    404 tenant_not_found / key_not_found       409 key_revoked (rotate of a revoked key)
    404 request_not_found (feedback)           401 metrics_unauthorized (METRICS_TOKEN set)
    404 not_found (no such route)              405 method_not_allowed
    Tenant portal (/app/api): 401 not_signed_in / session_expired / invalid_credentials,
    409 email_taken, 403 cross_origin, 415 unsupported_media_type
"""

from __future__ import annotations

from http import HTTPStatus

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.observability.logging import note_error

log = structlog.get_logger()


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


# Codes for errors Starlette raises itself (no route, wrong method, the body cap below). Any other status
# gets its HTTP reason phrase in snake_case. 413 is spelled out: its phrase differs across Pythons.
_HTTP_CODES = {404: "not_found", 405: "method_not_allowed", 413: "content_too_large"}


def _http_code(status: int) -> str:
    if status in _HTTP_CODES:
        return _HTTP_CODES[status]
    try:
        return HTTPStatus(status).phrase.lower().replace(" ", "_").replace("-", "_")
    except ValueError:
        return "http_error"


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _body(request: Request, code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": _request_id(request)}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        note_error(request, exc.code)
        headers = dict(exc.headers)
        rid = _request_id(request)
        if rid:
            headers.setdefault("X-Request-ID", rid)
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(request, exc.code, exc.message),
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _http_code(exc.status_code)
        note_error(request, code)
        if exc.status_code == 404:
            message = f"no route {request.method} {request.url.path}"
        elif exc.status_code == 405:
            message = f"{request.method} is not allowed on {request.url.path}"
        else:
            message = str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(request, code, message),
            headers=exc.headers,  # e.g. Allow on a 405
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        # For unparseable JSON, loc is a character offset and msg a bare "JSON decode error".
        if first.get("type") == "json_invalid":
            detail = first.get("ctx", {}).get("error")
            message = f"invalid JSON body ({detail})" if detail else "invalid JSON body"
        else:
            loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
            msg = first.get("msg", "invalid request")
            message = f"{loc}: {msg}" if loc else msg
        note_error(request, "validation_error")
        return JSONResponse(status_code=422, content=_body(request, "validation_error", message))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        note_error(request, "internal_error")
        log.exception("unhandled_error", error=str(exc))
        return JSONResponse(
            status_code=500,
            content=_body(request, "internal_error", "internal error"),
            headers={"X-Request-ID": _request_id(request) or ""},
        )


class _BodyTooLarge(HTTPException):
    def __init__(self, limit: int) -> None:
        super().__init__(413, f"request body is larger than {limit} bytes")


class BodySizeLimitMiddleware:
    """Refuse request bodies over MAX_REQUEST_BYTES with 413 content_too_large.

    A declared Content-Length over the cap is refused before anything is read. A body without one
    (chunked) or that lies about it is counted as it is read, and reading stops at the cap: the
    receive wrapper raises a FastAPI HTTPException, which the route's body parsing re-raises as is
    and the StarletteHTTPException handler above turns into the envelope.

    Pure ASGI rather than BaseHTTPMiddleware, so the body is never buffered here; it sits inside
    RequestContextMiddleware, so the 413 carries the request id.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = get_settings().max_request_bytes
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            request = Request(scope)
            note_error(request, "content_too_large")
            response = JSONResponse(
                status_code=413,
                content=_body(request, "content_too_large", _BodyTooLarge(limit).detail),
            )
            await response(scope, receive, send)
            return

        received = 0

        async def capped_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge(limit)
            return message

        await self.app(scope, capped_receive, send)
