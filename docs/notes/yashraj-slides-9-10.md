# Slides 9–10 — observability and load numbers

OWNER: Yashraj. 2.5 minutes for both slides. The deck is Naresh's shared Google Slides file; this is
the content to paste in, plus what to say. Every number here is a line in `docs/MEASUREMENTS.md` — if
you change a number there, change it here too.

Required by `docs/briefs/06-yashraj.md`: dashboard by category · burst chart + the `verify_quota`
line · mock-vs-real latency (D8) · what breaks at 10× (DESIGN §5) · next steps · live URL ·
trade-off: per-tenant labels vs ledger aggregation · failure mode: naive check-then-call.

---

## Slide 9 — "Can we see it, and does the budget hold?"

**Title:** Observability: three views, one ledger

### Left column — what we watch (DESIGN §4 categories)

- **Traffic** — req/s, status split, limiter refusals
- **Latency** — p50/p95/p99 end-to-end, and platform overhead on its own
- **Cost** — µUSD per request, spend per tenant, budget utilisation
- **Quality** — judge score and thumbs up/down, **per prompt version**
- **Drift** — cost per request, guardrail block rate, score over time

### Right column — who looks at which

| View | Who | Built on |
|---|---|---|
| Grafana operational | us, on call | Prometheus `/metrics` |
| Grafana admin | platform admin | Neon **read replica** (SQL) |
| `/dashboard` | the paying tenant | `/v1/usage` (the ledger) |

> One sentence that ties it together: **metrics answer "is the platform healthy", the ledger answers
> "what does this tenant owe" — and they are deliberately different sources.**

### Bottom strip — the burst result

**Budget hard-cutoff under concurrency: HOLDS**

- 50 users · 50 keys · **one** tenant · $0.02 budget · 60 s · Postgres 16 · `MOCK_LATENCY_MS=800`
- ~13,000 requests at **218 req/s** → **200: 157 · 402: 8,650 · 429: 4,195**
- `verify_quota.py` says so itself:

```
  spent <= limit : True  ($0.018765 of $0.020000)
  reserved == 0  : True  ($0.000000)
  402 refusals   : 8650

quota enforcement: HOLDS
```

