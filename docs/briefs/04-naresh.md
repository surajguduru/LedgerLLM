# Brief 04 — Billing correctness and usage API — **Naresh**

## Mission
You own the correctness of the project's central claim — **budgets cannot be overspent under burst** — and the usage API that
exposes spend to tenants. The base already has atomic reserve → settle and a ledger; your job is to *prove* it on Postgres,
finish the soft-warning semantics, and extend the usage API. You also present the architecture and assemble the deck.

## Files you own
`app/billing/{pricing,budget,ledger,usage}.py`, `app/api/usage.py`, `config/prices.yaml`, `tests/test_budget.py`,
`tests/test_pricing.py`. You may add columns to `budget_periods` and `usage_ledger`.

## How the base works
`reserve()`: `UPDATE budget_periods SET reserved += est WHERE tenant, period AND spent + reserved + est <= hard_limit`;
`rowcount == 1` means admitted. `settle(est, actual)` adds actual to spent and releases est; `release(est)` undoes a reservation.
Guardrail-classifier cost is booked via `settle(0, cost)` + a `purpose="guardrail"` ledger row.

## Must-have tasks
### A. Concurrency proof on Postgres (replace the xfail)
Start uvicorn in a thread on a free port against Postgres (`docker compose up -d db`), tenant with `budget_override_usd=0.004`,
50 concurrent `httpx` requests via `ThreadPoolExecutor`; assert `sum(ledger.cost) == spent`, `spent <= hard_limit`,
`reserved == 0`, and `#200 == #admitted reservations`. `@pytest.mark.skipif` unless `DATABASE_URL` is Postgres, so it runs in
the `test-postgres` CI job.
### B. Soft warning exactly once per period (replace the xfail)
In `reserve()`: if committed ≥ 80 % and `soft_warned_at IS NULL` → `UPDATE … SET soft_warned_at = now WHERE soft_warned_at IS NULL`;
on `rowcount == 1` write the `budget.soft_warning` audit event (`app.compliance.audit`). Remove the pipeline `TODO(Naresh)`
(`CONTRACT CHANGE:` line). Test: 10 requests → exactly one event.
### C. Rollover and limit sync
Tests: new month → new row (monkeypatch `current_period`); plan/override change mid-month → `hard_limit` updates on the next request.
### D. Guardrail allowance decision (D15)
Classifier tokens are billed but not reserved. Either add `guardrail_allowance_usd` per plan to the estimate, or accept the boundary
overshoot and document it. Write D15 into DESIGN §3.
### E. Usage API
`by_day` (requests, cost — `func.date(UsageLedger.created_at)` works on both dialects), `last_requests` (10 most recent ledger rows),
`GET /v1/usage/statement.csv`. Additive `UsageSummary` fields → `CONTRACT CHANGE: additive`. Record `estimate_microusd` on completion
rows (new column + optional kwarg to `ledger.book`) so the pessimism ratio is measurable.

## Good to have
- Webhook/e-mail stub on soft warning; admin top-up endpoint for a period.

## Numbers to produce
- Cost per request at list price by style/max_words; reservation pessimism (median, p95 of estimate/actual);
  burst admitted vs 402 and `spent` vs `hard_limit` (with Yashraj); share of spend that is guardrail cost.

## What to write
- README: "Cost attribution & budgets" section (ledger row example, two sentences on reserve-then-settle, numbers).
- Slides 3–4 (3.5 min): architecture (components → pipeline → data model → deployment) then billing (ledger anatomy, D2 animation of
  stale-counter failure vs atomic reserve, burst chart, D5, D7). Failure mode: provider failure releases the reservation.
- Deck: create the shared Google Slides + template + timer; everyone's slide in by Thu 9 Oct 21:00.

## Pitfalls
- Money is `int` micro-USD everywhere until `microusd_to_usd` at the response layer.
- `db.refresh()` after raw UPDATEs (`expire_on_commit=False`).
- SQLite serialises writers — only Postgres proves concurrency.
- Postgres `func.date()` uses the session time zone: add `options=-c timezone=UTC` to the DSN or cast explicitly.

## Agent kickoff prompt
> Read `CONTRIBUTING.md`, `docs/DESIGN.md`, `docs/briefs/README.md` and this brief, then `app/billing/budget.py`
> and `app/api/summarize.py`. Implement the once-per-period soft-warning audit event, a Postgres-only 50-way concurrency test
> asserting the ledger never exceeds the limit, rollover tests, and `by_day` / `last_requests` / CSV statement on the usage API.
> Run `make lint test` on SQLite and Postgres. Do not edit other packages beyond the `TODO(Naresh)` line.
