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
multi-region deployment. Stretch: PDF ingestion, semantic cache, Langfuse tracing.

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
          ④ acquire content: fetch URL (SSRF guard) → extract → fit (D20)  422 / 400
          ④½ exact-match response cache (per tenant) — a hit returns here  200, cost 0
          ⑤ estimate worst-case cost → ATOMIC RESERVE on budget_periods    402
          ⑥ input guardrail: instructions, then document                   400 blocked_input (reservation released)
          ⑦ LLM call(s); versioned prompt; document wrapped as data        502 upstream_error (reservation released)
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
billable call: purpose, model, tokens, cost, reservation estimate, price_version, prompt_version, latency) · `idempotency_records` ·
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
| D15 | Input guardrail = weighted regex signals (noisy-OR) with a cascaded Flash-Lite JSON classifier for scores in the 0.3–0.9 band, behind `GUARDRAIL_LLM` | Always-on model classifier; or regex only | Heuristics are sub-millisecond and free, so the paid call is only made where they are unsure; the classifier's tokens are billed to the tenant; cached by text hash; a classifier outage degrades to heuristics, never to a refusal | Two detection paths to evaluate (the red-team eval reports which configuration it measured) |
| D16 | Exact-match response cache per tenant, checked after content is acquired and before the budget reserve; a hit is free and booked as a zero-cost ledger row with status `cached`; TTL per plan; anything a guardrail flagged (even in shadow mode) is never stored; `Cache-Control: no-cache` bypasses it and `RESPONSE_CACHE_ENABLED=false` switches it off | Bill hits at full or a flat fee; cache before fetching; semantic cache | The tenant asked for nothing new and we spent nothing, so charging would be rent; keying on the extracted text, model and prompt hash means a changed page, model or prompt can never serve a stale summary; a zero-cost row keeps request counts honest while token totals still match provider usage | Hits on URL requests still pay for the fetch; no cross-tenant sharing, so popular pages are summarised once per tenant; storage grows with TTL |
| D17 | Online quality sampling: 5 % of live summaries judged on a background thread; judge calls are ledger rows with `purpose="judge"` under the tenant for attribution but **not settled against the tenant's budget** | Bill the judge to the tenant; or judge synchronously; or offline eval only | The tenant did not ask for the judge, so it is a platform cost — like monitoring. Async keeps p99 unchanged; a single paced worker stays inside the free-tier rate limit. The per-prompt-version mean on live traffic is the online half of the evaluation story (offline = golden set) | A judge outage silently loses samples; a `quality_samples` table and one more ledger purpose to explain in the usage API |
| D18 | SSRF guard in process: validate the scheme, refuse credentials, resolve the host and refuse the URL if any address is not globally routable; follow redirects manually (max 3) and validate every hop | Egress proxy (e.g. Smokescreen) or a destination allow-list | No extra infrastructure on the free tier and nothing to operate; an allow-list would defeat "summarize any URL"; about 6.5 µs per check plus the DNS lookup | DNS rebinding: httpx resolves the name again when it connects, so a hostile resolver can switch to an internal address in between. Next step is connecting to the validated IP with the original Host header and SNI (no infrastructure, closes the window); an egress proxy or deny-RFC 1918 network policy is the production answer at scale |
| D19 | Fall back once on retryable errors (429, 5xx, timeout, network) to a configured secondary model (`LLM_FALLBACK_*`; default chain `gemini-3.8-flash` → `gemini-3.5-flash-lite` on one key, or another vendor such as Groq); only if the fallback model is on the tenant's plan; bill the model that answered at its list price, keyed by the configured name; reserve the max of the chain's estimates | Retry the same model with backoff; no fallback | Free-tier 429s are per model and 503 "high demand" bursts are per model too, so retrying the same model mostly hits the same wall, while a second model is usually available: a fallback turns an outage into a slower answer. Reserving the chain's max keeps D2's guarantee (no request settles above its reservation) even when the fallback is the pricier model. A 400 or an exhausted output budget fails the same way on any model, so it never falls back | A fallback answer can differ in quality from the requested model (flash-lite is a smaller model; `usage.model` and `usage.fallback_from` say so, and the eval scores the primary only); latency can reach about the sum of the two timeouts; the reservation is pessimistic by the price gap when the fallback is dearer; a fallback answer is cached under the request's cache key for the plan's TTL |
| D20 | Long documents: head+tail truncation for every plan (70 % head, 30 % tail, cut on whitespace, `[… N characters omitted …]` marker, never above the limit); map-reduce for paid plans up to a per-plan ceiling (`map_reduce_max_chars`: pro 240k, enterprise 600k, free off): paragraph-aligned chunks of `max_input_chars` with ~2 % or one-paragraph overlap, each summarised as bullets with the same versioned prompt, then the bullets summarised into the requested style inside `<document>`; text above the ceiling is head+tail-truncated to it first. One up-front reservation for the worst case of every call (Σ over calls of the chain's max estimate at each call's output cap), one ledger row per call (`purpose=completion`, stage in `prompt_version`: `#map:2/5`, `#reduce`), settle the sum. Any call fails → release everything, 502, nothing billed; the tokens already spent are logged as absorbed (`map_reduce_failed_absorbed`). `usage.model` is the requested model unless every call fell back; `usage.fallback_from` is set if any did; `source.strategy` says which path ran | Truncate the head (`text[:limit]`); map-reduce for everyone; a long-context model | Endings carry conclusions, so the head alone loses what readers want most; map-reduce reads the whole document, but each chunk is a full-price call, so it is a paid-plan feature whose cost is bounded by the ceiling and reserved up front, which keeps D2's guarantee (no request settles above its reservation). A long-context model would price a 600k-character page as one call at the dearest rates, and the free tier has no such model; free stays one call, so its spend per request does not change | N+1 calls run one after another (free-tier rpm), so latency grows with the chunk count (about 4 calls for a 140k-character chapter, measured in MEASUREMENTS.md); chunks lose cross-chunk context (the overlap softens it at boundaries only); the reservation is pessimistic (every call at its output cap, the reduce input at the sum of map caps), so a tenant near its limit can be refused a request that would have fit; a failure in the last call throws away the work of the earlier ones, which the platform pays for |
| D21 | Summary-quality eval on **Groq's free tier**: `qwen/qwen3.8-27b` summarises through the production prompt and pipeline, `openai/gpt-oss-120b` judges (`reasoning_effort` low, its own provider instance so the summarizer sends none); strict JSON with a 1/3/5 rubric and one retry, cases with no valid judgement excluded from the means but gated at ≤ 10 %; calls paced per model by requests (8 rpm) and by a 60 s token window (7,000 of Groq's 8,000 tokens/min, each call charged prompt + max_tokens, as Groq does), back-off that follows the server's Retry-After (else 15/30/60 s); the full 30 cases run locally with results committed, an 8-case `ci` subset gates PRs; plus a human calibration set (`agreement.py`); the mock run gates plumbing only. Gemini (flash summarises, flash-lite judges) remains selectable with `--provider gemini` | Gemini only (flash + flash-lite, the original choice); programmatic checks only; a paid judge; human eval only | Zero cost: Gemini's free tier allows 20 requests/day on gemini-3.8-flash, so a 30-case judged run (60 calls) can never finish there; Groq allows 1,000/day. The judge is a larger model from a different family than the summarizer, which reduces self-preference bias more than a same-family sibling did. Keyword checks cannot see invented facts; human-only scoring cannot gate every PR. Calibration says how far the judge can be trusted | The eval measures the **prompt and pipeline on Qwen, not the production model** (`DEFAULT_MODEL` is still Gemini), so a Gemini-specific regression would not show; a paid setup would summarise with the production model itself and judge with a stronger model from another vendor (or two judges, reporting disagreement). Throughput is bounded by 8,000 tokens/min per model: the 30 cases take ~21 min, which is why CI runs the 8-case subset (~4 min); the subset can miss a regression on the other 22 cases; one judge, calibrated only on the rows Sai scores by hand (it penalised one summary for leaving out an injected instruction, see MEASUREMENTS.md) |
| D22 | Output moderation: canary / foreign-URL / toxicity → **withhold**; PII → **redact and return** | Withhold everything; or redact everything | A canary or an invented link proves the model obeyed the document, so the whole summary is untrusted. PII in a summary is usually faithful to the source, the tenant already paid for the completion, and the request log is redacted anyway — serving `[EMAIL]` keeps the response useful | Redacted summaries are not cached; one extra branch in the pipeline |
| D23 | Guardrail-classifier tokens are billed (own ledger row, added to `spent`) but not included in the reservation | Add a per-plan `guardrail_allowance_usd` to every reservation | The model classifier (D15) is off by default and, when on, runs only on inputs the heuristics are unsure about, cached by text hash; an allowance on every request would refuse requests near the limit to cover spend that is usually zero (measured: 0 % of spend with `GUARDRAIL_LLM=off`); the completion — the large cost — always stays inside its reservation | With the classifier on, `spent` can exceed the hard limit by at most two classifier calls (instructions + document, ≤ $0.0011 at list price) per request in flight at the boundary; once over, every further request is refused. Revisit (add the allowance) if the guardrail share of spend becomes material |

