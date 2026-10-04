# Measurements, trade-offs, failure modes

Add yours as soon as you have it. Yashraj copies final values into the README benchmarks table.
Format: **what** · value · conditions · command · date · who.

## Numbers
- Base platform overhead, SQLite, mock, single request: 13 ms (`http_request latency_ms` log line) · 4 Oct · Suraj — replace with Locust p99.
- Red-team baseline (heuristics only, 7 attacks / 5 benign): catch 85.7 %, FPR 20 %, p50 0.005 ms · `make eval-redteam` · 4 Oct · Suraj.

## Trade-offs ("we chose X over Y because Z")
- Postgres-only over Redis for counters/budgets — one atomic UPDATE suffices under 100 rps; one fewer service.
- Reserve-then-settle over check-then-call — bursts cannot overspend; cost: pessimistic refusals near the limit.
- Integer micro-USD over float — exact sums, atomic increments.

## Failure modes (and the handling)
- Provider failure → 502, reservation released, nothing billed, audit event (`test_upstream_failure_releases_reservation_and_audits`).
