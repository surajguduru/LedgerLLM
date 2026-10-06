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
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _body(request: Request, code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": _request_id(request)}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        headers = dict(exc.headers)
        rid = _request_id(request)
        if rid:
            headers.setdefault("X-Request-ID", rid)
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(request, exc.code, exc.message),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        msg = first.get("msg", "invalid request")
        message = f"{loc}: {msg}" if loc else msg
        return JSONResponse(status_code=422, content=_body(request, "validation_error", message))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error=str(exc))
        return JSONResponse(
            status_code=500,
            content=_body(request, "internal_error", "internal error"),
            headers={"X-Request-ID": _request_id(request) or ""},
        )
