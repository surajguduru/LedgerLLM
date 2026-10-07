"""Prometheus metrics. OWNER: Yashraj.

These tests assert the counters actually move, because a dashboard built on a metric that never
increments looks healthy while telling you nothing.

Important: prometheus_client keeps one global REGISTRY for the whole test process, so counters carry
over between tests and absolute values are meaningless here. Every test measures a *delta* around
the action instead.
"""

from prometheus_client import REGISTRY

from tests.conftest import SAMPLE_TEXT, make_tenant, summarize


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


def test_scrape_exposes_every_family_the_dashboard_queries(client, api_key):
    """The Grafana panels in ops/grafana/dashboards/ledgerllm.json query exactly these names."""
    summarize(client, api_key)
    text = client.get("/metrics").text
    for family in (
        "ledgerllm_cost_microusd_total",
        "ledgerllm_tokens_total",
        "ledgerllm_rejections_total",
        "ledgerllm_llm_latency_seconds",
        "ledgerllm_guardrail_verdicts_total",
        "ledgerllm_feedback_total",
        "ledgerllm_upstream_errors_total",
        "http_request_duration_seconds",  # from the instrumentator, used for p50/p99 panels
    ):
        assert family in text, f"{family} missing from /metrics"
