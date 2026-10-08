# Measurements, trade-offs, failure modes

Add yours as soon as you have it. Yashraj copies final values into the README benchmarks table.
Format: **what** · value · conditions · command · date · who.

## Numbers
- Base platform overhead, SQLite, mock, single request: 13 ms (`http_request latency_ms` log line) · 4 Oct · Suraj — replace with Locust p99.
- Red-team baseline (heuristics only, 7 attacks / 5 benign): catch 85.7 %, FPR 20 %, p50 0.005 ms · `make eval-redteam` · 4 Oct · Suraj.
- Rate-limit check (atomic upsert + commit), 1,000 calls: Postgres 16 in Docker on a laptop p50 747 µs / p99 1,404 µs; SQLite file p50 317 µs / p99 711 µs · `python -m scripts.bench_traffic` · 6 Oct · Suraj.
- Fixed-window edge burst: 10 requests admitted within 2 s on the free plan (rpm 5) = 2.0× rpm, the bound D3 accepts · `python -m scripts.bench_traffic` · 6 Oct · Suraj.
- Docker image 460 MB; container start → `/healthz` OK in 1.1 s locally (Render free-tier wake-up still to measure) · `docker build`, `docker run` · 6 Oct · Suraj.
- Gemini reasoning tokens vs `reasoning_effort`, one ~300-word news document, bullets ≤ 80 words, `max_tokens` 250 · 8 Oct · Sai Venkatesh. Reasoning = `total − prompt − completion` (the endpoint sends no `completion_tokens_details`); prompt was 355 tokens in every call:

  | model | `reasoning_effort` | HTTP | finish | visible chars | completion tokens | reasoning tokens |
  |---|---|---|---|---|---|---|
  | gemini-3.8-flash | not sent | 200 | length | 29 | 6 | 240 |
  | gemini-3.8-flash | none | 200 | stop | 525 | 108 | 0 |
  | gemini-3.8-flash | minimal | 400 "Thinking level MINIMAL is not supported" | – | – | – | – |
  | gemini-3.8-flash | low | 200 (two 503 "high demand" first) | stop | 524 | 106 | 0 |
  | gemini-3.5-flash-lite | not sent | 200 | stop | 537 | 119 | 0 |
  | gemini-3.5-flash-lite | none | 400 "invalid argument" | – | – | – | – |
  | gemini-3.5-flash-lite | minimal | 200 | stop | 553 | 122 | 0 |
  | gemini-3.5-flash-lite | low | 200 | stop | 474 | 109 | 0 |

  Findings: `completion_tokens` excludes reasoning, `total_tokens` includes it, and `max_tokens` caps both together (6 + 240 = 246 ≤ 250), so the reservation bound holds once reasoning is billed as output. "low" is the lowest level both models accept and is now the Gemini default · throwaway httpx script against `/v1beta/openai/chat/completions` (11 calls) · 8 Oct · Sai Venkatesh.
