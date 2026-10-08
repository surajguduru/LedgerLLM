# Measurements, trade-offs, failure modes

Add yours as soon as you have it. Yashraj copies final values into the README benchmarks table.
Format: **what** · value · conditions · command · date · who.

## Numbers
- Base platform overhead, SQLite, mock, single request: 13 ms (`http_request latency_ms` log line) · 4 Oct · Suraj — replace with Locust p99.
- Red-team baseline (heuristics only, 7 attacks / 5 benign): catch 85.7 %, FPR 20 %, p50 0.005 ms · `make eval-redteam` · 4 Oct · Suraj.
- Rate-limit check (atomic upsert + commit), 1,000 calls: Postgres 16 in Docker on a laptop p50 747 µs / p99 1,404 µs; SQLite file p50 317 µs / p99 711 µs · `python -m scripts.bench_traffic` · 6 Oct · Suraj.
- Fixed-window edge burst: 10 requests admitted within 2 s on the free plan (rpm 5) = 2.0× rpm, the bound D3 accepts · `python -m scripts.bench_traffic` · 6 Oct · Suraj.
- Docker image 460 MB; container start → `/healthz` OK in 1.1 s locally (Render free-tier wake-up still to measure) · `docker build`, `docker run` · 6 Oct · Suraj.

## Trade-offs ("we chose X over Y because Z")
- Postgres-only over Redis for counters/budgets — one atomic UPDATE suffices under 100 rps; one fewer service.
- Reserve-then-settle over check-then-call — bursts cannot overspend; cost: pessimistic refusals near the limit.
- Integer micro-USD over float — exact sums, atomic increments.
- Fixed window over token bucket or sliding log — one upsert per request and no per-request history; cost: up to 2× rpm across a boundary (measured above).
- Free cache hits booked as zero-cost ledger rows over billing hits — the tenant gets an honest request count and pays only for model work (D16).

## Failure modes (and the handling)
- Provider failure → 502, reservation released, nothing billed, audit event (`test_upstream_failure_releases_reservation_and_audits`).
- Duplicate request in flight with the same Idempotency-Key → 409 `idempotency_in_progress` + Retry-After; a failed request releases the key (`test_duplicate_in_flight_gets_409_in_progress`, `test_failure_clears_pending_so_retry_runs`).
- Two identical cache misses at once → the cache write is an upsert, so neither request fails (`cache.store`).
