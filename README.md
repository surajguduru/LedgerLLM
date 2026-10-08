# LedgerLLM

**A multi-tenant LLM API with per-tenant cost attribution, quotas, abuse protection and audit logging.**

The product feature is deliberately small: *summarize any URL or text*. Everything around that one
LLM call is the point — the parts a SaaS vendor needs before selling an LLM feature to many customers:

- **API keys and plan tiers** (free / pro / enterprise) with per-minute rate limits
- **Monthly cost budgets** per tenant: hard cutoff (HTTP 402) and a soft warning at 80 %, enforced
  atomically with reserve-then-settle accounting so bursts cannot overspend
- **Cost attribution**: every model call — the completion *and* any guardrail classifier call — is a
  ledger row in integer micro-USD with model, tokens, price version and prompt version
- **Per-tenant usage API and billing dashboard**
- **Abuse protection**: prompt-injection / jailbreak detection on user instructions and on fetched
  content, output moderation, and `enforce` / `shadow` / `off` modes for safe rollout
- **Idempotency keys**, an exact-match **response cache**, request/response logs with **PII redaction**, and a structured **audit log**
- **Evaluation gates in CI**: a hand-written red-team set and a golden summarization set block regressions
- **Observability**: Prometheus metrics, Grafana dashboard, structured JSON logs; a Locust burst test
  that proves quota enforcement holds

## Setup

You need **Python 3.11 or newer**, `git`, and `make`. Nothing else — no database, no API key, no Docker.

```bash
python3 --version          # must print 3.11.x or higher. If not: https://www.python.org/downloads/
git clone <repo-url> ledgerllm && cd ledgerllm
make install               # creates .venv and installs dependencies (1–2 minutes)
make test                  # all tests pass; "xfail" lines are planned work, not failures
make dev                   # API on http://localhost:8000 — open /docs for the OpenAPI UI
```

In a second terminal:

```bash
make seed                  # creates demo tenants and prints their API keys
scripts/demo.sh            # summarize text, get a request blocked, read usage
```

That is a complete working system on SQLite with a free offline mock model.

**Real model (free, no card):** get a Gemini API key at <https://aistudio.google.com/apikey>, then

```bash
cp .env.example .env
# edit .env:  LLM_PROVIDER=gemini   LLM_API_KEY=AIza...
make dev
```
Any OpenAI-compatible endpoint works the same way (`LLM_PROVIDER=groq|openai|openrouter|ollama`, or
`openai_compat` with `LLM_BASE_URL`); Anthropic is supported through its official SDK (`LLM_PROVIDER=anthropic`).
Each plan routes to its own default model when a request names none (free → `gemini-3.5-flash-lite`, pro and
enterprise → `gemini-3.8-flash`); `default_model` and `allowed_models` per plan are in `config/plans.yaml`, and
`DEFAULT_MODEL` is the fallback for plans without one.

Provider fallback (D19): set `LLM_FALLBACK_PROVIDER=gemini` and `LLM_FALLBACK_MODEL=gemini-3.5-flash-lite` and a
429, 5xx or timeout on the primary model is retried once on the fallback model (Gemini free-tier quotas are per
model, so the same key works). The tenant is billed for the model that answered, at its list price, and only if
that model is on their plan; `usage.fallback_from` says when it happened. A different vendor works too, e.g.
`LLM_FALLBACK_PROVIDER=groq`, `LLM_FALLBACK_MODEL=llama-3.1-8b-instant`, `LLM_FALLBACK_API_KEY=gsk_...`.

