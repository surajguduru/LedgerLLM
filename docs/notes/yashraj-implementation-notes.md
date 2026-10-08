# Implementation notes — Yashraj (observability, dashboards, load evidence)

Running log of what I built, why, and what I expect to be asked in the viva. Brief: `docs/briefs/06-yashraj.md`.

Branch: `feat/observability-dashboards-metrics`

| Task | What | Status |
|---|---|---|
| E | Feedback metric labelled by prompt version + `tests/test_metrics.py` | ✅ done |
| A | Grafana dashboard JSON (+ screenshot) | ✅ JSON done · screenshot needs Docker |
| C | Burst load test → `verify_quota.py` HOLDS | ✅ HOLDS on SQLite · Postgres re-run needs Docker |
| D | Real-provider latency sample | ✅ script done · real numbers need a Gemini key |
| B | Tenant `/dashboard` page | ✅ done · 3 panels auto-enable when Naresh ships his fields |

## Headline numbers measured so far (7 Oct)

| Number | Value | Target | Verdict |
|---|---|---|---|
| Platform overhead, uncontended | p50 **6–8 ms**, worst of 60 requests **11 ms** | p99 ≤ 25 ms | ✅ |
| Throughput, 50 users | **270 req/s** | ≥ 50 req/s | ✅ |
| Burst quota enforcement | **HOLDS** — $0.019264 spent of $0.020000, reserved back to 0 | never exceed limit | ✅ |
| Burst refusals | 4,022 × 402 of 4,125 requests, 0 × 429 (limiter still a stub) | — | — |
| Reservation pessimism | **3.7%** of budget left unspent | — | measured cost of D2 |

Full write-up with commands: `loadtest/README.md`. Filed in `docs/MEASUREMENTS.md` for the team.

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
| `test_dashboard_queries_only_metrics_we_actually_expose` | every metric name my Grafana panels query is actually present in `/metrics` (names are read out of the dashboard JSON — see Task A) |

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

---

## Task A — Grafana dashboard, and a histogram bug I had to fix first

### The bug I found (good viva material)
Before building panels I checked what the app actually exposes. The per-handler latency histogram
`http_request_duration_seconds` had only **three buckets: 0.1 s, 0.5 s, 1 s**, the library default
from `prometheus-fastapi-instrumentator`.

That cannot express either of our latency targets:
- platform overhead p99 ≤ **25 ms** — entirely inside the first bucket, so the answer is always
  "somewhere between 0 and 100 ms". Unmeasurable.
- end-to-end p99 ≤ **8 s** — above the last real bucket, so everything lands in `+Inf` and the
  quantile is unbounded.

**Why buckets decide what you can measure.** A Prometheus histogram does not store individual
timings. It stores counters: "how many requests were ≤ 0.1 s", "≤ 0.5 s", and so on.
`histogram_quantile` then *interpolates linearly inside whichever bucket the quantile falls into*. So
the precision of any percentile is bounded by the bucket edges around it — and a percentile inside a
bucket that spans 0 → 100 ms is a guess, not a measurement.

I widened it to 14 buckets, 5 ms → 21 s, and deliberately put **both SLO thresholds on bucket edges**
(`0.025` and `8`). When a target is an edge, the quantile at that target is read off a real counter
instead of interpolated, so "p99 ≤ 8 s" is a fact rather than an estimate.

### The second, subtler bug
My first attempt passed the buckets via `.add(metrics.default(latency_lowr_buckets=...))`. All HTTP
metrics then silently went to **zero** — no error, no warning, just a dead dashboard.

Cause: `app/main.py` creates an app at import time, and tests/scripts call `create_app()` again, so
`setup_metrics()` runs twice in one process. The library's `metrics.default()` tries to register
metric names that already exist, catches the duplicate-registration error, and **returns `None`**.
`Instrumentator.add()` then skips the `None`, leaving the instrumentation list empty — so nothing was
recorded at all.

Fix: pass buckets to `.instrument(app, latency_lowr_buckets=...)` instead. The library builds that
instrumentation inside its middleware and handles the repeat case itself.

