# Load test and latency evidence

Two separate measurements, deliberately not one (decision **D8**):

| | Tool | Provider | Question it answers |
|---|---|---|---|
| **Burst** | `locustfile.py` | **mock** | Does our quota logic hold when 50 clients race for one tiny budget? |
| **Latency** | `latency_sample.py` | **gemini** | How slow is a real request, and how much of that is us? |

The burst test uses the mock provider on purpose. The claim under test is *our* accounting, not
Google's response time, and hammering a real provider would burn the free-tier quota while proving
nothing about our code. Real-provider latency is measured separately on a small paced sample.

---

## Status

| Run | Status |
|---|---|
| Burst — **Postgres 16, `MOCK_LATENCY_MS=800`** | ✅ **done — HOLDS. This is the proof** |
| Platform overhead — sequential, local | ✅ done, target met |
| Burst — SQLite | ✅ done, kept below only as the contrast that shows the write lock |
| Burst — Render deployment | ⬜ not run yet (needs the deployment's `ADMIN_TOKEN` to set a test budget) |
| Real-provider latency — Gemini free tier, Groq free tier, production | ✅ done — numbers in the README benchmarks table |

> **8 Oct: the test had to be redesigned.** The rate limiter and response cache landed on `main`,
> and between them they made the old burst test meaningless — it stopped producing a single 402. See
> *Why one key and one document no longer works* below. Numbers on this page are from the new design.

> **Why the Postgres run is the real one.** SQLite takes a database-wide write lock, so concurrent
> requests queue instead of racing. That makes the budget *look* safe for the wrong reason — the
> serialisation hides the race rather than the reserve winning it. Postgres executes the atomic
> `UPDATE … WHERE spent + reserved + est <= limit` under genuine concurrency, which is the
> condition decision **D2** is designed for. Numbers below are a dry run of the tooling, not the proof.

---

## Commands

```bash
# 1. burst, local Postgres (the real run)
docker compose up -d db
export DATABASE_URL=postgresql+psycopg://ledger:ledger@localhost:5432/ledger
make seed                                 # seeds the 'pro' and 'free' keys the other users need
LLM_PROVIDER=mock MOCK_LATENCY_MS=800 .venv/bin/uvicorn app.main:app --port 8000
make loadtest                             # 50 users, 60 s
.venv/bin/python loadtest/verify_quota.py # must print: quota enforcement: HOLDS

# The burst tenant is created by the locustfile itself at test start (one key per user) and written
# to loadtest/burst_tenant.json, which verify_quota.py reads. That needs the admin token:
#   ADMIN_TOKEN=... make loadtest          # defaults to the dev token
# Without it the run falls back to the single seeded 'burst' key, prints a warning, and the 402
# count stops being a meaningful test of the cutoff.

# 2. platform overhead only — no fake model latency, sequential so there is no queueing
MOCK_LATENCY_MS=0 .venv/bin/uvicorn app.main:app --port 8000
.venv/bin/python loadtest/latency_sample.py --per-size 20 --sleep 0

# 3. real-provider latency (30 paced requests, ~2.5 min)
LLM_PROVIDER=gemini LLM_API_KEY=AIza... .venv/bin/uvicorn app.main:app --port 8000
.venv/bin/python loadtest/latency_sample.py

# against the deployment instead of localhost (warm it first, cold start is reported separately)
make loadtest HOST=https://<render-url>
HOST=https://<render-url> .venv/bin/python loadtest/verify_quota.py
```

---

## Why one key and one document no longer works

The old test pointed 50 users at one API key with one fixed document. On current `main` that produces:

```
status code distribution: {200: 120, 429: 3621}      <- not one 402
```

Two stages sit in front of the budget and absorbed the whole burst:

1. **The rate limiter is per key; the budget is per tenant.** Every request past `plan.rpm` is
   refused with `429` at stage 3, before the budget is consulted at stage 5. The limiter *shields*
   the budget, so the cutoff never fires. This is correct system behaviour — and it makes a
   single-key burst useless as a budget test.
2. **The response cache returns before the budget reserve.** Stage 4.5 is an exact match on the
   request body and returns at zero cost. Sending the same document every time means nearly every
   request is a free cache hit, so the budget is never spent down.

`verify_quota.py` still printed `HOLDS` — **vacuously**. Spend was under the limit because almost
nothing had been spent. That is the kind of green result that is worse than a red one.

**The fix** (`locustfile.py`): to put real concurrent pressure on one tenant budget,

- **one key per simulated user, all keys on one tenant** — budgets are per tenant, limits are per
  key, so N keys give N× the admitted throughput against a single shared budget;
- **each user paced below its key's own rpm** (enterprise, 600 rpm = 10 req/s; users run at ~5 req/s)
  so the limiter stays out of the way of the thing being measured;
- **a unique document per request** so the cache cannot serve it for free.

Separate small user classes still exercise the cache (`SteadyUser`, repeated document) and the
limiter (`RateLimitUser`, free-plan key at rpm 5) on purpose, so those paths still appear on the
dashboard. `verify_quota.py` now reports **INCONCLUSIVE** (exit 2) if the run produced no 402s at
all, so this failure cannot pass silently again.

---

## Result 1 — Platform overhead ✅ target met

Our own cost per request: auth, idempotency, rate-limit check, cache lookup, budget reserve,
guardrail heuristics, ledger write, redacted log, audit, metrics. `MOCK_LATENCY_MS=0` removes the
model, and requests are **sequential** so the number is per-request work, not queueing delay.

`latency_sample.py --alias enterprise --per-size 20 --sleep 0` · mock · macOS dev machine · 8 Oct (evening re-run on the current code)

| Document | Input tokens | p50, SQLite | steady-state max, SQLite | p50, Postgres (Docker) | steady-state max, Postgres |
|---|---|---|---|---|---|
| small | 1,192 | **13 ms** | 23 ms | **18 ms** | 23 ms |
| medium | 3,192 | **27 ms** | 31 ms | **32 ms** | 59 ms |
| large | 6,192 | **44 ms** | 50 ms | **53 ms** | 56 ms |

**Target: p99 ≤ 25 ms → met for ~1k-token documents only.**

Three honest notes:
- The **first request after process start** is 66–71 ms (lazy imports, cold connection pool, empty
  caches). It is excluded above and reported separately rather than folded into a percentile.
- Overhead rose from **7/8/10 ms** (8 Oct morning, `main` @ 72a7ab3) to **13/27/44 ms** on the current
  code. The pipeline gained the per-tenant limiter upsert, title scanning, the body-size check and a wider
  guardrail rule set; and the cost now clearly grows with the document, because the input guardrail scans
  all of it: measured in isolation, `classify_input(document)` is 3.5 / 9 / 18 ms and the log redactor
  0.6 / 1.6 / 3.1 ms on these three sizes. That scan is the next thing to optimise (or to cap at the first
  N characters) if the 25 ms target matters for long documents.
- Use a key whose plan allows 60 requests inside a minute (`--alias enterprise`); the pro key's 60 rpm is
  exactly the sample size and fails the run with 429s if anything else used the key that minute.

---

## Result 2 — Burst, quota enforcement ✅ **HOLDS on Postgres**

**50 users, 50 keys on one enterprise tenant, 60 s, `MOCK_LATENCY_MS=800`, Postgres 16** · 8 Oct (evening re-run)
Tenant budget **$0.02**, provisioned by the locustfile at test start.

| | |
|---|---|
| Requests | 16,047 (**268 req/s**) |
| `200 OK` | 157 (27 of them the burst tenant; the rest the steady pro key, mostly cache hits) |
| `402 budget_exceeded` | **1,173** |
| `429 rate_limited` | 14,733 (5,773 from the user class that trips the limiter on purpose; the rest the burst tenant's own 600 rpm, shared by its 50 keys) |
| p50 / p95 / p99 | 24 ms / 84 ms / **140 ms** |

```
  spent <= limit : True  ($0.018765 of $0.020000)
  reserved == 0  : True  ($0.000000)
  402 refusals   : 1173
  unspent        : $0.001235 (6.2% of budget, 1.8x the average request) — reservation pessimism, D2

quota enforcement: HOLDS
```

The morning run on `main` @ 72a7ab3 showed 8,650 × 402 and 4,195 × 429 at 218 req/s. Since then the rate
limit is enforced per tenant as well as per key (a tenant's keys share the plan's rpm), so most of the
burst is now refused at stage ② before the budget is consulted; the budget cutoff is still exercised
1,173 times, and the verdict and the spend are unchanged.

**Why this run is the proof and the SQLite one was not.** SQLite takes a database-wide write lock, so
concurrent requests queue instead of racing — the budget looks safe for the *wrong* reason, because
serialisation hides the race rather than the atomic reserve winning it. Postgres runs the
`UPDATE … WHERE spent + reserved + est <= limit` under genuine row-level concurrency. 50 keys on one
tenant means 50 clients genuinely racing for the same budget, which is the condition decision **D2**
exists for.

**Why 800 ms matters.** That is the hostile case, not a slow one. The naive alternative —
`check spent < limit`, *then* call the model — leaves a window the width of the model call during
which every concurrent request reads the same stale `spent` and all of them pass. The wider the
window, the more requests slip through it.

This is no longer a thought experiment: we **implemented the rejected design and measured it**
(README, "Cost attribution & budgets"). On a $0.004 budget with 50 concurrent requests, check-then-call
admitted 10 and spent **164 % of the limit**; the atomic reserve admitted 4 and spent 66 %, with the
ledger total equal to spend and nothing left reserved. That is the counterfactual, measured rather
than argued, and it is the strongest single number in the project.

Our run here is the same property at a wider window and a larger key count.
**Measured overspend: $0.000000** — spend stopped at $0.018765 of $0.020000.

The reserve closes the window because admission and accounting are the *same* statement: there is no
gap between deciding and recording.

**`reserved == 0` is the half people forget.** "spent ≤ limit" alone could be satisfied by a system
that reserved budget and never released it — it would look compliant while slowly eating money
tenants never spent. Reservations returning to exactly zero proves every reserve was matched by a
settle or a release, including on the 8,650 refusal paths and the error paths.

**Why the headroom, quantified.** $0.001235 (6.2%) went unspent because the reserve books the worst
case — a full `max_tokens` of output — and a shorter summary settles for less. The leftover is
**1.8× the average request**, i.e. less than two more requests' worth, so the cutoff was not being
wasteful: nothing further would reliably have fit. D2's accepted pessimism, measured.

**Why the cutoff is provably exercised.** `verify_quota.py` keys off the **402 count**, not the spend
level. A spend threshold cannot distinguish "stopped short because of reservation pessimism" from
"never reached the limit at all" — a mistake this script made in its first version and now avoids by
reporting **INCONCLUSIVE** (exit 2) when a run produces no 402s.

### Postgres vs SQLite, same code

| | SQLite (8 Oct morning) | Postgres 16 (8 Oct morning) | Postgres 16 (8 Oct evening, current code) |
|---|---|---|---|
| Throughput | 228 req/s | 218 req/s | 268 req/s |
| p99 | 670 ms | **140 ms** | **140 ms** |
| Quota verdict | HOLDS (but serialised) | **HOLDS (genuinely raced)** | **HOLDS (genuinely raced)** |

Postgres is **4.8× better at p99** on identical code. That gap is the proof that the SQLite p99 was
its write lock and not our overhead — and it is `DESIGN.md` §5 limit #1 (four writes per request)
showing up as a measurement rather than a prediction. Aggregate percentiles here are dominated by
402 refusals, which never reach the model; the `steady` class, which does, shows p99 2,800 ms under
a deliberately slowed 800 ms model.

**Note on cache hit rate during a burst.** The burst deliberately defeats the cache with unique
documents, so a burst run shows a poor hit rate. Only `SteadyUser` generates hits — read the cache
panel from steady traffic, not from a burst.

---

## Result 3 — Real-provider latency

`latency_sample.py` with `LLM_PROVIDER=gemini`: 30 requests, 10 per size, 4.5 s apart to stay inside
the free tier's per-minute limit. Reports end-to-end p50/p95/p99, model-only (`usage.latency_ms`) and
the difference. Results (Gemini free tier, Groq free tier, and production on Render + Neon) are in the
README benchmarks table; the free-tier quota refusals are recorded as failures, not retried.

Target for a ~3k-token document: p50 ≤ 3 s, p99 ≤ 8 s.