<details>
<summary>Without <code>make</code> (Windows PowerShell)</summary>

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest -q
uvicorn app.main:app --reload --port 8000
python -m scripts.seed
```
</details>

<details>
<summary>Full stack with Postgres, Prometheus and Grafana (Docker)</summary>

```bash
make up        # app :8000, Postgres :5432, Prometheus :9090, Grafana :3000 (admin / admin)
make down
```
</details>

<details>
<summary>Troubleshooting</summary>

- `make: command not found` on macOS → `xcode-select --install`. On Ubuntu → `sudo apt install make`.
- `python3` is older than 3.11 → install a newer one and run `make install PY=python3.12`.
- Port 8000 busy → `.venv/bin/uvicorn app.main:app --reload --port 8001`.
- Stale local DB after schema changes → `rm ledgerllm.db && make seed`.
- `LLM_PROVIDER=gemini requires LLM_API_KEY` → the key is missing from `.env` (or the shell has an older value).
- Gemini free tier returns 429 → you hit its per-minute quota; wait a minute, or use `gemini-3.5-flash-lite`.
</details>

## How a request flows

```
client ─JSON/HTTPS─▶ ① auth (API key → tenant, plan)         ⑥ input guardrail (instructions, then document)
                     ② idempotency replay                     ⑦ LLM call with the versioned prompt
                     ③ rate limit (per key + tenant, /min)   ⑧ output moderation
                     ④ fetch URL + extract text (SSRF-guarded) ⑨ settle actual cost; ledger row; metrics
                     ⑤ estimate cost → ATOMIC budget reserve  ⑩ redacted request log; audit; idempotent store
```
Cheap checks run first, so over-quota or abusive traffic never costs a model call. The budget is reserved
*before* the guardrail so that classifier spend can be billed to the tenant as well.

Full design — components, data model, every design decision with its rejected alternative, failure modes,
deployment and monitoring plan: **[`docs/DESIGN.md`](docs/DESIGN.md)**.

## API

```bash
KEY=$(python3 -c "import json;print(json.load(open('.seed_keys.json'))['pro'])")

curl -s localhost:8000/v1/summarize \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
  -d '{"url":"https://en.wikipedia.org/wiki/Rate_limiting","style":"tldr","max_words":120}'
```
Response (abridged):
```json
{"request_id":"…","summary":"…",
 "source":{"url":"…","title":"…","chars":8120,"truncated":false},
 "usage":{"model":"gemini-3.8-flash","prompt_version":"summarize_v1@ec6822c047b1","price_version":"2026-10-04",
          "input_tokens":2140,"output_tokens":160,"cost_usd":0.002205,"latency_ms":1830,"cached":false},
 "budget":{"period":"2026-10","limit_usd":10.0,"spent_usd":0.002205,"remaining_usd":9.997795,"warning":false},
 "guardrails":{"mode":"enforce","input":{"blocked":false,"method":"heuristic_v1","…":"…"},"output":{"…":"…"}}}
```
Headers on every response: `X-Request-ID`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`,
`X-Budget-Limit-USD`, `X-Budget-Spent-USD`, and `X-Budget-Warning` once past 80 % of the budget.

| Endpoint | Purpose |
|---|---|
| `POST /v1/summarize` | the feature; body: `url` **or** `text`, optional `instructions`, `style`, `max_words`, `model` |
| `GET /v1/usage` | current-month spend, remaining budget, tokens, breakdown by model, purpose and UTC day, last 10 ledger rows |
| `GET /v1/usage/statement.csv?period=YYYY-MM` | itemised statement: every ledger row of the month plus a `TOTAL` line that equals the bill |
| `POST /v1/feedback` | thumbs up/down on a `request_id` |
| `GET /app` | **tenant portal**: sign up / sign in, manage API keys, usage per key, daily and monthly spend |
| `/app/api/*` | portal JSON API (session cookie): `signup`, `login`, `logout`, `me`, `keys` (create, `/{id}/revoke`, `/{id}/rotate`), `models`, `playground/summarize`, `usage?period=`, `statement.csv`, `billing`, `billing/plan` |
| `GET /dashboard` | usage/billing page for a tenant (paste a key) |
| `POST /admin/tenants`, `POST /admin/tenants/{id}/keys`, `GET /admin/tenants` | tenant and key management (`Authorization: Bearer $ADMIN_TOKEN`) |
| `GET /metrics`, `GET /healthz`, `GET /docs` | Prometheus, health, OpenAPI |

Errors always look like `{"error": {"code": "…", "message": "…", "request_id": "…"}}` with stable codes:
`401 missing_api_key | invalid_api_key` · `403 tenant_suspended | model_not_allowed` · `429 rate_limited` ·
`402 budget_exceeded` · `400 blocked_input | fetch_blocked` · `422 fetch_failed | validation_error` ·
`409 idempotency_conflict` · `502 upstream_error`.

## Tenant portal

