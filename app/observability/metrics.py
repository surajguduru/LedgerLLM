"""Prometheus metrics. HTTP-level metrics come from the instrumentator; these are the LLM-specific ones.

Label cardinality note: `tenant` is the tenant id. Fine for tens/hundreds of tenants; at 10x scale,
drop the tenant label here and aggregate per-tenant from usage_ledger instead (see docs/DESIGN.md).
"""

from __future__ import annotations

from fastapi import FastAPI
from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

TOKENS = Counter(
    "ledgerllm_tokens_total",
    "Tokens booked to tenants",
    ["tenant", "model", "direction", "purpose"],
)
COST = Counter(
    "ledgerllm_cost_microusd_total", "Cost booked in micro-USD", ["tenant", "model", "purpose"]
)
REJECTIONS = Counter(
    "ledgerllm_rejections_total", "Requests refused before the LLM call", ["reason"]
)  # rate_limited | budget_exceeded | blocked_input | model_not_allowed | fetch_failed
LLM_LATENCY = Histogram(
    "ledgerllm_llm_latency_seconds",
    "Upstream LLM call latency",
    ["model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21),
)
GUARDRAIL = Counter(
    "ledgerllm_guardrail_verdicts_total", "Guardrail verdicts", ["stage", "blocked", "category"]
)
FEEDBACK = Counter("ledgerllm_feedback_total", "Thumbs up/down", ["rating"])
CACHE = Counter(
    "ledgerllm_cache_total", "Response cache lookups", ["result"]
)  # hit | miss | bypass
UPSTREAM_ERRORS = Counter(
    "ledgerllm_upstream_errors_total", "Provider errors", ["model", "retryable"]
)


def setup_metrics(app: FastAPI) -> None:
    Instrumentator(
        should_group_status_codes=False,
        excluded_handlers=["/metrics", "/healthz"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


def record_booking(
    *,
    tenant: str,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    cost_microusd: int,
) -> None:
    TOKENS.labels(tenant, model, "input", purpose).inc(input_tokens)
    TOKENS.labels(tenant, model, "output", purpose).inc(output_tokens)
    COST.labels(tenant, model, purpose).inc(cost_microusd)
