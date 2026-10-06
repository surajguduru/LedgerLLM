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
                     ③ rate limit (per key, per minute)       ⑧ output moderation
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
| `GET /v1/usage` | current-month spend, remaining budget, tokens, breakdown by model and purpose |
| `POST /v1/feedback` | thumbs up/down on a `request_id` |
| `GET /dashboard` | usage/billing page for a tenant (paste a key) |
| `POST /admin/tenants`, `POST /admin/tenants/{id}/keys`, `GET /admin/tenants` | tenant and key management (`Authorization: Bearer $ADMIN_TOKEN`) |
| `GET /metrics`, `GET /healthz`, `GET /docs` | Prometheus, health, OpenAPI |

Errors always look like `{"error": {"code": "…", "message": "…", "request_id": "…"}}` with stable codes:
`401 missing_api_key | invalid_api_key` · `403 tenant_suspended | model_not_allowed` · `429 rate_limited` ·
`402 budget_exceeded` · `400 blocked_input | fetch_blocked` · `422 fetch_failed | validation_error` ·
`409 idempotency_conflict` · `502 upstream_error`.

## Configuration is versioned, not hardcoded

| File | Contents |
|---|---|
| `config/plans.yaml` | requests/min, monthly budget, input size limit and allowed models per plan; soft-warning threshold |
| `config/prices.yaml` | list $/MTok per model with a **version**; every ledger row records which version priced it. Tenants are billed at list price even when the platform runs on a free-tier key |
| `prompts/summarize_v1.yaml` | system prompt + user template; the document is wrapped as data; a content hash is recorded per request |

Rolling out a new prompt = add `summarize_v2.yaml` and set `SUMMARIZE_PROMPT_VERSION`. Rolling back = set it back.

## Evaluation and CI

| Gate | Command | What it measures |
|---|---|---|
| Red-team | `make eval-redteam` | catch rate and false-positive rate of the input guardrail on `evals/redteam/cases.jsonl`; fails below `thresholds.yaml` |
| Summarization | `make eval-summ` (`PROVIDER=gemini` for the LLM judge) | key-point coverage, length, leak checks; with the judge: faithfulness and coverage on a 1–5 scale |
| Redaction | part of `make test` | every case in `evals/redaction/cases.jsonl` is redacted and nothing else is lost |

GitHub Actions runs lint, the test suite on SQLite **and** Postgres, and both eval gates on every pull request.
The LLM-judge run executes only when a PR touches `prompts/` or the feature code, to stay inside free-tier rate limits.

## Load test

```bash
make seed && make dev                  # or HOST=https://<deployment> for a remote target
make loadtest                          # 50 users: a tenant with a $0.02 budget + steady pro-plan traffic
python loadtest/verify_quota.py        # prints "quota enforcement: HOLDS" if spend never exceeded the limit
```

## Benchmarks

Measured values are recorded here as they are produced; conditions are stated next to every number.

| Metric | Value | Conditions |
|---|---|---|
| End-to-end latency p50 / p99 | pending | Gemini 3.8 Flash, ~3k-token document, free-tier host |
| Platform overhead p99 (auth + quota + guardrail heuristics, no model call) | pending | mock provider |
| Throughput | pending | 50 Locust users, mock provider, one instance |
| Burst quota test: admitted / refused (402), ledger total vs limit | pending | 50 users against a $0.02 budget |
| Cost per request | pending | Gemini 3.8 Flash list price, bullets, 150 words |
| Red-team catch rate / false-positive rate / added latency | pending | `evals/redteam` |
| Summarization faithfulness / coverage (LLM judge, 1–5) | pending | `evals/summarization` |

## Observability

`/metrics` exposes `ledgerllm_cost_microusd_total{tenant,model,purpose}`, `ledgerllm_tokens_total`,
`ledgerllm_rejections_total{reason}`, `ledgerllm_llm_latency_seconds`, `ledgerllm_guardrail_verdicts_total`,
`ledgerllm_feedback_total`, plus HTTP request counts and latency histograms. Grafana dashboards are provisioned
from `ops/grafana/dashboards/`. Logs are JSON with a `request_id` on every line; the `request_logs` (redacted)
and `audit_events` tables explain every refusal after the fact.

## Deployment

Docker image (`Dockerfile`) deployed as a Render web service via `render.yaml`, with a Neon Postgres database.
Configuration is entirely environment variables: `DATABASE_URL`, `LLM_PROVIDER`, `LLM_API_KEY`,
`ADMIN_TOKEN`, `GUARDRAILS_MODE`, `SUMMARIZE_PROMPT_VERSION`. Merges to `main` deploy automatically once CI
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

Suraj, Loukik, Naresh, Sai, Thrishal, Yashraj. Who owns which part of the system, and the work in each part:
[`docs/WORK_ALLOCATION.md`](docs/WORK_ALLOCATION.md).
