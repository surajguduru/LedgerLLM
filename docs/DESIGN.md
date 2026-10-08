# LedgerLLM — Design

Status: v0.1 base. This document explains what the system does, how it is built, which decisions were
made and why, how it fails, and how it is operated.

---

## 1. Problem statement and requirements

**Objective.** Let a SaaS vendor sell an LLM feature ("summarize any URL") to many customers through one API,
while guaranteeing that no customer can spend more than their plan allows, every token is billed to the right
customer, and abusive inputs never reach the model.

**The ML task.** Input: a public URL *or* raw text (bounded by the plan's input limit), optional focus instructions
and a style. Output: a faithful summary of at most `max_words` words plus usage and billing metadata. Quality target:
a judge rates the summary ≥ 4/5 on faithfulness (no invented facts) and ≥ 3.5/5 on coverage of key points. The LLM
is a dependency; the system around it — metering, quotas, safety, audit — is the product.

**Functional requirements.**
| # | Requirement |
|---|---|
| F1 | API-key authentication; keys belong to tenants; tenants have a plan (free / pro / enterprise) |
| F2 | Tiered rate limits per key (requests per minute by plan) with `X-RateLimit-*` and `Retry-After` headers |
| F3 | Monthly cost budget per tenant: hard cutoff (HTTP 402) and soft warning (header + audit event at 80 %) |
| F4 | Cost attribution: every model call (completion and guardrail classifier) booked as a ledger row with tokens, model, price version, prompt version |
| F5 | Per-tenant usage endpoint and billing dashboard |
| F6 | Abuse protection: prompt-injection / jailbreak classifier on instructions and fetched content; output moderation; `enforce` / `shadow` / `off` |
| F7 | Idempotency keys: identical requests replay; reuse with a different body is rejected (409) |
| F8 | Request/response logging with PII redaction; structured audit log of every refusal and admin action |
| F9 | Feedback (thumbs up/down) per request as an online quality signal |
| F10 | Admin API for tenants and keys |

**Non-functional requirements (targets; measured values live in the README).**
| Requirement | Target | Measurement |
|---|---|---|
| End-to-end latency, ~3k-token document, Gemini 3.8 Flash | p50 ≤ 3 s, p99 ≤ 8 s | Locust steady run, Prometheus histogram |
| Platform overhead (auth, quota, guardrail heuristics; no model call) | p99 ≤ 25 ms | Locust with the mock provider |
| Quota correctness under burst | zero requests admitted beyond the hard limit; ledger total ≤ limit | `loadtest/verify_quota.py` after a 50-user burst |
| Throughput, single free-tier instance, mock provider | ≥ 50 req/s | Locust |
| Guardrail quality on the red-team set | catch rate ≥ 90 %, false-positive rate ≤ 5 % | `evals/redteam` in CI |
| Summary quality | faithfulness ≥ 4.0, coverage ≥ 3.5 (LLM judge, 1–5) | `evals/summarization` in CI |
| Cost per request at list price, Gemini 3.8 Flash, 3k-token document | ≤ $0.004 | ledger |
| Development and evaluation API spend | $0 (Gemini free tier) — fall back to ≤ $20 if a paid key is needed | provider console |

**Scope.** In: all of the above, a public deployment, CI eval gates, a Grafana dashboard, load-test evidence.
Out: end-user accounts and OAuth (API keys only), card payments (we meter and bill in a ledger), streaming responses,
multi-region deployment. Stretch: PDF ingestion, semantic cache, async judge sampling, Langfuse tracing.

---

## 2. Architecture

### 2.1 Components
| Component | Choice | Why this and not the alternative |
|---|---|---|
| API server | FastAPI, Python 3.12, single process | Team fluency; pydantic gives validated contracts and OpenAPI for free; the model call dominates latency so an async stack buys little here |
| State | Postgres only (Neon in production; SQLite in tests and local dev) | One atomic `UPDATE … WHERE` is correct and sufficient for budgets and rate counters below ~100 req/s. Avoids operating a second datastore for a small team. See D1 |
| LLM | Gemini 3.8 Flash through its OpenAI-compatible endpoint (plain `httpx`), behind a `LLMProvider` protocol; Anthropic via its SDK as an alternative; a `MockProvider` for tests, CI and load tests | Gemini Flash has a permanent free tier, so development costs nothing; one OpenAI-compatible adapter also covers Groq, OpenAI, OpenRouter and Ollama; the protocol keeps the system provider-agnostic; the mock makes CI free and deterministic |
| Content extraction | `httpx` + `trafilatura` | Strong boilerplate removal, pure Python |
| Guardrails | Layered: regex heuristics → model-based classifier → output moderation; shadow mode | Heuristics are free and sub-millisecond; the second layer catches paraphrases; shadow mode measures false positives on live traffic before anyone is blocked |
| Metering | Integer micro-USD ledger + versioned `prices.yaml` | Exact sums, atomic increments, reproducible bills. See D5 |
| Observability | structlog JSON → stdout; Prometheus `/metrics`; Grafana; request and audit tables | Standard and free; no SaaS dependency |
| Evaluation | Hand-written red-team, golden summarization and redaction sets; runners in `evals/`, gates in GitHub Actions | A pull request blocked by an eval regression is the proof that the gate works |
| Load test | Locust | Python, scriptable assertions on status-code distribution |
| Deployment | Docker → Render web service (`render.yaml`) + Neon Postgres | Free tier, public URL, environment-variable configuration, one-click redeploy of a previous build |

### 2.2 Request pipeline — `POST /v1/summarize`, synchronous, JSON over HTTPS
```
client ─▶ ① auth: X-API-Key → tenant, plan                               401 / 403
          ② idempotency replay (tenant, Idempotency-Key)                  200 replay / 409
          ③ rate limit: per key, fixed 60 s window, plan.rpm              429 + Retry-After
          ④ acquire content: fetch URL (SSRF guard) → extract → truncate   422 / 400
          ④½ exact-match response cache (per tenant) — a hit returns here  200, cost 0
          ⑤ estimate worst-case cost → ATOMIC RESERVE on budget_periods    402
          ⑥ input guardrail: instructions, then document                   400 blocked_input (reservation released)
          ⑦ LLM call; versioned prompt; document wrapped as data           502 upstream_error (reservation released)
          ⑧ output moderation                                              200 with the summary withheld if blocked
          ⑨ SETTLE actual cost; ledger row(s); metrics
          ⑩ redacted request log; audit; store idempotent + cached response; sample for the online judge ─▶ client
```
Every error has the shape `{"error": {"code", "message", "request_id"}}`. Every response carries `X-Request-ID`,
`X-RateLimit-*`, `X-Budget-Limit-USD`, `X-Budget-Spent-USD`, and `X-Budget-Warning` once past 80 %.

### 2.3 Data model
All money is integer micro-USD. All IDs are UUID strings. Every table holding tenant data carries `tenant_id`.

`tenants` · `api_keys` (sha256 of the key, display prefix) · `rate_limit_windows` (key, minute → count) ·
`budget_periods` (tenant, YYYY-MM → hard_limit, spent, reserved, soft_warned_at) · `usage_ledger` (one row per
billable call: purpose, model, tokens, cost, price_version, prompt_version, latency) · `idempotency_records` ·
`audit_events` · `request_logs` (redacted) · `feedback` · `response_cache` (tenant, key → summary, model, prompt version, tokens, hits, expires_at).

### 2.4 Deployment
```
GitHub main ── CI: ruff · pytest (SQLite + Postgres) · red-team gate · summarization gate ──▶ Render (Docker, 1 instance)
                                                                                              ├─▶ Neon Postgres
                                                                                              └─▶ Gemini API (OpenAI-compatible endpoint)
Local: docker compose = app + postgres + prometheus + grafana
```

---

## 3. Design decisions and failure modes

| # | Decision | Alternative rejected | Reason | Cost accepted |
|---|---|---|---|---|
| D1 | Postgres for budgets **and** rate counters | Redis token bucket | One atomic UPDATE is correct and sufficient below ~100 req/s; one datastore to operate | Hot counters move to Redis at 10× |
| D2 | Reserve-then-settle budget accounting | Check `spent < limit`, then call | N concurrent requests all read the same stale `spent` and all pass — overspend by N. Reservation is atomic, admission exact | Pessimistic: a tenant at 99 % may be refused a request that would have fit |
| D3 | Fixed 60 s window limiter | Sliding log / token bucket | One upsert, no per-request history | Up to 2× rpm across a window boundary |
| D4 | Pre-designed schema, `create_all`, no migration tool | Alembic | Six people committing in parallel; migration-head conflicts are the top integration risk for a short project | No in-place production migrations |
| D5 | Integer micro-USD | float / Decimal | Exact sums, atomic `+=`; `tokens × $/MTok` is already micro-USD | Convert at the presentation layer; BIGINT columns |
| D6 | Pipeline order: cheap checks → reserve → guardrail → LLM | Guardrail first | Abusive or over-quota traffic must never cost a model call; reserving before the guardrail lets classifier tokens be billed to the tenant | A guardrail block releases a reservation (one extra write) |
| D7 | 402 for budget exhaustion, 429 for rate limits | 429 for both | Clients act differently (back off vs upgrade); dashboards and load tests separate them | Non-standard (Stripe-style) use of 402 |
| D8 | Load test with the mock provider; real-provider latency on a small sample | Load test against the real model | The claim under test is our quota logic and overhead, not the provider's latency; spend budget | Two numbers to report instead of one |
| D9 | Layered guardrails with shadow mode | Single model classifier, enforce only | Heuristics are free; shadow mode measures false positives before blocking | More code paths to test |
| D10 | Gemini 3.8 Flash (free tier) as default model and judge; tenants billed at **list** price from versioned YAML | Paid Anthropic/OpenAI key; prices in code | Zero development cost; bills independent of which tier the platform runs on; reproducible when prices change | Free-tier content may be used by Google to improve its products (unacceptable for real customer data — a paid tier would be used in production); judge and system share a model family (noted bias); free-tier rate limits cap eval throughput |
| D11 | Prompt as versioned YAML with a content hash; document wrapped in `<document>` and declared data | Prompt in code | Indirect-injection defence at the prompt layer; every ledger row says which prompt produced it; rollback is an env flip | Prompt change needs a config change |
| D12 | API keys stored as sha256; prefix for display | Plaintext or reversible encryption | A database leak does not leak keys | Keys are shown once |
| D13 | Idempotency scoped per tenant; body-hash mismatch → 409; only 200s stored | Global keys; store errors | Tenant isolation; failed calls should be retried, not replayed | Clients need a new key for a changed body |
| D14 | Dashboard served by the API | Separate Streamlit app | One deployable, one URL | Minimal UI |
| D16 | Exact-match response cache per tenant, checked after content is acquired and before the budget reserve; a hit is free and booked as a zero-cost ledger row with status `cached`; TTL per plan; anything a guardrail flagged (even in shadow mode) is never stored; `Cache-Control: no-cache` bypasses it and `RESPONSE_CACHE_ENABLED=false` switches it off | Bill hits at full or a flat fee; cache before fetching; semantic cache | The tenant asked for nothing new and we spent nothing, so charging would be rent; keying on the extracted text, model and prompt hash means a changed page, model or prompt can never serve a stale summary; a zero-cost row keeps request counts honest while token totals still match provider usage | Hits on URL requests still pay for the fetch; no cross-tenant sharing, so popular pages are summarised once per tenant; storage grows with TTL |
| D21 | Summary-quality eval: LLM judge on a different model of the same family (gemini-3.8-flash summarises, gemini-3.5-flash-lite judges), strict JSON with a 1/3/5 rubric and one retry, cases with no valid judgement excluded from the means but gated at ≤ 10 %, calls paced per model (8 rpm) with 15/30/60 s back-off, plus a human calibration set (`agreement.py`); the mock run gates plumbing only | Programmatic checks only; a judge from another vendor; human eval only | Zero cost and repeatable in CI: both models are on the free tier with separate quotas, and a different model reduces self-preference. Keyword checks cannot see invented facts; another vendor needs a paid key; human-only scoring cannot gate every PR. Calibration says how far the judge can be trusted | Same-family bias remains; free-tier throughput (about 10 rpm and a daily cap per model, plus 503s at peak) caps the set size and can fail a run for capacity, not quality; the judge is calibrated only on the rows Sai scores by hand |

**Failure modes.**
| Failure | Behaviour | Evidence |
|---|---|---|
| Provider timeout / 5xx / 429 | 502 `upstream_error` + `Retry-After`; reservation released; nothing billed; audit event | `test_upstream_failure_releases_reservation_and_audits` |
| Burst exceeding the budget | Exactly the requests that fit are admitted; the rest get 402 | `test_hard_cutoff_returns_402_and_never_overspends`; Locust + `verify_quota.py` |
| Database unavailable | Fail closed: no reservation → no model call → 500 | by construction (reserve precedes the call) |
| Hostile URL (internal address, huge page, slow host) | SSRF guard → 400 `fetch_blocked`; 2 MB cap; 10 s timeout | `tests/test_fetch.py` |
| Guardrail false positive | Shadow mode for rollout; false-positive-rate gate in CI; feedback endpoint | `evals/redteam`, `test_shadow_mode_records_but_does_not_block` |
| Model follows an injected instruction | Prompt treats the document as data; output moderation withholds | `evals/summarization` leak checks |
| Price change | New `prices.yaml` version; old rows keep the old version | ledger `price_version` |
| Key leak | Revoke; `last_used_at`; audit trail | `api_keys.revoked_at` |
| Runaway output | `max_tokens` derived from `max_words`, capped by the prompt artifact | `output_token_cap` |
| Reasoning model spends the output budget → empty text | Gemini preset sends `reasoning_effort=low` (`LLM_REASONING_EFFORT`); hidden reasoning tokens are billed as output so the ledger matches the provider; `finish_reason: length` with empty text → non-retryable `ProviderError` → 502 `upstream_error`, reservation released, nothing billed (the platform absorbs any provider charge) | `test_exhausted_budget_returns_502_and_bills_nothing`; MEASUREMENTS.md reasoning table |
| Document tries to close the data wrapper / placeholder smuggling | Prompt rendered in one regex pass, so a `{text}` or `{instructions_block}` inside the instructions, title or page is inserted verbatim, never expanded; `<document` / `</document` in any untrusted value is rewritten to `<\document` / `<\/document`, so the rendered prompt has exactly one wrapper. Template and content hash unchanged | `tests/test_prompts.py` |
| Eval judge replies with prose, bad JSON or out-of-range scores | One retry that names the rejection; then the case is a `judge_error`, left out of the means; more than 10 % judge errors fails the gate, so a broken judge cannot pass | `tests/test_eval_runner.py` |
| Free-tier 429 / 503 during the eval | Calls paced per model; 15 → 30 → 60 s back-off; then that case alone is given up and counts as a judge error | `test_backoff_*`; `results/last_gemini.json` (2 of 3 cases given up on 8 Oct) |

---

## 4. Operations

**Deployment and rollout.** Stateless FastAPI container on Render with managed Postgres on Neon. A pull request must
pass lint, the test suite on SQLite and Postgres, the red-team gate and (when prompts or feature code change) the
summarization gate; merging to `main` auto-deploys. A new prompt ships as a new YAML file and goes live by setting
`SUMMARIZE_PROMPT_VERSION`; rollback is setting it back. A new guardrail layer ships in `GUARDRAILS_MODE=shadow`
first, then `enforce`. Code rollback is Render's redeploy-previous. Schema changes are additive only.

**Monitoring.**
| Category | Signals | Source |
|---|---|---|
| Operational | req/s, p50/p99 by endpoint, error rate by `error.code`, upstream errors by retryability, model latency by model | Prometheus / Grafana |
| Input | input size distribution, `blocked_input` rate by category and source (instructions vs document), fetch failure rate, truncation rate | metrics, `request_logs` |
| Output | output tokens, withheld-summary rate, length ratio vs `max_words` | metrics, `request_logs` |
| Quality | feedback ratio per prompt version; offline eval scores per prompt hash; (stretch) async judge scores on a sample | `feedback`, `evals/`, CI artefacts |
| Drift | cost and tokens per request per tenant and model over time; guardrail block-rate trend | Prometheus rules (stretch) |
| Cost | micro-USD per tenant vs budget, soft warnings, 402 count | ledger, dashboard |

**Evaluation.** Offline: the red-team set (catch rate, false-positive rate, latency), the golden summarization set
(programmatic checks on every PR; LLM-judge faithfulness and coverage on prompt changes), redaction cases as tests.
Online: the feedback endpoint, and a sampled asynchronous LLM judge (`app/quality/`) that scores a share of live
summaries without delaying responses and records a quality metric per prompt version.

---

## 5. Scaling limits — what breaks first at 10×
1. **Write amplification**: each request performs four writes (ledger, request log, audit, key `last_used_at`).
   Mitigation: sample or drop `last_used_at`, batch logs through a queue, partition `usage_ledger` by month.
2. **Hot-key counter contention** in `rate_limit_windows` → Redis `INCR` / `EXPIRE`.
3. **Guardrail classifier CPU** on a single instance → separate service or asynchronous pre-filter.
4. **Prometheus label cardinality** on `tenant` → aggregate per tenant from the ledger instead.
5. A single instance is the current limit; horizontal scaling is safe because no state lives in-process.

## 6. Error contract
Codes are stable strings and part of the API; the table lives in the `app/errors.py` docstring.
