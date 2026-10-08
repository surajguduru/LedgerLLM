"""Prometheus metrics. OWNER: Yashraj.

These tests assert the counters actually move, because a dashboard built on a metric that never
increments looks healthy while telling you nothing.

Important: prometheus_client keeps one global REGISTRY for the whole test process, so counters carry
over between tests and absolute values are meaningless here. Every test measures a *delta* around
the action instead.
"""

import json
import re
from pathlib import Path

from prometheus_client import REGISTRY

from tests.conftest import SAMPLE_TEXT, make_tenant, summarize

DASHBOARD = Path(__file__).resolve().parent.parent / "ops/grafana/dashboards/ledgerllm.json"

# Metrics the dashboard is ready for but another owner still has to emit. Remove as they land.
# ledgerllm_cache_total landed with Suraj's response cache (PR #4), so nothing is pending now.
PENDING_METRICS: set[str] = set()


def value(name: str, **labels) -> float:
    """Current value of one labelled sample, 0.0 when the label combination has not been used yet."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_cost_and_token_counters_move_after_a_request(client, api_key):
    tenant = client.get("/v1/usage", headers={"X-API-Key": api_key}).json()["tenant_id"]
    labels = {"tenant": tenant, "model": "gemini-3.8-flash", "purpose": "completion"}

    before_cost = value("ledgerllm_cost_microusd_total", **labels)
    before_in = value("ledgerllm_tokens_total", **labels, direction="input")

    body = summarize(client, api_key).json()

    booked = value("ledgerllm_cost_microusd_total", **labels) - before_cost
    assert booked == body["usage"]["cost_usd"] * 1_000_000  # metric agrees with the billed cost
    assert (
        value("ledgerllm_tokens_total", **labels, direction="input") - before_in
        == (body["usage"]["input_tokens"])
    )


def test_llm_latency_histogram_records_the_call(client, api_key):
    before = value("ledgerllm_llm_latency_seconds_count", model="gemini-3.8-flash")
    summarize(client, api_key)
    assert value("ledgerllm_llm_latency_seconds_count", model="gemini-3.8-flash") - before == 1


def test_feedback_counter_is_labelled_by_prompt_version(client, api_key):
    body = summarize(client, api_key).json()
    prompt_version = body["usage"]["prompt_version"]  # e.g. summarize_v1@ec6822c047b1

    before = value("ledgerllm_feedback_total", rating="up", prompt_version=prompt_version)
    r = client.post(
        "/v1/feedback",
        json={"request_id": body["request_id"], "rating": "up"},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 201, r.text

    after = value("ledgerllm_feedback_total", rating="up", prompt_version=prompt_version)
    assert after - before == 1  # the rating is attributed to the prompt that produced the summary


def test_rejection_counter_moves_on_a_refused_request(client):
    key = make_tenant(client, plan="free")["api_key"]
    before = value("ledgerllm_rejections_total", reason="model_not_allowed")
    assert summarize(client, key, model="claude-opus-5-5").status_code == 403
    assert value("ledgerllm_rejections_total", reason="model_not_allowed") - before == 1


def test_upstream_error_counter_moves_on_provider_failure(client, api_key):
    labels = {"model": "gemini-3.8-flash", "retryable": "true"}
    before = value("ledgerllm_upstream_errors_total", **labels)
    assert summarize(client, api_key, text=SAMPLE_TEXT + " [[MOCK_FAIL]]").status_code == 502
    assert value("ledgerllm_upstream_errors_total", **labels) - before == 1


def dashboard_metric_names() -> set[str]:
    """Every metric name the committed Grafana dashboard queries, read out of its PromQL."""
    dashboard = json.loads((DASHBOARD).read_text())
    exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
    return {m for e in exprs for m in re.findall(r"\b(?:ledgerllm|http)_[a-z0-9_]+", e)}


def test_dashboard_queries_only_metrics_we_actually_expose(client, api_key):
    """A panel whose metric was renamed shows an empty graph, not an error. This test is the alarm.

    Read the panel PromQL rather than a hardcoded list, so adding a panel automatically extends the
    check. Declared-but-unused counters still appear as `# HELP` lines, and the one request below
    gives the histograms an observation so their `_bucket` series exist.
    """
    summarize(client, api_key)
    text = client.get("/metrics").text
    missing = {n for n in dashboard_metric_names() - PENDING_METRICS if n not in text}
    assert not missing, f"dashboard panels query metrics that /metrics does not expose: {missing}"


def _http_request_lines(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e.get("event") == "http_request"]


def test_log_line_carries_tenant_and_error_code(client, api_key):
    """A refusal must be traceable to a customer from the logs alone, without joining the ledger."""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        assert summarize(client, api_key).status_code == 200
    ok = _http_request_lines(logs)[-1]
    assert ok["tenant_id"] and ok["error_code"] is None

    with capture_logs() as logs:
        assert summarize(client, api_key, model="claude-opus-5-5").status_code == 403
    refused = _http_request_lines(logs)[-1]
    assert refused["error_code"] == "model_not_allowed"
    assert refused["tenant_id"] == ok["tenant_id"]  # same customer, now attributable


def test_log_line_has_no_tenant_when_auth_fails(client):
    """No tenant was resolved, so the field is None rather than a stale or guessed value."""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        r = client.post("/v1/summarize", json={"text": "x"}, headers={"X-API-Key": "llk_bogus"})
    assert r.status_code == 401
    line = _http_request_lines(logs)[-1]
    assert line["tenant_id"] is None and line["error_code"] == "invalid_api_key"