**Lesson worth saying out loud:** a metrics pipeline fails *silently*. Broken business logic throws;
a broken counter just reads zero, which looks exactly like "no traffic". That is why Task E's tests
assert counters **move** rather than merely that `/metrics` responds.

### The dashboard
`ops/grafana/dashboards/ledgerllm.json` — 12 panels in 4 rows, auto-provisioned by docker-compose
(`ops/grafana/provisioning/`). Rows are named after the monitoring categories in `DESIGN.md §4`, so
the dashboard's structure *is* the monitoring plan rather than a random pile of graphs:

| Row | Panels |
|---|---|
| **Operational** — up and fast? | requests/s by status · end-to-end p50/p99 · model latency p50/p99 by model · upstream errors by retryability |
| **Cost** — who spends what | spend/min by tenant · spend/min by purpose (stacked) · cache hit rate |
| | tokens/s by direction |
| **Input** — what arrives, what we refuse | rejections in 1 h by reason · guardrail verdicts by stage/category/blocked |
| **Quality** — are summaries good | feedback 24 h by prompt version · thumbs-up share |

Two deliberate choices:
- **`by purpose` is stacked.** It separates `completion` (what the tenant asked for) from `guardrail`
  (the safety classifier's tokens, billed to the tenant per D6) and `judge` (quality sampling, *not*
  billed, D17). One glance answers "what share of the bill is safety overhead?"
- **Every panel description names the design decision it is evidence for.** Hovering a panel in the
  demo explains why it exists. Costs nothing and makes the dashboard self-documenting.

Also pinned `uid: prometheus` in the datasource provisioning, so the committed JSON binds to the
datasource deterministically instead of depending on a uid Grafana generates at first boot.

### The test that stops the dashboard rotting
`test_dashboard_queries_only_metrics_we_actually_expose` reads the committed dashboard JSON, regexes
every metric name out of the panel PromQL, and asserts each one appears in `/metrics`.

The point: if someone renames a metric, Grafana does not error — it draws an **empty panel**, which
looks like "no traffic" and can go unnoticed for weeks. This test turns that silent failure into a
failed CI run. Adding a panel extends the check automatically because the list is derived, not
hardcoded. `ledgerllm_cache_total` is in a small `PENDING_METRICS` allowlist until Suraj's cache lands.

I verified the alarm works by renaming a metric in the JSON, watching the test fail, and restoring it.

---

## Task D — Real-provider latency sample

`loadtest/latency_sample.py`. Sequential, paced requests at three document sizes, reporting
end-to-end vs model-only latency and the gap between them.

### Why this exists as a separate script (decision D8)
The burst test runs on the **mock** provider. That looks like cheating but is the right call: the
claim being tested is that *our* quota accounting is correct, and the mock makes the test free,
deterministic and repeatable. Hammering Gemini would measure Google's capacity, burn the free-tier
quota, and tell us nothing about our code.

But "our code is fast" is not the same as "the product is fast", so real latency still needs
measuring — on a small, paced sample rather than a burst. **Two numbers instead of one is the
accepted cost of D8**, and being able to explain why is the point.

### Design choices
- **Three sizes: 1.2k / 3.2k / 6.2k input tokens.** The target in `DESIGN.md` is written for a
  ~3k-token document, so the sizes bracket it — one below, one at it, one above. Latency vs document
  size is then a line, not a single point.
- **End-to-end *and* model-only.** End-to-end is wall clock around the HTTP call (what a client
  feels); model-only is `usage.latency_ms` from the response (time inside the provider call). The
  **difference is our platform overhead**, measured on the same request rather than inferred from two
  different runs.
- **Nearest-rank percentiles, no interpolation.** Every number printed is an actually-observed
  request. With n=10 a "p99" is really just the slowest request, and the script prints that caveat
  itself rather than letting me quote a number that sounds more rigorous than it is.
- **Paced with `--sleep 4.5`.** The Gemini free tier allows ~15 requests/min. A provider 429 arrives
  as a 502 `upstream_error`; the script records it as a failure instead of retrying, so a throttled
  run shows up as missing samples rather than as fake good latency.

---

## Task C — Burst test: quota enforcement HOLDS

