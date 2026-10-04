# Area briefs

One brief per area of the system, matching `docs/WORK_ALLOCATION.md`. Each brief has implementation guidance,
acceptance criteria, the numbers to produce and a prompt to start a coding agent with. Read the context below first.

## Project context

- **Repo** `ledgerllm/` — a multi-tenant LLM API: "summarize any URL or text" behind API keys, plan tiers, rate limits,
  monthly cost budgets, per-request cost attribution, guardrails, idempotency, a response cache, PII-redacted logs and an
  audit log. Python 3.12 · FastAPI · SQLAlchemy 2 · Postgres (Neon) in prod, SQLite in dev/tests. **Model: Gemini 3.8 Flash on
  Google's free tier** (key from https://aistudio.google.com/apikey, no card) through an OpenAI-compatible `httpx` adapter in
  `app/llm/openai_compat.py`; the same adapter serves Groq / OpenAI / OpenRouter / Ollama; Anthropic via its SDK is optional.
  `MockProvider` for tests, CI and load tests (free, deterministic; `[[MOCK_FAIL]]` in the input simulates an upstream
  failure; `MOCK_LATENCY_MS` adds fake latency). Env: `LLM_PROVIDER=mock|gemini|…`, `LLM_API_KEY`, `DEFAULT_MODEL`.
- **Pipeline** (`app/api/summarize.py`): ① auth → ② idempotency replay → ③ rate limit → ④ fetch + extract + truncate →
  ④½ response-cache lookup (hit = free, no budget) → ⑤ estimate worst-case cost + ATOMIC budget reserve → ⑥ input guardrail
  (instructions, then document; classifier tokens are billed to the tenant) → ⑦ LLM → ⑧ output moderation → ⑨ settle actual
  cost + ledger rows + metrics → ⑩ redacted request log + audit + idempotent store + cache store + online-judge sampling hook.
  Every error: `{"error": {"code", "message", "request_id"}}`; codes in the `app/errors.py` docstring.
- **Conventions**: money is integer micro-USD (1 USD = 1,000,000); tenants are billed at **list** price (`config/prices.yaml`)
  even though the platform runs on a free tier; IDs are UUID strings; every tenant table has `tenant_id`; no migrations
  (`create_all`; dev DB is disposable: `rm ledgerllm.db`); prices, plans and prompts are versioned YAML.
- **Read in the repo**: `docs/DESIGN.md` (requirements with target values, decisions D1–D14, failure modes, monitoring plan,
  10× limits) and `CONTRIBUTING.md` (package ownership, which files are shared contracts and how a `CONTRACT CHANGE:` PR
  works, branch and CI expectations). Module docstrings in your package say what the base already does and what is left;
  `pytest` `xfail` reasons with your name are your to-do list.
- **Commands**: `make install` · `make test` · `make dev` · `make seed` · `scripts/demo.sh` · `make lint` / `make fmt` ·
  `make eval-redteam` · `make eval-summ [PROVIDER=gemini]` · `make loadtest` · `make up` (Docker: app + Postgres + Prometheus
  + Grafana). Postgres tests: `docker compose up -d db && DATABASE_URL=postgresql+psycopg://ledger:ledger@localhost:5432/ledger make test`.
