# Work allocation

One owner per area of the system, listed in pipeline order. Each person's full list of work is below. A detailed brief per
area (implementation guidance, acceptance criteria) is shared with each owner separately. If you would rather swap an area
with someone, say so in the group chat.

Shared files (`app/schemas.py`, `app/models.py`, `app/errors.py`, `app/api/summarize.py`) may be changed by anyone with a
`CONTRACT CHANGE:` line in the PR; the owner of the affected stage reviews (see `CONTRIBUTING.md`).

---

## Loukik — identity, admin, compliance (`app/auth/`, `app/api/admin.py`, `app/compliance/`, `evals/redaction/`)
- PII redaction patterns: credit card (Luhn-validated), Aadhaar, PAN, secret tokens (incl. `AIza…` and `llk_…`), IPv4; ordered specific → generic; cases and negatives in `evals/redaction/cases.jsonl`; `redact_dict()` applied to audit details. Makes `tests/test_redaction.py` fully green.
- Admin API: list a tenant's keys; revoke a key; rotate a key; `PATCH /admin/tenants/{id}` for plan, status (suspended → 403) and budget override; paginated `GET /admin/audit` with filters. Tests in `tests/test_admin.py`.
- Audit policy for the two refusal paths that are not audited yet (`model_not_allowed`, `fetch_failed`); documented in `app/compliance/audit.py`.
- Tenant isolation tests (`tests/test_isolation.py`): usage, feedback, idempotency keys and admin routes cannot cross tenants.
- Numbers: redaction cases passing per type, redaction latency per KB, audit events per refusal type.
- README section "Security & compliance". Slides 1–2 (business objective, ML problem, requirements, scope) and slide 6 (identity & compliance). The project summary line for the README (2–3 sentences, resume style).
- Optional: request-log retention; per-key scopes; batching the `last_used_at` write.

## Suraj — traffic control, response cache, routing, deployment, CI (`app/traffic/`, `Dockerfile`, `render.yaml`, `docker-compose.yml`, `.github/`)
- Rate limiter: fixed 60 s window per key with one atomic upsert on `rate_limit_windows`; correct `X-RateLimit-*` and `Retry-After`; pruning of old windows. Makes `tests/test_ratelimit.py` green plus per-key and window-reset tests.
- Idempotency: 24 h TTL with cleanup; in-flight duplicate requests get 409 `idempotency_in_progress` via a pending record; failures clear the pending record.
- Response cache: `response_cache` table behind the already-wired `lookup`/`store` hooks; TTL per plan; `ledgerllm_cache_total{result}` metric; billing policy for hits written up as decision D16. Tests in `tests/test_cache.py`.
- Plan-aware model routing: `default_model` per plan in `config/plans.yaml`, used when a request names no model. Tests in `tests/test_routing.py`.
- Deployment: Neon Postgres, Render web service from `render.yaml`, production tenants seeded, live URL in the README; Hugging Face Spaces as fallback.
- Repository: GitHub repo, `LLM_API_KEY` secret, branch protection on the four CI jobs, review of `CONTRACT CHANGE:` pull requests.
- Numbers: limiter microseconds per check, window-edge burst vs rpm, cache hit rate and dollars saved at list price, cold-start seconds.
- README section "Rate limits, idempotency, cache" with the live URL. Slide 5 (traffic control, cache, CI → deploy → rollback).
- Optional: tokens-per-minute limit; semantic cache.

## Sai Venkatesh Alampally — feature, model providers, quality evaluation (`app/feature/`, `app/llm/`, `prompts/`, `evals/summarization/`)
- SSRF guard: scheme allow-list, resolve and reject private/loopback/link-local/metadata/IPv6-ULA addresses, redirect re-validation, `FetchBlocked → 400 fetch_blocked`. Makes `tests/test_fetch.py` green plus unit tests per address class.
- Long documents: head 70 % + tail 30 % truncation with a marker; map-reduce summarization for pro/enterprise when the text exceeds the plan limit, every call booked to the ledger.
- Model providers: `FallbackProvider` (primary Gemini → secondary such as Groq on 429/5xx/timeout) with `LLM_FALLBACK_*` config, provider recorded on the result, fallback metric; list price of any new model added to `config/prices.yaml`. Tests with `httpx.MockTransport` in `tests/test_fallback.py`.
- Golden set: about 30 hand-written documents (text stored, mixed domains, key points, must-not-include facts and injection canaries).
- LLM judge: strict JSON, paced for the free tier's RPM, results committed with the prompt hash; five hand-scored cases for judge agreement.
- CI evidence: one PR with a deliberately worse prompt that the gate blocks (screenshot for the README); then `summarize_v2.yaml` with the eval diff, merged and enabled through `SUMMARIZE_PROMPT_VERSION`.
- Prompt tests (`tests/test_prompts.py`): braces inside documents are never interpreted; styles map; token cap respects the ceiling.
- Numbers: faithfulness and coverage per prompt version, key-point hit rate, judge agreement, tokens per request, fallback count, extraction yield.
- README sections "Summarization quality" and "Model providers". Slide 7 (feature, SSRF, map-reduce, fallback, golden set, judge, blocked-PR screenshot).
- Optional: exact token counting through the provider; PDF ingestion; a second judge model.