The locustfile and `verify_quota.py` already existed; what was missing was *running* them and
reporting it. Full write-up in `loadtest/README.md`.

### The headline result
20 users, 20 s, against the `burst` tenant whose seeded monthly budget is **$0.02**:

```
4,125 requests  ->  103 x 200,  4,022 x 402,  0 x 429
ledger: spent $0.019264 of $0.020000 limit,  reserved back to $0.000000
quota enforcement: HOLDS
```

**What to say about it in the presentation.** 4,022 requests were refused *before* the model call, so
they cost nothing — that is pipeline ordering D6 working (cheap checks first). Spend stopped **under**
the limit, never over. And `reserved_usd` returning to exactly **0** is the half people forget: it
proves no reservation leaked, so a refusal or crash mid-flight does not permanently eat budget a
tenant never spent.

### The contrast that makes the point
The failure mode this defends against: naive *check-then-call*. At an 800 ms model latency, 50
concurrent requests all read the same stale `spent`, all pass the check, and all call the model —
overspending by roughly the number of concurrent requests (~50×). The atomic reserve closes that
window because admission and accounting are the **same** database statement, so there is no gap
between deciding and recording.

### Two honest caveats I will state rather than hide
1. **SQLite serialises writes.** It takes a database-wide write lock, so concurrent requests queue
   instead of racing. The budget looks safe for the *wrong reason* — the serialisation hides the race
   rather than the atomic reserve winning it. **The Postgres run is the actual proof** and is still
   pending (needs Docker). I would rather present this clearly than claim the SQLite run proves
   concurrency.
2. **0 × 429** because `app/traffic/ratelimit.py` is still a pass-through stub (Suraj's task). Needs
   a re-run once that lands.

### An unexpected number worth presenting: reservation pessimism
$0.000736 of the $0.02 budget was left unspent — **3.7%**. Cause: the reserve books the *worst case*
(a full `max_tokens` of output) before the call, and a shorter summary settles for less. So a tenant
near their limit can be refused a request that would actually have fit.

That is exactly the trade-off `DESIGN.md` lists as the accepted cost of D2 — and now it is a measured
number instead of a hand-wave. Good answer to "what does your design cost you?"

### Throughput, and a number that looks bad until you read it
50 users, `MOCK_LATENCY_MS=0`: **270 req/s** (target ≥ 50) but **p99 930 ms**.

930 ms versus the 11 ms uncontended worst case is not platform overhead — it is SQLite's write lock
at 50 concurrent writers. This is scaling limit #1 from `DESIGN.md §5` (four writes per request:
ledger, request log, audit, `last_used_at`) showing up in a measurement. Useful for slide 10: the
"what breaks at 10×" claim is not theoretical, I have the graph.

---

## Likely viva questions on A / C / D

**Q: Why not load-test against the real model?** Decision D8. The claim under test is our quota
accounting, not the provider's throughput. The mock makes it free, deterministic and repeatable, and
`MOCK_LATENCY_MS` lets me simulate a slow model — which is the *hostile* case for budget correctness,
because a long call widens the window during which a naive check-then-call would overspend. Real
latency is measured separately and reported as its own number.

**Q: Your p99 is 930 ms but you claim 25 ms overhead. Which is it?** Both, under different
conditions, and the gap is the finding. 6–11 ms is per-request work with no contention. 930 ms is 50
concurrent writers queueing on a SQLite database-wide write lock. Same code, different bottleneck —
and the bottleneck is the datastore, which is why the real run belongs on Postgres.

**Q: How do you know the metric is telling the truth?** `test_cost_and_token_counters_move_after_a_request`
asserts the cost counter's delta **equals** the `cost_usd` in the API response. The dashboard and the
bill cannot silently disagree.

**Q: Why 14 buckets, not 50?** Every bucket is a separate time series per handler, so buckets
multiply cardinality. 14 is enough to put both SLO thresholds on edges and keep resolution in the
millisecond range where overhead lives and the second range where model calls live. Metric cardinality
is one of the numbers I report (`curl /metrics | wc -l`).