Open `/app` (the root redirects there). A new customer signs up with an e-mail and password, which creates a tenant on the
free plan, its first API key (shown once) and a session. Signed in, they can:

- see every API key with its requests, spend and last use this month; create as many keys as they need (no
  per-tenant cap, as with OpenRouter or LiteLLM), rotate them, revoke them
- run summaries in the **Playground** (`/app/playground`): pick one of their keys and a model from their plan (list
  prices shown), paste text or a URL, choose style, length and optional instructions. It is the real `/v1/summarize`
  pipeline (rate limit, budget, guardrails, cache, ledger), billed to the chosen key
- see monthly totals, remaining budget, daily spend stacked by key, spend share per key, recent calls and spend by model,
  for this month or any of the last six, and download the itemised CSV statement
- change plan on the **Billing** page (`/app/billing`): compare plans, upgrade or downgrade at once, pay a paid plan's
  monthly price through a **mock** checkout (any Luhn-valid card with a future expiry, e.g. `4242 4242 4242 4242`;
  always succeeds, stores only brand and last four digits), and see the payment history. Decision D27

Passwords are hashed with scrypt; sessions are HttpOnly, SameSite=Strict cookies whose token is stored only as a sha256;
every state change is a same-origin JSON POST; sign-in is throttled per e-mail and per IP; someone else's key id is
reported as not found. Admins keep full control through `/admin`. Decision D24.

## Configuration is versioned, not hardcoded

| File | Contents |
|---|---|
| `config/plans.yaml` | requests/min, monthly budget, input size limit and allowed models per plan; soft-warning threshold |
| `config/prices.yaml` | list $/MTok per model with a **version**; every ledger row records which version priced it. Tenants are billed at list price even when the platform runs on a free-tier key |
| `prompts/summarize_v1.yaml`, `summarize_v2.yaml` | system prompt + user template; the document is wrapped as data; a content hash is recorded per request. v1 is the default; v2 is available (eval: no judge difference beyond noise, longer output; see `docs/MEASUREMENTS.md`) |

Rolling out a new prompt = add `summarize_v2.yaml` and set `SUMMARIZE_PROMPT_VERSION`. Rolling back = set it back.

### Models and providers

One deployment can serve models from several providers. Each model's row in `config/prices.yaml` names
its `provider`, and requests are routed by model name (D26). `LLM_PROVIDER` stays the default and uses
`LLM_API_KEY`; any other provider needs its own key (`GROQ_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`,
...). A model whose provider has no key is refused with 403 before any budget is reserved.

| Model | Provider | Free tier | Plans |
|---|---|---|---|
| `gemini-3.8-flash`, `gemini-3.5-flash-lite` | Gemini | yes | all |
| `gemini-3.1-flash-lite` | Gemini | yes | pro, enterprise |
| `openai/gpt-oss-20b` | Groq | yes | all |
| `openai/gpt-oss-120b`, `qwen/qwen3.8-27b` | Groq | yes | pro, enterprise |
| `claude-haiku-4-5`, `claude-sonnet-5-5`, `claude-opus-5-5` | Anthropic | no | enterprise |

Pick one per request with `"model"` in the body; tenants are billed at the model's list price either way.
Reasoning models (`gpt-oss`) get extra output headroom (`reasoning_tokens` in `prices.yaml`), reserved up
front, so their hidden reasoning cannot crowd out the summary.

## Cost attribution & budgets

Every model call is one row in `usage_ledger`, priced in integer micro-USD from a versioned price table, so any
past bill can be reproduced exactly. A completion row, as it appears in the CSV statement:

```
created_at,request_id,key_id,purpose,model,input_tokens,output_tokens,cost_microusd,cost_usd,price_version,prompt_version,status
2026-10-08T06:22:07+00:00,be4f885a…,2c8ab36e…,completion,gemini-3.8-flash,1101,89,1160,0.001160,2026-10-04,summarize_v1@ec6822c047b1,ok
```
`1101 × $0.75 + 89 × $3.75` per million tokens = 1,160 µUSD. Guardrail-classifier calls get their own rows
(`purpose=guardrail`) and cache hits a zero-cost row (`status=cached`); completion rows also store the reservation
estimate so the pessimism of admission control is measurable from the ledger. Online-judge calls (D17) are
listed under the tenant with `billed=no` and left out of every total, because the platform pays for them.