## Naresh — billing correctness and usage API (`app/billing/`, `app/api/usage.py`)
- Concurrency proof on Postgres: 50 parallel requests against a $0.004 budget; the ledger never exceeds the limit, reservations return to zero; runs in the `test-postgres` CI job.
- Soft warning exactly once per period using `budget_periods.soft_warned_at`, emitting the `budget.soft_warning` audit event; removes the pipeline `TODO`.
- Period rollover and mid-month limit changes covered by tests.
- Guardrail cost allowance decision (D15): add a per-plan allowance to the estimate, or document the accepted boundary overshoot.
- Usage API: `by_day`, `last_requests`, `GET /v1/usage/statement.csv`; `estimate_microusd` recorded on completion ledger rows.
- Numbers: cost per request at list price by style, reservation pessimism (estimate ÷ actual), burst admitted vs 402 and spent vs limit, share of spend that is guardrail cost.
- README section "Cost attribution & budgets". Slides 3–4 (architecture; billing). The shared slide deck (file, template, timer).
- Optional: soft-warning webhook stub; admin top-up endpoint.

## Thrishal — guardrails, red-team evaluation, online quality sampling (`app/guardrails/`, `app/quality/`, `evals/redteam/`)
- Second detection layer: cascaded LLM classifier on `gemini-3.5-flash-lite` behind `GUARDRAIL_LLM`, JSON verdict with model and token counts so the cost is billed, thresholds per source, in-process verdict cache; keyword rules become scored signals (fixes the `ben-004` false positive). Makes `tests/test_guardrails.py` green.
- Output moderation: PII leak via the redaction module, canary and foreign-URL checks, toxicity list; redact-and-return vs withhold policy documented.
- Red-team set: about 50 attacks (direct, indirect in documents, persona, obfuscation, multilingual) and 50 benign look-alikes; thresholds raised to catch ≥ 0.9 and FPR ≤ 0.05 once met.
- Shadow report (`evals/redteam/shadow_report.py`) over `guardrail.shadow_block` audit events.
- Online quality sampling (`app/quality/online_judge.py`): background judge on a sample of live summaries, `quality_samples` table, `ledgerllm_quality_score{prompt_version}` metric, `purpose="judge"` ledger rows not settled against tenants (D17), `GET /admin/quality`. Tests in `tests/test_online_judge.py`.
- Numbers: catch rate and FPR for heuristics-only vs cascade, added p50/p99 latency, classifier cost per 1,000 requests, share blocked by source, sampled quality mean per prompt version.
- README sections "Abuse protection" and "Online quality monitoring". Slide 8 (layered detection, shadow rollout, who pays, quality per prompt version).
- Optional: per-plan guardrail policy; secret-leak check on outputs; drift alert on quality mean.

## Yashraj — dashboards, metrics, load evidence (`app/observability/`, `app/api/dashboard.py`, `app/api/feedback.py`, `ops/`, `loadtest/`)
- Grafana dashboard JSON in `ops/grafana/dashboards/` with panels for requests by status, p50/p99, model latency, cost by tenant, tokens, rejections by reason, guardrail verdicts, feedback ratio, upstream errors, cache hit rate; rows labelled by monitoring category; screenshot.
- Tenant dashboard page (`/dashboard`): budget gauge, cost by day, by-model and by-purpose tables, last requests, warning banner, statement download; screenshot.
- Burst load test on Postgres with `MOCK_LATENCY_MS=800`, then against the deployment after warming it; `verify_quota.py` result; `loadtest/README.md` with commands and the result table.
- Real-provider latency sample (`loadtest/latency_sample.py`): 30 paced requests at three document sizes, end-to-end and model-only p50/p99.
- Feedback metric labelled by `prompt_version`; `tests/test_metrics.py`; `tenant_id` and `error_code` on the request log line.
- Numbers: p50/p95/p99 steady, platform overhead p99, req/s, burst 200/402/429 counts, quota verification, real-provider p50/p99 by size, metric cardinality.
- README benchmarks table filled from `NUMBERS.md`; dashboard screenshots; link to `loadtest/README.md`. Slides 9–10 (observability, load numbers, mock-vs-real latency; what breaks at 10×, next steps, live URL).
- Optional: Langfuse tracing; Prometheus alert rules.