**Q: Why is `reserved_usd == 0` part of the proof?** Because "spent ≤ limit" alone could be satisfied
by a system that reserved budget and never released it — it would look compliant while slowly eating
tenants' money. Reservations returning to zero proves every reserve was matched by a settle or a
release, including on the error paths.

---

## Task B — Tenant dashboard (`/dashboard`)

The operator dashboard (Grafana) answers "is the platform healthy". This one answers a different
question for a different person: **"what am I being charged, and why?"** — the self-serve billing page
a customer opens before they email support.

Served by the API itself rather than as a separate Streamlit app: one deployable, one URL
(decision **D14**). Dependency-free apart from Chart.js off a CDN, and nothing is written to browser
storage, so the pasted key does not outlive the tab.

### What is on it
| Panel | Why it is there |
|---|---|
| Budget gauge (doughnut) | spent / reserved / remaining — **exactly the three numbers the budget check compares against the limit**, so the customer sees the same arithmetic the server does |
| Committed % bar | `(spent + reserved) / limit`, which is what the soft-warning threshold actually fires on |
| Summary cards | requests, tokens in/out, average cost per request |
| Spend by day | where the month went |
| By model | which model they used and what it cost |
| By purpose | **who pays for what** (below) |
| Recent requests | last 10, with 402/429 rows in red so a refusal is visible |
| Soft-warning banner | shown past 80%, and says what happens next |
| CSV statement | download for their own records |

### The two details I would point at in a demo
**1. Reserved is shown, not hidden.** Most billing pages show one "spent" number. Showing
`reserved` separately makes reserve-then-settle (D2) visible to the customer: money is held while a
request is in flight and released when it settles. Without it, a customer watching the page mid-burst
would see "remaining" drop and then rise again and think the meter was broken. The page says
explicitly: *"Reserved is budget held for requests still running. It returns to zero when each one
settles or fails."*

**2. The by-purpose table says who pays for what.** Three purposes, three different billing answers:
- `completion` — the summary the tenant asked for. Billed.
- `guardrail` — the safety classifier's *own* tokens. **Billed to the tenant** (D6: the budget is
  reserved before the guardrail precisely so this spend can be attributed).
- `judge` — our online quality sampling. **Not billed** (D17) — we are the ones who wanted the
  measurement, so we pay for it.

That is a real product decision made legible on the page, and it is the kind of thing a customer
would otherwise dispute.

### Shipping around a dependency instead of waiting for it
Three panels need things Naresh has not shipped yet: `by_day` and `last_requests` on `GET /v1/usage`,
and `GET /v1/usage/statement.csv`.

Rather than block, each panel **feature-detects**: if the field is absent it renders a short note
naming the owner; if present it renders for real. No change to this file when his work lands. The CSV
button probes the route first, so a 404 shows a note instead of a dead button.

This is the general pattern worth stating: **the consumer degrades, the producer does not get
blocked.** It also means my task is finished and reviewable now, instead of sitting half-done waiting
on someone else's PR.

Implementation note: the CSV is fetched and turned into a `Blob` URL rather than linked directly,
because the route authenticates with an `X-API-Key` **header** and a plain `<a href>` cannot send one.

### How I verified it without a browser
No browser automation available, so I checked it two ways:
1. `node --check` on the extracted script — catches syntax errors.
2. Ran the page's own `load()` in Node against a **real `/v1/usage` payload** with a stubbed
   `document`, `fetch`, `Chart`, `Blob` and `URL`. Confirmed the gauge and tables populate, and all
   three pending panels show their note. Then injected the missing fields and confirmed the day
   chart, recent-requests table and download link all render — i.e. the degradation works in **both**
   directions. Also checked the soft-warning banner and the red 402 row, and that the committed
   percentage computed correctly (8.4 + 0.3 of 10.00 → 87.0%).

Honest gap: this proves the logic runs and the markup is produced. It does **not** prove it looks
good. Still need to open it in a browser and take the screenshot for the README.

---

## Likely viva questions on Task B

