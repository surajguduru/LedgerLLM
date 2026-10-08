from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.api import admin, dashboard, feedback, health, summarize, usage
from app.config import check_production_safety, get_settings
from app.db import init_db
from app.errors import BodySizeLimitMiddleware, install_error_handlers
from app.llm import get_provider
from app.observability.logging import RequestContextMiddleware, configure_logging
from app.observability.metrics import setup_metrics
from app.portal import api as portal_api
from app.portal import pages as portal_pages


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    get_provider()  # fail fast on a misconfigured LLM_PROVIDER / missing LLM_API_KEY
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    check_production_safety(settings)
    configure_logging(settings)
    app = FastAPI(
        title="LedgerLLM",
        version="0.1.0",
        description=(
            "Multi-tenant LLM API (summarize any URL) with per-tenant cost attribution, "
            "tiered rate limits, monthly budgets, abuse protection, idempotency and audit logging."
        ),
        lifespan=lifespan,
    )
    # Added first, so it runs inside the request context and its 413 carries the request id.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    setup_metrics(app)
    for r in (
        health.router,
        summarize.router,
        usage.router,
        feedback.router,
        admin.router,
        dashboard.router,
        portal_api.router,
        portal_pages.router,
    ):
        app.include_router(r)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/app")

    return app


app = create_app()
