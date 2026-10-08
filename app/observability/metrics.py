"""Prometheus metrics. HTTP-level metrics come from the instrumentator; these are the LLM-specific ones.

Label cardinality note: `tenant` is the tenant id. Fine for tens/hundreds of tenants; at 10x scale,
drop the tenant label here and aggregate per-tenant from usage_ledger instead (see docs/DESIGN.md).
"""

from __future__ import annotations

from fastapi import FastAPI
from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

# Buckets for the per-handler HTTP latency histogram. The library default is only (0.1, 0.5, 1),
# which cannot express either of our two latency targets. A histogram quantile is interpolated
# inside a bucket, so both targets from docs/DESIGN.md §1 are bucket EDGES here and can be read
# exactly: platform overhead p99 <= 25 ms (0.025) and end-to-end p99 <= 8 s (8).
HTTP_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21)

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
FEEDBACK = Counter(
    "ledgerllm_feedback_total", "Thumbs up/down", ["rating", "prompt_version"]
)  # prompt_version lets us compare satisfaction across prompt versions (quality signal, DESIGN.md §4)
PORTAL_LOGINS = Counter(
    "ledgerllm_portal_logins_total", "Tenant portal sign-ins", ["result"]
)  # ok | invalid | throttled | signup
PLAN_CHANGES = Counter(
    "ledgerllm_plan_changes_total", "Self-service plan changes in the portal", ["direction"]
)  # upgrade | downgrade
CACHE = Counter(
    "ledgerllm_cache_total", "Response cache lookups", ["result"]
)  # hit | miss | bypass
QUALITY_SCORE = Histogram(
    "ledgerllm_quality_score",
    "Online LLM-judge score (1-5) of sampled live summaries",
    ["prompt_version", "dimension"],  # dimension: faithfulness | coverage
    buckets=(1, 2, 3, 4, 5),
)
UPSTREAM_ERRORS = Counter(
    "ledgerllm_upstream_errors_total", "Provider errors", ["model", "retryable"]
)


def setup_metrics(app: FastAPI) -> None:
    Instrumentator(
        should_group_status_codes=False,
        excluded_handlers=["/metrics", "/healthz"],
    ).instrument(
        app,
        # Our buckets on http_request_duration_seconds, the handler-labelled histogram the
        # dashboard queries. Passed here rather than via .add(metrics.default(...)): the library
        # builds that instrumentation inside the middleware, and a second default() call in the
        # same process returns None (duplicate registration), which silently instruments nothing.
        latency_lowr_buckets=HTTP_LATENCY_BUCKETS,
    ).expose(app, endpoint="/metrics", include_in_schema=False)


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