**Q: Why two dashboards?** Different audiences and different questions. Grafana is for us —
time-series, rates, aggregated across tenants, "is the platform healthy right now". `/dashboard` is
for one customer — their current bill, their requests, "what am I paying for". Putting per-tenant
billing detail in Grafana would also mean a `tenant` label on everything, which is the cardinality
problem `DESIGN.md §5` already flags.

**Q: Is it safe to paste an API key into a web page?** It is the tenant's own key, it is sent only to
our own `/v1/usage` over HTTPS, and it is never persisted — no `localStorage`, no cookie, so closing
the tab discards it. For production the honest answer is that this should be a session behind a real
login; API keys are a machine credential and a human-facing page is the wrong place for one. Scope
says end-user accounts and OAuth are out (`DESIGN.md §1`), so this is the deliberate shortcut.

**Q: Why not server-render it?** Client-side `fetch` means the page reuses the *same* public
`/v1/usage` contract a customer's own integration would call, so the dashboard cannot accidentally
show numbers the API would not. It also keeps the route a static string with no DB access.

---

## 8 Oct — integration status, and a panel bug Suraj's branch exposed

Suraj has pushed six branches (`feat/traffic-ratelimit`, `-idempotency`, `-cache`, `-routing`,
`fix/ci-postgres-tests`, `feat/deploy-prep`). They **stack**, so `feat/deploy-prep` is the tip that
contains all of it. **None are merged to `main` yet** — `main` is still at `f37e226`.

### A panel bug I only found by reading his code
His cache metric emits **three** label values, not two: `metrics.CACHE.labels("hit" | "miss")` on a
lookup and `labels("bypass")` when the cache is never consulted.

My hit-rate panel divided by `sum(rate(ledgerllm_cache_total[15m]))` — **all three**. So every
bypassed request would have dragged the reported hit rate down, and the panel would have shown a
number that was wrong in a direction nobody would question (caches are *supposed* to look mediocre).
Fixed the denominator to `{result=~"hit|miss"}`.

Worth saying in the viva: this is the failure mode my own dashboard-sync test does **not** catch. That
test proves the metric *exists*; it cannot prove the PromQL *means* what I think. Label semantics are
a contract between two people, and the only way to check it was to read the producer's code.

### Merge conflicts, already scouted
I dry-ran the merge and aborted it. Two conflicts, both trivial "both added":
1. `app/observability/metrics.py` — he appends `CACHE` right below the `FEEDBACK` line that I edited
   to add `prompt_version`. Resolution: keep my two-label `FEEDBACK` **and** his `CACHE`.
2. `docs/MEASUREMENTS.md` — we appended to the same sections. Resolution: keep both sets of lines.

I deliberately did **not** merge his branch into mine: his work is not on `main`, so merging would
pull his commits into my PR and entangle two reviews. Correct order is his branches land on `main`
first, then I rebase. 30 seconds of conflict resolution, known in advance.

### What unblocks once his work merges
- **Cache hit-rate panel** starts showing data — his `result` labels match my query.
- `ledgerllm_cache_total` can come out of `PENDING_METRICS` in `tests/test_metrics.py`. It must stay
  until then, because the metric does not exist on `main` and the test would fail.
- **Burst test re-run gets a real 429 count.** His fixed-window limiter replaces the pass-through
  stub, so the 0 × 429 in my current numbers becomes a real figure.
- His own numbers are already filed: rate-limit check p50 747 µs / p99 1,404 µs on Postgres, window
  edge burst measured at exactly the 2.0× rpm bound D3 accepts, 460 MB image, 1.1 s local cold start.

---

## 8 Oct (later) — Suraj's work merged, and it broke my burst test

All six of Suraj's branches merged to `main` via PRs #1–#7 (`main` is now `72a7ab3`): fixed-window
rate limiter, idempotency TTL, response cache, plan-aware routing, Postgres CI, deploy prep. I
rebased onto it — the two conflicts were exactly the ones I had scouted, resolved in about a minute.
Suite went from 45 passed / 16 xfailed to **80 passed / 13 xfailed**.

Then I re-ran my burst test and it no longer worked. This is the most useful thing I did all day.

### The silent failure
```
status code distribution: {200: 120, 429: 3621}      <- not one 402
quota enforcement: HOLDS                              <- vacuously true
```

