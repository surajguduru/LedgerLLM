# Brief 06 — Dashboards, metrics, load evidence — **Yashraj**

## Mission
You own both dashboards (Grafana for operators, the `/dashboard` page for tenants), the Prometheus metrics, the feedback
signal, and the **Locust burst test that proves quota enforcement holds**. You consolidate everyone's numbers into the README
and close the talk with "what breaks at 10×".

## Files you own
`app/observability/{logging,metrics}.py`, `app/api/dashboard.py`, `app/api/feedback.py`, `ops/**`, `loadtest/**`,
new `tests/test_metrics.py`.

## Must-have tasks
### A. Grafana dashboard (JSON committed + screenshot)
`make up` → Grafana :3000 (admin/admin) → build → Share → Export → `ops/grafana/dashboards/ledgerllm.json`. Panels / PromQL:
requests/s by status `sum(rate(http_requests_total{handler="/v1/summarize"}[1m])) by (status)`; p50/p99
`histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{handler="/v1/summarize"}[5m])) by (le))`; model latency
`histogram_quantile(0.5, sum(rate(ledgerllm_llm_latency_seconds_bucket[5m])) by (le, model))`; cost/min by tenant
`sum(rate(ledgerllm_cost_microusd_total[5m])) by (tenant) * 60 / 1e6`; tokens by direction; rejections by reason
`sum(increase(ledgerllm_rejections_total[1h])) by (reason)`; guardrail verdicts by stage/category; feedback ratio; upstream errors;
cache hit rate (`ledgerllm_cache_total`, Suraj adds it). Label rows by monitoring category.
### B. Tenant dashboard (`/dashboard`)
Dependency-free (Chart.js CDN ok): budget gauge (spent / reserved / remaining), cost-by-day bars (Naresh's `by_day`), by-model and
by-purpose tables, last 10 requests, warning banner, statement download. Screenshot for README.
### C. Burst load test
`docker compose up -d db`; app with `LLM_PROVIDER=mock MOCK_LATENCY_MS=800` against Postgres; `make seed`; `make loadtest`;
`python loadtest/verify_quota.py` → `HOLDS`. Repeat against the Render URL after warming it. Write `loadtest/README.md` with
commands and the result table (p50/p95/p99 steady and burst, req/s, 200/402/429 counts, verify output).
### D. Real-provider latency sample
`loadtest/latency_sample.py`: 30 sequential requests with `LLM_PROVIDER=gemini`, three document sizes; p50/p99 end-to-end and
model-only (`usage.latency_ms`). Respect the free tier's RPM (sleep between calls).
### E. Feedback per prompt version + metrics test
Add `prompt_version` label to `ledgerllm_feedback_total` (look it up from `request_logs` → ledger row); `tests/test_metrics.py`
asserts counters move after a request and after feedback.

## Good to have
- Langfuse tracing (`app/observability/tracing.py`, no-op unless keys set; 4 spans in the pipeline).
- Prometheus alert rules (402 spike, p99 > 8 s, upstream error rate > 5 %).

## Numbers to produce (and consolidate everyone's into README on Wed 8 Oct)
p50/p95/p99 (mock steady), platform overhead p99 (`MOCK_LATENCY_MS=0`), req/s, burst 200/402/429, `verify_quota`, real-provider
p50/p99 by size, metric cardinality (`curl /metrics | wc -l`).

## What to write
- README: Benchmarks table filled from `docs/MEASUREMENTS.md`; dashboard screenshots; link `loadtest/README.md`.
- Slides 9–10 (2.5 min): dashboard by category; burst chart + `verify_quota` line; mock-vs-real latency (D8); 10× limits (DESIGN §5);
  next steps; live URL. Trade-off: per-tenant labels vs ledger aggregation. Failure mode: naive check-then-call at 800 ms model latency
  would overspend ~50×; the atomic reserve holds.

## Pitfalls
- Locust must mark 402/429 as expected (already done in the locustfile — keep it).
- Warm Render before measuring; report cold start separately.
- Don't run the burst with the real provider: it burns the free-tier quota and proves nothing about *our* code.

## Agent kickoff prompt
> Read `CONTRIBUTING.md`, `docs/DESIGN.md`, `docs/briefs/README.md` and this brief, then
> `app/observability/metrics.py`, `app/api/dashboard.py`, `loadtest/locustfile.py`. Add the `prompt_version` label to the feedback
> metric with `tests/test_metrics.py`, build the tenant dashboard with Chart.js panels from `/v1/usage`, write
> `loadtest/latency_sample.py` and `loadtest/README.md`, and help me assemble the Grafana dashboard JSON from the PromQL above.
> Run `make lint test`. Do not edit other packages.