*(Chart to draw: one stacked bar, or a 3-slice donut, of the 200/402/429 split. Put the
`verify_quota` line underneath as monospace text — it is the single most convincing artefact on the
slide, because it is the tool's own verdict, not my summary of it.)*

### Trade-off to state out loud

**Per-tenant Prometheus labels vs aggregating from the ledger.**
Measured: **171 series** total, of which only **18** carry a `tenant` label = **6 series per tenant**.
Extrapolate to 1,000 tenants × 3 models ≈ **27,000 series** from this app alone. So per-tenant
*metrics* do not scale, and that is exactly why the tenant's own numbers come from `usage_ledger`
(one row per billable call, aggregated on read) and not from Prometheus. DESIGN §5 limit #4.

Honest aside worth volunteering: of those 171 series, **85 are `http_*` and mostly my own doing** —
widening the latency histogram from 3 buckets to 14 costs ~11 extra series per handler/method pair.
Cardinality is the price of being able to measure the SLO at all.

---

## Slide 10 — "What it costs, what breaks next"

**Title:** Latency: mock vs real, and the first thing to break at 10×

### Block 1 — mock vs real (D8: load-test on the mock, measure quality on the real model)

| Condition | end-to-end p50 | platform overhead | verdict |
|---|---|---|---|
| Mock, local, no model latency | — | **7 / 8 / 10 ms** (1.2k/3.2k/6.2k tokens) | overhead target p99 ≤ 25 ms **met** |
| Mock, Postgres, under burst | 55 ms (p95 95, p99 **140**) | — | throughput target ≥ 50 req/s **met** (218 req/s) |
| **Groq** `qwen3.8-27b`, real | **0.50 / 0.73 / 0.82 s** | 64 / 106 / 133 ms | p50 ≤ 3 s, p99 ≤ 8 s **met** |
| **Gemini** free tier, real | **6.6 / 5.7 s** (12 of 30 calls survived) | 57 / 94 ms | **missed** — free-tier queueing |
| **Production** (Render + Neon) | **2,273 ms** (p95/p99 **25,660**) | **~860 ms** | p99 ≤ 8 s **missed** |

**The point of the table:** the pipeline's own cost is ~0.1 s in every single row. Latency is set by
the provider and the tier, not by our code — which is *why* D8 says load-test on the mock: a burst
against a real provider measures the provider's queue and burns the free-tier quota.

### Block 2 — the production miss, and its cause

Platform overhead in production was **~860 ms** against **7–10 ms** locally. Not CPU:

- the pipeline makes **~19 database calls per request**
- `render.yaml` pinned no region, so Render defaulted to **Oregon**; Neon is in **Ohio**
- 19 × ~50 ms RTT ≈ **950 ms**, which matches the measured gap
- Fix: **one line** — `region: ohio` in `render.yaml`

This is DESIGN §5 limit #1 (four writes per request) showing up in production while being invisible
locally. Good slide material because the diagnosis is arithmetic, not guesswork.

> Status to say honestly: the fix is written and in review; the re-measurement after redeploy is the
> number I still owe. Do **not** present the fix as verified.

### Block 3 — what breaks first at 10× (DESIGN §5)

1. **Write amplification** — 4 writes/request → sample `last_used_at`, batch logs, partition the ledger by month *(and it is already the top production cost, measured above)*
2. **Hot-key counter contention** in `rate_limit_windows` → Redis `INCR`/`EXPIRE`
3. **Guardrail classifier CPU** on one instance → separate service
4. **Prometheus `tenant` cardinality** → aggregate from the ledger *(measured: 6 series/tenant)*
5. **One instance** is the current ceiling; scaling out is safe because no state lives in-process

### Block 4 — failure mode: naive check-then-call

Measured, side by side on the same code path (Naresh, `bench_billing burst`, 50 concurrent,
$0.004 budget, 50 ms model latency):

- atomic **reserve**-then-settle → admits 4, spends **66 %** of the limit
- naive **check `spent`** then call → admits 10, spends **164 %** of the limit

So the naive version overspends; the atomic reserve cannot. Say it as *"it overspent by 1.6× at
50 ms"* — and if asked how bad it gets, give the driver rather than a number:

> overspend ≈ (arrival rate × model latency) ÷ (requests that fit in the budget)

because `spent` only rises when a call *returns*, so everything that arrives inside one model-latency
window sees a stale zero. At my burst's parameters (218 req/s, 800 ms, a budget fitting ~157
requests) that is ≈ 2×; on a budget that fits only ~3 requests the same arithmetic reaches 50×,
because one 800 ms window admits ~174 of them. **The brief's "~50×" is therefore budget-dependent, not a constant — don't quote it flat.**

### Block 5 — next steps (keep to three, say them fast)

1. **Alembic migrations.** D4 accepts no in-place migrations; today a schema change against
   production means dropping the schema. This is the real blocker to calling it production-ready.
2. **Verify the region fix** and re-measure production overhead (expect ~860 ms → tens of ms).
3. **Aggregate per-tenant cost from the ledger** and drop the `tenant` metric label before tenant
   count grows — the 27,000-series extrapolation is the trigger.

### Live URL

- API + tenant dashboard: `https://<service>.onrender.com/dashboard`
- Admin (Grafana): `https://ledgerllm-admin.onrender.com`
- Free tier sleeps after 15 min idle — **warm it before the demo**, and report cold start (30–60 s)
  separately from the latency numbers.

---

## Delivery notes (2.5 min is tight)

- Slide 9: ~70 s. Spend it on the burst result and the trade-off. The three-views table is a glance,
  not a read-out.
- Slide 10: ~80 s. Lead with "the pipeline is 0.1 s in every row", then the 860 ms diagnosis, then
  one sentence each for 10× and next steps.
- Have ready for questions, not on the slide: the histogram-bucket reason both SLO targets are bucket
  **edges**; why the first burst test was invalid (per-key limiter + exact-match cache in front of the
  per-tenant budget); Postgres p99 140 ms vs SQLite 670 ms and why (write lock, not our overhead).

## Screenshots still owed for these slides

- [ ] Grafana operational dashboard (needs the stack up: `docker compose up -d`)
- [ ] `/dashboard` for a tenant with real spend (budget doughnut + spend-by-day)