Yesterday's headline result — 4,022 × 402, quota HOLDS — **was no longer reproducible**, and the
script still said HOLDS. Two stages that landed on `main` sit in front of the budget:

1. **The rate limiter is per KEY; the budget is per TENANT.** Everything past `plan.rpm` is refused
   with 429 at stage 3, before the budget is consulted at stage 5. The limiter *shields* the budget,
   so the cutoff never fires. That is correct system behaviour — and it makes a single-key burst
   worthless as a budget test.
2. **The response cache returns before the budget reserve.** Stage 4.5 is an exact match on the
   request body and returns at zero cost. My locustfile sent the *same* document every request, so
   almost everything was a free cache hit and the budget was never spent down.

`verify_quota.py` passed because spend was under the limit. It was under the limit because nothing
had been spent. **A green check that means nothing is worse than a red one** — this is the single
best point I have for the presentation.

### The redesign
The insight that fixes it: **rate limits are per key, budgets are per tenant.** So to put genuine
concurrent pressure on one budget:

- **one key per simulated user, all on one tenant** — N keys give N× admitted throughput against one
  shared budget;
- **each user paced below its own key's rpm** (enterprise 600 rpm = 10 req/s; users run ~5 req/s) so
  the limiter stays out of the way of what is being measured;
- **a unique document per request** so the cache cannot serve it free.

The locustfile now provisions that tenant itself at test start via the admin API (one key per user)
and writes `loadtest/burst_tenant.json` for `verify_quota.py`. Two small user classes still hit the
cache (`SteadyUser`, repeated document) and the limiter (`RateLimitUser`, free plan at rpm 5) on
purpose, so those paths still show up on the dashboard.

New result — all three refusal paths exercised:
```
6,621 requests at 228 req/s -> 200: 92,  402: 4,278,  429: 2,426
spent $0.018765 of $0.020000,  reserved back to 0,  402 refusals 4,278
quota enforcement: HOLDS
```

### A second bug: my own verification was wrong
My first attempt at "was the cutoff actually exercised?" tested `spent >= 95% of limit`. It reported
**INCONCLUSIVE** on a run with 4,278 refusals, because spend stopped at 93.8%.

The reason is the thing I had already measured: reserve-then-settle books worst-case cost and settles
for less, so spend *deliberately* stops short of the limit. A spend threshold cannot tell
"stopped short because of reservation pessimism" apart from "never reached the limit at all".

Fixed by keying off the **402 count** — the direct evidence — which the locustfile now hands over.
`verify_quota.py` exits 2 with **INCONCLUSIVE** if a run produced no 402s, so this class of silent
pass cannot recur.

### Numbers that moved
| | 7 Oct | 8 Oct (main @ 72a7ab3) |
|---|---|---|
| Platform overhead p50 (1.2k/3.2k/6.2k tok) | 6/7/8 ms | **7/8/10 ms** |
| Throughput | 270 req/s | 228 req/s |
| 402 count | 4,022 | 4,278 |
| 429 count | 0 (limiter was a stub) | **2,426** |
| Reservation pessimism | 3.7% | **6.2%** (1.8× avg request) |

Overhead rose ~2 ms because the pipeline gained a rate-limiter upsert and a cache lookup — a real
cost of their features, visible in my measurement. Also found that the **first request after process
start** is 12–47 ms (lazy imports, cold pool); excluded from the percentile and reported separately
rather than folded in.

`latency_sample.py` had the same cache bug and got the same fix (unique document per request),
otherwise every request after the first would have measured the cache instead of the model.

### Housekeeping
`ledgerllm_cache_total` came out of `PENDING_METRICS` in `tests/test_metrics.py` — the metric exists
on `main` now, so the dashboard-sync test covers it for real. Also fixed my hit-rate panel
denominator to `{result=~"hit|miss"}` after reading his code: the metric also emits `result=bypass`,
and counting those would have understated the hit rate.

---

## Likely viva questions on the 8 Oct integration