**Failure modes.**
| Failure | Behaviour | Evidence |
|---|---|---|
| Provider timeout / 5xx / 429 | 502 `upstream_error` + `Retry-After`; reservation released; nothing billed; audit event | `test_upstream_failure_releases_reservation_and_audits` |
| Provider outage or per-model quota exhausted, fallback configured (D19) | Primary 429 / 5xx / timeout → one call to the fallback model (if on the tenant's plan) → 200 billed at the fallback's list price with `usage.fallback_from`, `llm_fallback` log line, `ledgerllm_provider_fallbacks_total` +1. Both fail → 502 `upstream_error`, reservation released, nothing billed. Fallback not on the plan → 502 as without a chain | `tests/test_fallback.py`; MEASUREMENTS.md fallback entry (8 Oct) |
| Burst exceeding the budget | Exactly the requests that fit are admitted; the rest get 402 | `test_hard_cutoff_returns_402_and_never_overspends`; 50-way concurrent burst on Postgres `test_burst_of_concurrent_requests_cannot_overspend`; `scripts/bench_billing.py burst`; Locust + `verify_quota.py` |
| Database unavailable | Fail closed: no reservation → no model call → 500 | by construction (reserve precedes the call) |
| Hostile URL (internal address, redirect to one, huge page, slow host) | Every resolved address of the URL and of every redirect hop must be globally routable (no private, loopback, link-local, CGNAT, ULA, multicast, metadata; IPv4-mapped IPv6 unwrapped) → 400 `fetch_blocked`, logged, nothing billed; at most 3 redirects; body streamed and cut at 2 MB; 10 s timeout. Not covered: DNS rebinding between check and connect (D18) | `tests/test_fetch.py` (`test_non_public_addresses_are_blocked`, `test_redirect_to_metadata_is_blocked_at_the_hop`, `test_body_is_cut_at_the_byte_cap_without_reading_the_rest`, `test_ssrf_targets_are_blocked`) |
| Guardrail false positive | Shadow mode for rollout; false-positive-rate gate in CI; feedback endpoint | `evals/redteam`, `test_shadow_mode_records_but_does_not_block` |
| Model follows an injected instruction | Prompt treats the document as data; output moderation withholds | `evals/summarization` leak checks |
| Price change | New `prices.yaml` version; old rows keep the old version | ledger `price_version` |
| Key leak | Revoke; `last_used_at`; audit trail | `api_keys.revoked_at` |
| A map or reduce call fails mid-way through a long document (D20) | Whole reservation released; 502 `upstream_error` naming the stage (`map:2/5: …`); nothing billed, no ledger rows; the tokens of the calls that had answered are logged as `map_reduce_failed_absorbed` (platform cost); each call may still fall back on its own before that (D19) | `test_failure_in_the_second_map_call_bills_nothing` |
| Very long page on a paid plan | Head+tail-truncated to the plan's `map_reduce_max_chars` before chunking, so calls and cost per request stay bounded; `source.truncated` true | `test_text_above_the_ceiling_is_truncated_then_map_reduced` |
| Runaway output | `max_tokens` derived from `max_words`, capped by the prompt artifact | `output_token_cap` |
| Reasoning model spends the output budget → empty text | Gemini preset sends `reasoning_effort=low` (`LLM_REASONING_EFFORT`); hidden reasoning tokens are billed as output so the ledger matches the provider; `finish_reason: length` with empty text → non-retryable `ProviderError` → 502 `upstream_error`, reservation released, nothing billed (the platform absorbs any provider charge) | `test_exhausted_budget_returns_502_and_bills_nothing`; MEASUREMENTS.md reasoning table |
| Document tries to close the data wrapper / placeholder smuggling | Prompt rendered in one regex pass, so a `{text}` or `{instructions_block}` inside the instructions, title or page is inserted verbatim, never expanded; `<document` / `</document` in any untrusted value is rewritten to `<\document` / `<\/document`, so the rendered prompt has exactly one wrapper. Template and content hash unchanged | `tests/test_prompts.py` |
| Eval judge replies with prose, bad JSON or out-of-range scores | One retry that names the rejection; then the case is a `judge_error`, left out of the means; more than 10 % judge errors fails the gate, so a broken judge cannot pass | `tests/test_eval_runner.py` |
| Free-tier 429 / 503 during the eval | Calls paced per model by requests and by estimated tokens per minute; back-off waits the server's Retry-After (else 15 → 30 → 60 s), and a Retry-After above 2 min (a daily quota) gives up at once; then that case alone is given up and counts as a judge error | `test_backoff_*`, `test_tpm_*`; `results/last_gemini.json` (2 of 3 cases given up on 8 Oct); `results/last_groq.json` (30 cases, 0 retries needed, 8 Oct) |

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
| Quality | feedback ratio per prompt version; offline eval scores per prompt hash; async judge scores on a 5 % sample of live summaries, per prompt version (`ledgerllm_quality_score`, `GET /admin/quality`) | `feedback`, `evals/`, CI artefacts |
| Drift | cost and tokens per request per tenant and model over time; guardrail block-rate trend; sampled quality mean per prompt version, last 24 h against the week before (`GET /admin/quality` → `drift`, flagged at a 0.5-point drop over ≥ 10 samples) | `/admin/quality`; Prometheus rules for cost and block rate (stretch) |
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