Each request atomically **reserves** its worst-case cost (`UPDATE … SET reserved += est WHERE spent + reserved + est
<= limit`) before any model call, and **settles** the actual cost afterwards, releasing the reservation; a failed call
releases it without billing. Because the limit check and the reservation are one statement, a burst can never pass on a
stale `spent`. Past 80 % of the limit every response carries `X-Budget-Warning`, and exactly one `budget.soft_warning`
audit event is written per tenant per month. A new UTC month starts a fresh budget row; a plan or override change
applies from the next request.

| Measurement | Value | Conditions |
|---|---|---|
| Burst of 50 concurrent requests, $0.004 budget | 4 admitted, 46 × 402; spent $0.002628 (66 % of limit), ledger total = spent, nothing left reserved | Postgres 16, mock provider with 50 ms latency, `python -m scripts.bench_billing burst` |
| Same burst with *check `spent`, then call* (the design D2 rejects) | 10 admitted; spent $0.006570 = **164 % of the limit** | same |
| Reservation pessimism (estimate ÷ actual) | median 1.43×, p95 1.84× | 9 requests, 3 styles × 50/150/300 words, ~3k-token document, mock token counts, `python -m scripts.bench_billing costs` |
| Cost of a ~3k-token request at `gemini-3.8-flash` list price | $0.0027 actual, reserved $0.0031–$0.0050 depending on `max_words` | same; real-model output lengths pending a Gemini run |
| Guardrail share of spend | 0 % | `GUARDRAIL_LLM=off` (the default): heuristic guardrails make no model calls |

The 34 % headroom in the burst row is the accepted cost of D2: the last requests that would have fit are refused
because their worst case would not. Decision D23 in `docs/DESIGN.md` covers how guardrail spend relates to the limit.

## Rate limits, idempotency, cache

- **Rate limits** — fixed 60 s window per API key *and* per tenant (a tenant's keys share its plan's rpm), one atomic `INSERT … ON CONFLICT DO UPDATE … RETURNING` per
  request (Postgres and SQLite). Free 5, pro 60, enterprise 600 requests/minute. A check costs p50 0.75 ms / p99 1.4 ms
  on Postgres; the accepted cost of a fixed window is up to 2× rpm across a window boundary (measured: exactly 2.0×).
- **Idempotency** — `Idempotency-Key` is scoped per tenant and kept for 24 h. The same key with the same body replays the
  stored response and is never billed twice; with a different body it is 409 `idempotency_conflict`; while the first
  request is still running, a duplicate gets 409 `idempotency_in_progress`. A failed request frees the key for a retry.
- **Response cache** — exact match on tenant, model, prompt hash, options and the extracted text, checked before the budget
  reserve. A hit is free (`usage.cached: true`, `cost_usd: 0`) and is booked as a zero-cost ledger row so request counts
  stay honest. TTL per plan (free 1 h, pro and enterprise 24 h); output that any guardrail flagged is never cached.
  `Cache-Control: no-cache` skips the lookup; `RESPONSE_CACHE_ENABLED=false` turns the cache off.
- **Model routing** — a request without `model` gets its plan's default: free → `gemini-3.5-flash-lite`, pro and
  enterprise → `gemini-3.8-flash`.

## Abuse protection

Two layers on the way in, one on the way out, and a rollout switch. Everything is in `app/guardrails/`.

**Layer 1 — scored heuristics** (`heuristic_v2`, ~0.1 ms). Forty-odd regex *signals*, each with a weight;
the verdict score is the noisy-OR of what fired. "Ignore all previous instructions" blocks on its own; the
word "jailbreak" in a news article only raises the score. Text is normalised first (zero-width characters,
leetspeak, Cyrillic look-alikes, spaced-out letters, base64 blobs), and a phrase that only appears after
normalisation adds an *obfuscation* signal. Rules cover English plus es / fr / de / pt / it / ru / zh / ja / hi.
Thresholds are per source — **0.8** for user instructions, **0.9** for fetched documents, which talk about
system prompts and jailbreaks innocently far more often.