**Q: Your test passed and you changed it anyway. Why?** Because it passed for the wrong reason. The
assertion was "spend never exceeded the limit", and that is trivially true if nothing was ever spent.
Once the limiter and cache landed, 4,000 requests were being refused at stage 3 or served free at
stage 4.5, so the budget code under test was barely executing. The fix was to make the budget the
binding constraint again and to add an INCONCLUSIVE verdict so the failure announces itself.

**Q: Isn't provisioning 50 keys artificial?** It is the realistic shape, not the artificial one. A
real customer on an enterprise plan has many keys — one per service, per environment, per region —
all drawing on one account budget. That is precisely the case where the atomic reserve has to be
correct, and a single-key test never reaches it.

**Q: Why not just raise the rate limit for the test?** Changing the limit to make a test pass would
be testing a configuration we do not ship. Many keys at a realistic per-key rate reproduces real
concurrency without weakening the limiter.

**Q: Why is cache hit rate low in the burst numbers?** Because the burst deliberately defeats the
cache with unique documents — it has to, or the budget is never exercised. Hit rate should be read
from steady traffic. Worth stating when showing the dashboard so the panel is not misread.

---

## 8 Oct (evening) — the Postgres proof, alerts, and the stack verified end to end

Docker came up, so the two blocked items are done. Tasks A and C are now complete except for the
screenshots, which need a browser.

### The headline result, done properly
**Postgres 16, 50 users on 50 keys of one tenant, 60 s, `MOCK_LATENCY_MS=800`** (the latency the
brief specifies, which I had been running at 120):

```
~13,000 requests at 218 req/s  ->  200: 157,  402: 8,650,  429: 4,195
spent $0.018765 of $0.020000,  reserved back to $0,  402 refusals 8,650
quota enforcement: HOLDS           measured overspend: $0.000000
```

**This supersedes the SQLite run**, which held for the wrong reason. SQLite takes a database-wide
write lock, so concurrent requests queue instead of racing — the serialisation hid the race rather
than the atomic reserve winning it. Postgres runs the admission `UPDATE … WHERE spent + reserved +
est <= limit` under real row-level concurrency, and 50 keys on one tenant means 50 clients genuinely
racing one budget.

**Why 800 ms is the number to quote.** It is the hostile case, not merely a slow one. The naive
alternative — check `spent < limit`, *then* call the model — leaves a window as wide as the model
call in which every concurrent request reads the same stale `spent` and all of them pass. At 800 ms
with 50 in flight, ~50 requests get admitted where ~1 fits: about **$0.035 of overspend against a
$0.020 limit, ~2.7× the entire budget in one wave**, repeating every wave until a settle lands. The
atomic reserve closes the window because admission and accounting are the *same* statement.

### The side result I did not expect to be this clean
| | SQLite | Postgres 16 |
|---|---|---|
| Throughput | 228 req/s | 218 req/s |
| **p99** | **670 ms** | **140 ms** |

**4.8× better at p99 on identical code.** That gap is the proof that the SQLite p99 was its write
lock and not our platform overhead — and it turns `DESIGN.md` §5 limit #1 (four writes per request)
from a prediction into a measurement. Good slide-10 material: the "what breaks at 10×" claim is not
theoretical, I changed one variable and watched the predicted bottleneck appear and disappear.

### Prometheus alert rules (`ops/alerts.yml`) — the good-to-have, done
Three rules, each phrased against a target in `DESIGN.md` §1 rather than a round number:

| Alert | Expression shape | `for:` |
|---|---|---|
| `BudgetRefusalSpike` | sustained 402 **rate** > 1/s | 10m |
| `EndToEndLatencyP99AboveTarget` | `histogram_quantile(0.99, …) > 8` | 10m |
| `UpstreamErrorRateHigh` | upstream errors / total requests > 5% | 5m |

Three deliberate choices worth defending:
- **The upstream alert is a ratio, not a count.** 5 errors/s is an incident at 20 req/s and noise at
  2,000 req/s. `clamp_min` on the denominator stops it dividing by zero when there is no traffic.
- **Every rule has a `for:` window.** One slow request or one provider blip is not an incident; the
  window is what separates a signal from a twitch.
