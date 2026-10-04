# Contributing

## Ownership

Each package is owned by one person. Owners merge their own pull requests once CI is green.
The full list of work per owner is in [`docs/WORK_ALLOCATION.md`](docs/WORK_ALLOCATION.md).

| Area | Packages / paths | Owner |
|---|---|---|
| Traffic control (rate limits, idempotency, response cache), deployment, CI | `app/traffic/`, `Dockerfile`, `render.yaml`, `docker-compose.yml`, `.github/` | Suraj |
| Identity, admin, compliance | `app/auth/`, `app/api/admin.py`, `app/compliance/`, `evals/redaction/` | Loukik |
| Billing correctness and usage API | `app/billing/`, `app/api/usage.py` | Naresh |
| Feature, model providers, quality evaluation | `app/feature/`, `app/llm/`, `prompts/`, `evals/summarization/` | Sai |
| Guardrails, red-team evaluation, online quality sampling | `app/guardrails/`, `app/quality/`, `evals/redteam/` | Thrishal |
| Dashboards, metrics, load testing | `app/observability/`, `app/api/dashboard.py`, `app/api/feedback.py`, `ops/`, `loadtest/` | Yashraj |
| Shared contracts | `app/schemas.py`, `app/models.py`, `app/errors.py`, `app/api/summarize.py` | everyone, reviewed by the owner of the affected stage |

## Shared contracts

The request/response schemas, the database schema, the error codes, and the function signatures that
`app/api/summarize.py` calls are shared contracts. To change one:

1. make the change in your PR,
2. add a line starting with `CONTRACT CHANGE:` to the PR description explaining what and why,
3. request a review from the owner of any other package that calls or implements it.

Adding a column to a table your package owns is fine; mention it in the PR. There are no migrations:
`init_db()` runs `create_all` and development databases are disposable (`rm ledgerllm.db`).

## Workflow

- Branch from `main`: `feat/<area>-<topic>`, e.g. `feat/billing-soft-warning`. Rebase, don't merge `main` in.
- Open the PR as a draft early. Merge when CI is green: `ruff`, `pytest` on SQLite and Postgres, the
  red-team eval gate and the summarization eval gate.
- Touch only your own packages plus `tests/`, your `evals/` directory and your README section. A change
  to someone else's package goes in a separate small PR titled `[<their area>] …`.
- Lowering an eval threshold requires an explanation in the PR description.

## Tests

`make test`. Tests marked `xfail` describe behaviour that is designed but not yet implemented; the reason
names the owner. Remove the marker when the behaviour lands. `tests/test_smoke.py` covers the end-to-end
pipeline and must stay green on every PR.

Run against Postgres locally:

```bash
docker compose up -d db
DATABASE_URL=postgresql+psycopg://ledger:ledger@localhost:5432/ledger make test
```

## Secrets and spend

Never commit keys. `LLM_API_KEY` lives in GitHub Actions secrets and in the deployment environment.
Use `LLM_PROVIDER=mock` for development, tests and load tests; use the real provider (Gemini free tier by
default) for the LLM-judge eval, the guardrail classifier and latency measurements.

## Style

`make fmt` before committing. `ruff` enforces the rest in CI.
