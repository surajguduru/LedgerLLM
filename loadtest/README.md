# Load test and latency evidence

Owner: Yashraj. Two separate measurements, deliberately not one (decision **D8**):

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
| Burst — SQLite, local | ✅ done (below). Preliminary: SQLite serialises writes |
| Platform overhead — sequential, local | ✅ done (below), target met |
| Burst — **Postgres** | ⬜ **pending** — this is the run that actually proves concurrency |
| Burst — Render deployment | ⬜ pending deployment (Suraj) |
| Real-provider latency — Gemini | ⬜ pending (needs `LLM_API_KEY`) |

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
DATABASE_URL=postgresql+psycopg://ledger:ledger@localhost:5432/ledger make seed
DATABASE_URL=postgresql+psycopg://ledger:ledger@localhost:5432/ledger \
  LLM_PROVIDER=mock MOCK_LATENCY_MS=800 .venv/bin/uvicorn app.main:app --port 8000
make loadtest                             # 50 users, 60 s
.venv/bin/python loadtest/verify_quota.py # must print: quota enforcement: HOLDS

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

## Result 1 — Platform overhead ✅ target met

Our own cost per request: auth, idempotency, rate-limit check, budget reserve, guardrail heuristics,
ledger write, redacted log, audit, metrics. `MOCK_LATENCY_MS=0` removes the model, and requests are
**sequential** so the number is per-request work and not queueing delay.

`latency_sample.py --per-size 20 --sleep 0` · mock · SQLite · macOS dev machine · 7 Oct

| Document | Input tokens | p50 | p95 | max (n=20) |
|---|---|---|---|---|
| small | 1,181 | **6 ms** | 9 ms | 9 ms |
| medium | 3,181 | **7 ms** | 9 ms | 9 ms |
| large | 6,181 | **8 ms** | 11 ms | 11 ms |

**Target: p99 ≤ 25 ms → met.** Worst request observed across all 60 was 11 ms.

Overhead grows only ~2 ms from a 1.2k-token to a 6.2k-token document, i.e. it is roughly flat in
document size. That is expected: the per-request work is a fixed number of small SQL statements plus
regex guardrails; nothing in the platform path scales with document length except the regex scan.

---

## Result 2 — Burst, quota enforcement ✅ HOLDS (SQLite, preliminary)

`locustfile.py`, 20 users, 20 s, `MOCK_LATENCY_MS=120`, SQLite · 7 Oct

The `burst` tenant is seeded with a **$0.02** monthly budget (`scripts/seed.py`), so it exhausts
after a couple of dozen requests and everything after that must be refused.

| | |
|---|---|
| Requests sent | 4,125 (229 req/s) |
| `200 OK` | 103 |
| `402 budget_exceeded` | 4,022 |
| `429 rate_limited` | 0 — see note |
| Latency p50 / p95 / p99 | 17 ms / 170 ms / 430 ms |

`verify_quota.py`:

```
limit_usd      0.020000
spent_usd      0.019264     <- never exceeded the limit
reserved_usd   0.0          <- every reservation was settled or released
requests       28
quota enforcement: HOLDS
```

**What this shows.** 4,022 requests were refused *before* reaching the model, so they cost nothing.
Spend stopped at $0.019264 of a $0.020 limit — under it, never over. `reserved_usd` returning to
exactly 0 is the second half of the proof: no reservation leaked, so a crash or a refusal mid-flight
does not permanently eat a tenant's budget.

**Why the headroom.** $0.000736 was left unspent. The reserve books the *worst-case* cost (full
`max_tokens` of output) before the call, and a summary that comes back shorter settles for less. So a
tenant near their limit can be refused a request that would in fact have fit. That pessimism is the
accepted cost of D2, and this is it measured: ~3.7% of the budget unused.

**Note on 0 × 429.** `app/traffic/ratelimit.py` is still a pass-through stub (Suraj's task), so no
request was rate limited. Re-run after that lands to get a real 429 count.

---

## Result 3 — Throughput ✅ target met (SQLite, preliminary)

`locustfile.py`, 50 users, 30 s, `MOCK_LATENCY_MS=0`, SQLite · 7 Oct

| | |
|---|---|
| Throughput | **270 req/s** (target ≥ 50) |
| Requests | 8,040 |
| p50 / p95 / p99 | 73 ms / 310 ms / 930 ms |
| `200` / `402` | 374 / 7,666 |

Throughput beats the target 5×, but read it with the caveat that 95% of these requests are cheap
402s that never reach the model.

**The p99 of 930 ms is not platform overhead** — compare it with the 11 ms worst case in Result 1.
The difference is SQLite's database-wide write lock: at 50 concurrent writers, requests spend their
time waiting for the lock, not doing work. This is the clearest argument for re-running on Postgres,
and it is also a live demonstration of scaling limit #1 in `DESIGN.md §5` (write amplification — four
writes per request).

---

## Result 4 — Real-provider latency ⬜ pending

`latency_sample.py` with `LLM_PROVIDER=gemini`: 30 requests, 10 per size, 4.5 s apart to stay inside
the free tier's per-minute limit. Reports end-to-end p50/p95/p99, model-only (`usage.latency_ms`) and
the difference. Verified working against the mock; needs a Gemini key for the real numbers.

Target for a ~3k-token document: p50 ≤ 3 s, p99 ≤ 8 s.