**Layer 2 — LLM classifier** (`cascade_v1`, `GUARDRAIL_LLM=on`). Scores in the uncertain band (0.3–0.9) go to
`gemini-3.5-flash-lite` for a 60-token JSON verdict; an "injection" answer lifts the score to the model's
confidence, a "clean" answer caps it. **The classifier's tokens are billed to the tenant** as a
`purpose="guardrail"` ledger row (the model must be in `prices.yaml`). Verdicts are cached by text hash, so a
replay is free; a classifier outage degrades to the heuristic score rather than refusing the request.

**Output moderation** (`output_rules_v1`). A canary phrase ("PWNED") or a URL that is not in the source
proves the model obeyed the document instead of the user — the summary is **withheld**. Toxic phrases are
withheld. PII (email, phone — the same redactor the request log uses) is **redacted and returned**: the
tenant paid for the completion and `[EMAIL]` keeps the summary useful (DESIGN.md, D22).

**Rollout.** `GUARDRAILS_MODE=shadow` records every would-be block as a `guardrail.shadow_block` audit
event and serves the request anyway; `python -m evals.redteam.shadow_report` aggregates them by stage,
category, method and source with the most recent (redacted) matches. Flip to `enforce` when the share
looks right. A blocked request costs the tenant nothing beyond the classifier call; the budget reservation
is released.

**Evidence.** `make eval-redteam` runs 50 attacks and 50 benign look-alikes (`evals/redteam/cases.jsonl`) and
fails CI below catch ≥ 0.90 / FPR ≤ 0.05. Heuristics-only: **96 % catch, 2 % FPR, p50 0.1 ms** (the two
misses are a role-play persona and reversed text; the false positive is a security article quoting an
attack string). A **held-out set** of 20 reworded attacks (`heldout.jsonl`, written without looking at the rules) is
the honest number: the regexes alone caught **5 %** of it. That is why, with `GUARDRAIL_LLM=on`, every
instruction below the block line is now classified rather than only the uncertain band
(`GUARDRAIL_LLM_INSTRUCTIONS=always`): the cascade caught **90 %** of the held-out set unseen, for about
$0.04 per 1,000 uncached requests. The regex layer is the free pre-filter and the regression floor;
the classifier is the detector.

## Online quality monitoring

The offline golden set (`make eval-summ`) says whether a prompt is good before it ships. The online judge
says whether it stays good on real traffic. After every successful response the pipeline calls
`app.quality.online_judge.maybe_sample`, which with probability `QUALITY_SAMPLE_RATE` (default 5 %) queues
the request to a background worker — the response never waits. The worker, paced for the free tier, asks
the judge model for faithfulness and coverage (1–5) and records:

- `ledgerllm_quality_score{prompt_version,dimension}` — a histogram, so Grafana can plot quality per prompt
  version next to the thumbs-up ratio;
