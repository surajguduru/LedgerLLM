# Implementation notes — Yashraj (observability, dashboards, load evidence)

Running log of what I built, why, and what I expect to be asked in the viva. Brief: `docs/briefs/06-yashraj.md`.

Branch: `feat/observability-dashboards-metrics`

| Task | What | Status |
|---|---|---|
| E | Feedback metric labelled by prompt version + `tests/test_metrics.py` | ✅ done |
| A | Grafana dashboard JSON + screenshot | ⬜ next |
| C | Burst load test on Postgres → `verify_quota.py` HOLDS | ⬜ |
| D | Real-provider latency sample | ⬜ |
| B | Tenant `/dashboard` page | ⬜ (partly blocked on Naresh's `by_day`) |

---

## Task E — Feedback per prompt version, and a test that the metrics move

### What I changed
1. `app/observability/metrics.py` — `ledgerllm_feedback_total` gained a second label:
   `["rating"]` → `["rating", "prompt_version"]`.
2. `app/api/feedback.py` — new `_prompt_version()` helper that resolves the prompt version for a
   rating, and passes it as the label.
3. `tests/test_metrics.py` — new file, 6 tests.

### Why this matters (the point of the task)
Thumbs up/down is our **online** quality signal — the only quality measurement that comes from real
users rather than from an offline eval set. But a raw up/down ratio is useless on its own: if the
ratio drops from 80% to 60%, you cannot tell *why*.

Adding `prompt_version` turns the counter into an **A/B comparison**. Because prompts are versioned
YAML (`prompts/summarize_v1.yaml`, decision D11) and switched with an env var
(`SUMMARIZE_PROMPT_VERSION`), we can ship `summarize_v2`, watch the two series side by side in
Grafana, and roll back by flipping the env var if real users like v2 less. That closes the loop
between offline evals (Sai's golden set) and production.

### How the lookup works, and the one subtlety
A rating arrives with only a `request_id`. I need the prompt version that produced that summary.

- `request_logs` does **not** have a `prompt_version` column.
- `usage_ledger` **does** — it is recorded on every billable call.

So the helper does one query: ledger row for this `request_id`, for this tenant, with
`purpose == "completion"`.

**The `purpose == "completion"` filter is the subtlety.** One request can write several ledger rows:
the completion, plus one per guardrail classifier call, plus a judge call if sampled. Only the
completion row wrote the text the user is rating. Without the filter the query is ambiguous and could
label the feedback with a guardrail call's prompt version.

The `tenant_id` filter is a tenant-isolation guard: it is redundant today because the endpoint
already checks ownership against `request_logs` first, but it means the query cannot leak across
tenants even if that check is ever refactored away.

Falls back to the string `"unknown"` if no completion row exists (e.g. a cache hit, which is billed
at zero cost and writes no ledger row) — never `None`, because Prometheus labels must be strings.

### Why the tests measure deltas, not absolute values
`prometheus_client` keeps **one global `REGISTRY` per process**. Counters are never reset between
tests, so by the time my test runs, other tests have already incremented the same counters. Asserting
`counter == 1` passes alone and fails in the full suite.

Every test therefore reads the counter, performs the action, reads again, and asserts on the
difference. The `value()` helper returns `0.0` instead of `None` for a label combination that has not
been used yet, so the first read never crashes.

### What the 6 tests cover
| Test | Asserts |
|---|---|
| `test_cost_and_token_counters_move_after_a_request` | cost metric delta **equals the billed cost** in the response, and token delta equals reported tokens — the metric and the bill cannot silently disagree |
| `test_llm_latency_histogram_records_the_call` | histogram `_count` goes up by exactly 1 |
| `test_feedback_counter_is_labelled_by_prompt_version` | after a thumbs-up, the counter for *that* `prompt_version` moves by 1 |
| `test_rejection_counter_moves_on_a_refused_request` | `rejections_total{reason="model_not_allowed"}` moves on a 403 |
| `test_upstream_error_counter_moves_on_provider_failure` | `upstream_errors_total{retryable="true"}` moves on a 502 (triggered with `[[MOCK_FAIL]]`) |
| `test_scrape_exposes_every_family_the_dashboard_queries` | every metric name my Grafana panels query is actually present in `/metrics` |

That last test is the one I would point to if asked "how do you stop the dashboard silently
breaking?" — it fails the build if someone renames a metric my panels depend on.

### Result
`make lint` clean · `make test` → **45 passed, 16 xfailed** (was 39 passed before my 6).

---

## Likely viva questions on Task E

**Q: Why not just store `prompt_version` on `request_logs` and avoid the join?**
It would duplicate data that the ledger already owns, and `request_logs` is Loukik's package — adding
a column there is a cross-package change. The ledger is the designed source of truth for "what
produced this call" (it already carries `price_version` and `prompt_version` for billing
reproducibility), so reading it is the correct direction.

**Q: Isn't a second label a cardinality risk?**
Low. `prompt_version` only changes when we deliberately ship a new prompt — a handful of values over
the project's life, times 2 rating values. The real cardinality risk in this system is the `tenant`
label on the cost/token metrics, which grows with every customer; `DESIGN.md §5` already flags that as
a 10× scaling limit, with the fix being to drop the `tenant` label and aggregate per-tenant from the
ledger instead. I report measured cardinality (`curl /metrics | wc -l`) as one of my numbers.

**Q: Why is feedback an "online" signal and the golden set "offline"?**
Offline = a fixed set of documents with known answers, scored in CI before deploy; catches
regressions but cannot see real traffic. Online = signals from live production traffic (feedback
ratings, and Thrishal's sampled LLM judge); catches the things the eval set never thought of. You need
both — offline gates the deploy, online tells you the deploy was actually fine.

**Q: What happens if a user rates a cache hit?**
A cache hit is served for free and writes no ledger row, so the lookup finds nothing and the label
becomes `"unknown"`. Acceptable: the summary text came from an earlier request whose rating was
already attributed. Worth mentioning as a known edge once Suraj's cache lands.