- Summarization LLM judge, 3-case golden set, prompt `summarize_v1@ec6822c047b1`, summarizer gemini-3.8-flash (`reasoning_effort=low`), judge gemini-3.5-flash-lite, 8 rpm per model, free tier: **1 of 3 cases completed** — summarizer 503 ("high demand") then 429 after the full 15/30/60 s back-off on sum-001 and sum-003. On sum-002: faithfulness 5, coverage 5, key-point hit rate 1.00, 917 tokens (summary 407 incl. reasoning, judge 510); judge errors 2/3 (both "no summary to judge"; the judge itself returned valid JSON first time); wall time 406 s; gate FAIL (judge error rate 0.67 > 0.10), as designed: a run that could not score its cases must not pass. Not a quality number yet — rerun when the quota resets · `python -m evals.summarization.run --provider gemini --gate` → `evals/summarization/results/last_gemini.json` (13 calls) · 8 Oct · Sai Venkatesh.
- Same eval minutes earlier, before the `reasoning_effort=low` fix was merged (summaries cut off at ~12 words by hidden reasoning; tokens then excluded reasoning): faithfulness 4.67, coverage 2.33, hit rate 0.08, judge errors 0/3, 2,223 tokens (summaries 956, judge 1,267) ≈ 741 per case, wall time 113 s, gate FAIL (hit rate, coverage). The judge flagged the truncation itself ("the summary is cut off mid-sentence"). Three of these summaries are in `calibration.jsonl` as low-coverage rows · same command (9 calls, three of them 503s) · 8 Oct · Sai Venkatesh.
- Judge vs human agreement: pending — 4 rows in `evals/summarization/calibration.jsonl` await blind human scores · `python -m evals.summarization.agreement` · 8 Oct · Sai Venkatesh.
- `validate_url` overhead per call, resolver patched (DNS excluded): median 6.5 µs, p99 7.2 µs over 1,000 calls (three runs: 6.4 / 6.5 / 6.5 µs), Python 3.14, Apple M5 laptop. A real lookup adds whatever the resolver takes; the guard itself is negligible next to the fetch · throwaway `perf_counter_ns` loop over `validate_url("https://example.com/articles/ledger?x=1")` with `socket.getaddrinfo` returning one public address · 8 Oct · Sai Venkatesh.
- Provider fallback, real Gemini, one summarize request (~300-word news text, bullets ≤ 80 words, pro plan, model `gemini-3.8-flash`): primary forced to fail with `LLM_TIMEOUT_S=0.001` (timed out before any reply; reason "gemini timed out"), fallback `gemini-3.5-flash-lite` with `LLM_FALLBACK_TIMEOUT_S=30` → **200**, `usage.model` gemini-3.5-flash-lite, `usage.fallback_from` gemini-3.8-flash, 505 in / 107 out tokens, **419 µUSD** (505 × 0.30 + 107 × 2.50, flash-lite list price, price version 2026-10-08); ledger row `('gemini-3.5-flash-lite', 505, 107, 419, '2026-10-08', 1353, 'ok')`; budget period after settle reserved 0 / spent 419; metric `ledgerllm_provider_fallbacks_total{from_model="gemini-3.8-flash",to_model="gemini-3.5-flash-lite"} 1.0`. Latency: fallback call 1,353 ms, end-to-end 1,414 ms, so primary failure plus pipeline overhead ≈ 61 ms. A real outage costs the primary's timeout (30 s by default) before the fallback starts · app on `LLM_PROVIDER=gemini LLM_FALLBACK_PROVIDER=gemini LLM_FALLBACK_MODEL=gemini-3.5-flash-lite LLM_TIMEOUT_S=0.001 LLM_FALLBACK_TIMEOUT_S=30`, one `POST /v1/summarize`, then `usage_ledger`, `budget_periods` and `/metrics` read back (1 real call) · 8 Oct · Sai Venkatesh.

## Trade-offs ("we chose X over Y because Z")
- Postgres-only over Redis for counters/budgets — one atomic UPDATE suffices under 100 rps; one fewer service.
- Reserve-then-settle over check-then-call — bursts cannot overspend; cost: pessimistic refusals near the limit.
- Integer micro-USD over float — exact sums, atomic increments.
- Fixed window over token bucket or sliding log — one upsert per request and no per-request history; cost: up to 2× rpm across a boundary (measured above).
- Free cache hits booked as zero-cost ledger rows over billing hits — the tenant gets an honest request count and pays only for model work (D16).
- In-process SSRF validation of every resolved address and redirect hop over an egress proxy — no extra service on the free tier; cost: a DNS-rebinding window between check and connect (D18).
- One fallback to a second model over retrying the same model — free-tier quotas and 503 bursts are per model, so the second model is usually up; cost: the answer may come from a smaller model, and latency can reach the sum of both timeouts (D19).

## Failure modes (and the handling)
- Provider failure → 502, reservation released, nothing billed, audit event (`test_upstream_failure_releases_reservation_and_audits`).
- Duplicate request in flight with the same Idempotency-Key → 409 `idempotency_in_progress` + Retry-After; a failed request releases the key (`test_duplicate_in_flight_gets_409_in_progress`, `test_failure_clears_pending_so_retry_runs`).
- Reasoning model spends the whole output budget → `finish_reason: length` with no text → non-retryable `ProviderError` → 502 `upstream_error`, reservation released, nothing billed; the platform absorbs whatever the provider charged (`test_exhausted_budget_returns_502_and_bills_nothing`).
- Two identical cache misses at once → the cache write is an upsert, so neither request fails (`cache.store`).
- Eval judge replies with bad JSON or out-of-range scores → one retry, then `judge_error`, excluded from the means; > 10 % judge errors fails the gate (`tests/test_eval_runner.py`).
- Free-tier 503/429 during the eval → per-model pacing, 15/30/60 s back-off, then that case alone is given up and counted as a judge error (`results/last_gemini.json`, 8 Oct).
- URL pointing at an internal address, directly or through a redirect → 400 `fetch_blocked` before any request to it is sent, logged, nothing billed; DNS rebinding between check and connect is the accepted gap (D18, `tests/test_fetch.py`).
- Primary model rate-limited, overloaded or timing out, with a fallback configured → one call to the fallback model if it is on the tenant's plan, billed at that model's list price, `usage.fallback_from` set, `ledgerllm_provider_fallbacks_total` +1; both fail → 502, reservation released, nothing billed (D19, `tests/test_fallback.py`, real fallback above).