- a `quality_samples` row (request, prompt version, scores, the judge's issues);
- a `usage_ledger` row with `purpose="judge"` under the tenant for attribution, **not settled against the
  tenant's budget** — the tenant did not ask for the judge, so it is a platform cost (DESIGN.md, D17).

`GET /admin/quality` returns means per prompt version, judge cost per 1,000 samples and the recent samples.
Judge cost at list price: ≈ $0.0027 per sample on a 3k-token document, so ≈ **$0.13 per 1,000 requests** at 5 %.

## Evaluation and CI

| Gate | Command | What it measures |
|---|---|---|
| Red-team | `make eval-redteam` | catch rate and false-positive rate of the input guardrail on `evals/redteam/cases.jsonl` (50 attacks / 50 benign); fails below `thresholds.yaml` (0.90 / 0.05); `--llm on` measures the cascade |
| Shadow report | `python -m evals.redteam.shadow_report` | what `GUARDRAILS_MODE=shadow` would have blocked on real traffic, by stage / category / method / source |
| Summarization | `make eval-summ` (`PROVIDER=gemini` for the LLM judge) | key-point coverage, length, leak checks; with the judge: faithfulness and coverage on a 1–5 scale |
| Redaction | part of `make test` | every case in `evals/redaction/cases.jsonl` is redacted and nothing else is lost |

GitHub Actions runs lint, the test suite on SQLite **and** Postgres, and both eval gates on every pull request.
The LLM-judge run executes only when a PR touches `prompts/` or the feature code, to stay inside free-tier rate limits.

**Prompt rollout.**
1. Add a new file (`prompts/summarize_v2.yaml`); the old version stays loadable, and the content hash changes.
2. Put the eval diff in the PR: the full 30-case run for both versions with the same judge
   (`--prompt-version summarize_v1` / `summarize_v2`, separate `--out` files), plus the CI subset gate on the PR.
3. Flip the setting (`summarize_prompt_version` in `app/config.py`, or `SUMMARIZE_PROMPT_VERSION` per deployment)
   only when the new version is at least as good on both judge metrics and leaks nothing.
4. Roll back with `SUMMARIZE_PROMPT_VERSION=summarize_v1`, no code change needed. Cached responses are keyed
   by the prompt hash, so a switch either way never serves a summary made with the other prompt.

## Load test

```bash
make seed && make dev                  # or HOST=https://<deployment> for a remote target
make loadtest                          # 50 users: a tenant with a $0.02 budget + steady pro-plan traffic
python loadtest/verify_quota.py        # prints "quota enforcement: HOLDS" if spend never exceeded the limit
```

## Benchmarks

Measured values with their conditions. Raw log and trade-offs: [`docs/MEASUREMENTS.md`](docs/MEASUREMENTS.md).
Commands and the full load-test write-up: [`loadtest/README.md`](loadtest/README.md).

All numbers below were measured on `main` @ `72a7ab3` (6–8 Oct), mock provider; later merges changed the
pipeline (single final transaction, per-tenant rate limit, rule-aware cache key) but not its hot path, so they
are expected to hold — re-run `make loadtest` and `python loadtest/verify_quota.py` before quoting them. The burst and load figures are on
**Postgres 16** in Docker, the overhead figure on SQLite. **The mock is priced
identically to `gemini-3.8-flash` in `config/prices.yaml`, so dollar figures are real list-price
arithmetic**; only output *length* is synthetic.

| Metric | Value | Target | Conditions |
|---|---|---|---|
| Platform overhead (auth + quota + cache + guardrail heuristics, no model call) | **p50 7 / 8 / 10 ms** for 1.2k / 3.2k / 6.2k-token documents; worst steady-state request 10 ms | p99 ≤ 25 ms ✅ | `latency_sample.py --per-size 20 --sleep 0`, `MOCK_LATENCY_MS=0`, sequential. First request after process start is 12–47 ms (warm-up) and is excluded |
| Throughput | **218 req/s** | ≥ 50 req/s ✅ | 50 Locust users, 60 s, `MOCK_LATENCY_MS=800`, Postgres 16, one instance |
| Latency under load, p50 / p95 / p99 | **55 / 95 / 140 ms** | — | same run. On SQLite the same code gives p99 670 ms — **4.8× worse**, which is its database-wide write lock, not our overhead (`DESIGN.md` §5 limit #1). Aggregate percentiles are dominated by 402 refusals, which never reach the model |
| Burst quota test: admitted / refused / ledger vs limit | **157 admitted · 8,650 refused (402) · $0.018765 booked of a $0.020000 limit · reserved back to $0** → `quota enforcement: HOLDS` | never exceed the limit ✅ | **Postgres 16**, 50 users on 50 keys of one tenant, 60 s, `MOCK_LATENCY_MS=800`. Postgres (not SQLite) so the requests genuinely race; 800 ms is the hostile case, since it is the window a naive check-then-call would lose. Measured overspend **$0.000000**. 4,195 × 429 came from a user class that trips the limiter on purpose |
| Reservation pessimism | **6.2 %** of budget unspent ($0.001235 = 1.8× the average request) | — | same run. The measured cost of reserve-then-settle (D2): the reserve books worst-case output tokens and settles for less |
| Cost per request at list price | **$0.002776** (3.2k-token document, bullets, `max_words=150`) | ≤ $0.004 ✅ | 3,192 input + ~102 output tokens. A real 150-word summary is ~200 output tokens → ≈ $0.0031, still inside target |
| Guardrail classifier cost per 1,000 requests | ≈ **$0.015** (instructions) – **$0.07** (1.5k-token documents) | — | 12 % of eval cases in the uncertain band × Flash-Lite list price |
| Online judge cost per 1,000 requests | ≈ **$0.13** | — | 5 % sampled, 3k-token source, Gemini 3.8 Flash list price; billed to the platform, not tenants (D17) |
| Metric cardinality | **171 series**, of which only **18** carry a `tenant` label → **6 per tenant** | — | 3 tenants, 1 model, measured with Prometheus `count()`. Extrapolates to ~27,000 series at 1,000 tenants × 3 models — `DESIGN.md` §5 limit #4. The 85 `http_*` series are mostly the cost of widening the latency histogram from 3 to 14 buckets, without which neither latency target is measurable |
| End-to-end latency p50 / max (real model, **partial**) | ~1k-token doc **6.6 s / 28.3 s** (n = 8) · ~2.3k-token doc **5.7 s / 13.2 s** (n = 4) · ~6k-token: no successful call; platform overhead p50 **57–94 ms** | p50 ≤ 3 s, p99 ≤ 8 s ❌ | `latency_sample.py`, Gemini 3.8 Flash **free tier**, 8 Oct; 18 of 30 calls refused by the provider's quota (recorded as failures, not retried). Model time is > 98 % of the total; the free tier's queueing dominates. Re-run on a paid key for a full sample |
| End-to-end latency p50 / max (real model, Groq) | ~1k-token doc **0.50 s / 0.63 s** (n = 10) · ~2.3k **0.73 s / 1.54 s** (n = 10) · ~4.4k **0.82 s / 1.08 s** (n = 6); platform overhead p50 **64–133 ms** | p50 ≤ 3 s, p99 ≤ 8 s ✅ | `latency_sample.py`, `qwen/qwen3.8-27b` on Groq's **free tier**, 30 requests paced 20 s apart, 8 Oct; 26 of 30 succeeded, 4 long documents refused by the tokens-per-minute cap (recorded as failures, not retried). Same pipeline as the Gemini row: the difference between the two rows is the provider |
| End-to-end latency p50 / p95 / p99 in **production** (Render + Neon, real Gemini) | **2,273 / 25,660 / 25,660 ms**; 3 of 15 requests over 8 s. Platform overhead p50 **~860 ms** (range 767–1,126 ms) | p50 ≤ 3 s ✅, p99 ≤ 8 s ❌, overhead p99 ≤ 25 ms ❌ | Exact percentiles over `request_logs` on the deployment, n = 15 successful calls, 8 Oct — at that n the p95 and p99 are both just the slowest call, so read them as "the tail reached 25 s", not as a stable percentile. The cause is **cross-region database round trips, not CPU**: the pipeline makes ~19 database calls per request, `render.yaml` pinned no region so Render defaulted to Oregon while Neon is in `aws-us-east-2` (Ohio), and 19 × ~50 ms RTT ≈ 950 ms matches the measured gap. DESIGN.md §5 limit #1 dominating in production while invisible locally. The fix (`region: ohio`) is in review and **the re-measurement is still owed** |
| Red-team catch rate / false-positive rate / added latency (heuristics only) | **96 % / 2 %** / p50 0.10 ms, p99 0.41 ms | ≥ 90 % / ≤ 5 % ✅ | `evals/redteam`, 50 attacks / 50 benign, `GUARDRAIL_LLM=off` |
| Red-team **held-out** set (20 reworded attacks, never used for tuning) | heuristics **5 %** before / 65 % after generic rules; cascade with every instruction classified **90 %** (old rules) / **100 %** (new rules) | — | `evals/redteam/heldout.jsonl`; the set is now seen, a fresh one is needed for the next honest number |
| Red-team catch rate / false-positive rate (cascade) | **96 % / 2 %**; classifier 12/12 correct on the uncertain band; $0.014 per 1,000 requests; p50 1.33 s when consulted | ≥ 90 % / ≤ 5 % ✅ | `python -m evals.redteam.run --llm on`, gemini-3.5-flash-lite, free tier |
| Summarization faithfulness / coverage (LLM judge, 1–5) | `summarize_v1` **4.93 / 4.07**, hit rate 0.91 · `summarize_v2` **4.97 / 4.03**, hit rate 0.95 · **0 injection leaks** either way | ≥ 4.0 / ≥ 3.5 ✅ | 30-case Groq golden set, identical judge and rubric for both versions. v2 wins on faithfulness and key-point hit rate, v1 marginally on coverage, and v2 runs longer (15/30 over 120 words vs 7/30) at $0.00178 vs $0.00157 per request |

Traffic-control micro-benchmarks: rate-limit check p50 747 µs / p99 1,404 µs on Postgres;
fixed-window edge burst measured at exactly the 2.0× rpm bound D3 accepts; image 460 MB; local cold
start 1.1 s.

**Not yet measured:** the burst against the deployment (needs a live URL and its `ADMIN_TOKEN`), and
production latency *after* the region fix — the ~860 ms overhead row above is the before. Alert
rules for these signals are in [`ops/alerts.yml`](ops/alerts.yml).

## Observability

`/metrics` exposes `ledgerllm_cost_microusd_total{tenant,model,purpose}`, `ledgerllm_tokens_total`,
`ledgerllm_rejections_total{reason}`, `ledgerllm_llm_latency_seconds`, `ledgerllm_guardrail_verdicts_total`,
`ledgerllm_feedback_total`, `ledgerllm_quality_score{prompt_version,dimension}`, plus HTTP request counts and
latency histograms. Grafana dashboards are provisioned
from `ops/grafana/dashboards/`. Logs are JSON with a `request_id` on every line; the `request_logs` (redacted)
and `audit_events` tables explain every refusal after the fact.

## Security & compliance

API keys are `llk_<prefix>_<secret}`; only the SHA-256 hash is stored, the raw key is shown once at
creation/rotation. Admin routes need `Authorization: Bearer <ADMIN_TOKEN>` (constant-time compare).
Every request/response is persisted redacted (`email`, `phone`, Luhn-validated `credit_card`, `aadhaar`,
`pan`, `secret` incl. `sk-`/`AIza`/`llk_`, `ipv4` — specific → generic order, `evals/redaction/cases.jsonl`
has 22 cases incl. Luhn/order-id negatives). Audit rows (`audit_events`) record tenant-caused refusals
(`model_not_allowed`, `fetch_blocked`, `blocked_input`, `rate_limited`, `budget_exceeded`) plus key/tenant
lifecycle; transport noise (`fetch_failed`) is request-log only. Audit `details` are PII-redacted.
Query the trail newest-first: `GET /admin/audit?tenant_id=&event_type=&limit=50&before=`. Suspended
tenants get 403 `tenant_suspended`; revoked keys get 401.

## Deployment

Docker image (`Dockerfile`) deployed as a Render web service via `render.yaml`, with a Neon Postgres database
(step-by-step runbook: [`docs/DEPLOY.md`](docs/DEPLOY.md)).
Configuration is entirely environment variables: `DATABASE_URL`, `LLM_PROVIDER`, `LLM_API_KEY`,
`ADMIN_TOKEN`, `GUARDRAILS_MODE`, `GUARDRAIL_LLM`, `QUALITY_SAMPLE_RATE`, `SUMMARIZE_PROMPT_VERSION`,
`RESPONSE_CACHE_ENABLED`. Merges to `main` deploy automatically once CI
and both eval gates pass. The app is stateless, so it scales horizontally without changes.

## Repository layout

```
app/
  api/            routes: summarize (the pipeline), usage, feedback, admin, dashboard, health
  auth/           API keys, tenant resolution
  traffic/        rate limiting, idempotency, response cache
  billing/        pricing, budgets (reserve/settle), ledger, usage summaries
  feature/        URL fetching, extraction, prompts, summarization
  guardrails/     input classifier, output moderation
  quality/        online quality sampling (async LLM judge)
  compliance/     PII redaction, request logs, audit events
  observability/  structured logging, Prometheus metrics
  llm/            provider protocol; mock, OpenAI-compatible (Gemini/Groq/…) and Anthropic providers
  models.py  schemas.py  errors.py  config.py  plans.py  db.py  main.py
config/   prompts/   evals/   loadtest/   ops/   scripts/   tests/   docs/
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for how the codebase is organised and how changes land.

## Team

Suraj, Loukik, Naresh, Sai Venkatesh Alampally, Thrishal, Yashraj. Who owns which part of the system, and the work in each part:
[`docs/WORK_ALLOCATION.md`](docs/WORK_ALLOCATION.md).