- **402s alert on a sustained rate, never on a single event.** A tenant hitting their cap is the
  system working as designed — the alert fires only when it stays high, which means a client stuck
  retrying a 402 or a misconfigured budget.

Each annotation also says what the alert does *not* cover, and names the dashboard row to open.

### Verified the whole stack, not just the files
`make up` → app + Postgres + Prometheus + Grafana. Then, rather than assume:

1. **Prometheus is scraping** — target `ledgerllm` health `up`.
2. **Alert rules loaded** — all three `health=ok`, `state=inactive`. (First attempt showed *no* rules:
   the container was still running from an earlier `make up` that predated the mount, so I had to
   `--force-recreate` it. Worth remembering — a provisioning change needs a container recreate, not
   a restart.)
3. **Grafana provisioned the dashboard** — `uid=ledgerllm`, 16 panels, in the LedgerLLM folder.
   (The second "LedgerLLM" in the search API is the *folder*, type `dash-folder`, not a duplicate.)
4. **Every panel returns live data** — I generated mixed traffic (successes on two tenants, cache
   hits, `[[MOCK_FAIL]]` upstream errors, a 403, guardrail blocks, 429 floods, feedback) and executed
   **all 14 panel queries against the live Prometheus API**. Result: *0 panels without data.*

The single most convincing check: the **thumbs-up share panel read 50.09%**, and my traffic generator
alternates up/down. That is the entire chain verified end to end — feedback endpoint → ledger lookup
→ `prompt_version` label → Prometheus scrape → panel arithmetic.

A lesson on reading an idle dashboard: on the first pass every panel showed `0` and the two histogram
panels showed `NaN`. Neither was a bug — `rate()` over a window with no traffic is legitimately 0,
and `histogram_quantile` of all-zero rates is `NaN`. I only trusted the panels once traffic was
running *concurrently* with the query. Worth knowing before the demo: **open the dashboard with
traffic flowing, or it looks broken.**

### Cardinality, measured properly instead of guessed
I had recorded "85 series". On the live stack it is **171 series** — but the useful number is finer:

- only **18** series carry a `tenant` label, across 3 tenants → **6 series per tenant**
  (tokens: tenant × model × 2 directions × purposes; cost: tenant × model × purposes)
- extrapolated: 1,000 tenants × 3 models ≈ **27,000 series** from this app alone → exactly why
  `DESIGN.md` §5 limit #4 says drop the `tenant` label and aggregate from `usage_ledger`
- the 85 `http_*` series are mostly **my own** doing: widening the latency histogram from 3 buckets
  to 14 costs ~11 extra series per handler/method pair

That last point is the honest version of my own design decision: **cardinality is the price of
resolution.** I bought the ability to measure a 25 ms target and an 8 s target, and I paid ~11 series
per endpoint for it. Being able to state both halves is better than quoting only the benefit.

---

## Likely viva questions on the Postgres run

**Q: Why does the SQLite run appear in your write-up at all, if Postgres is the real proof?**
Because the contrast *is* a result. Same code, p99 670 ms vs 140 ms — that is how I know the SQLite
number was the datastore and not our code, and it is a live demonstration of the first scaling limit
the design document predicts.

**Q: Your aggregate p50 is 55 ms but the model takes 800 ms. How?**
Because 8,650 of ~13,000 requests were refused with 402 before the model was ever called — that is
pipeline ordering D6 working, and it is why the aggregate percentiles are dominated by the cheap
refusal path. The `steady` class, which does reach the model, shows p99 2,800 ms.

**Q: How do you know the alert rules work and not just that the YAML parses?**
Prometheus reports them `health=ok` and `state=inactive` with a recent `lastEvaluation` — it is
evaluating the expressions, not just holding the file. I have not forced one to fire; that would be
the next step and I would do it by dropping a threshold rather than by generating a real incident.

**Q: Was anything actually wrong with the dashboard?**
Two things, both found by checking rather than assuming. The cache hit-rate denominator counted
`result=bypass` and would have understated the rate — found by reading Suraj's code, not the metric
name. And the Prometheus container silently had no rules because it predated the volume mount — a
provisioning change needs a recreate, not a restart.
