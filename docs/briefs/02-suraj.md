# Brief 02 — Traffic control, response cache, routing, deployment, CI — **Suraj**

## Mission
You own everything that decides whether a request is even allowed to cost money — **rate limits**, **idempotency**, the
**response cache**, **plan-aware model routing** — plus the delivery path (Docker, Render + Neon, GitHub Actions, branch
protection) and the integration role. Three requirements are yours (tiered rate limits, idempotency keys, the live URL)
and the cache is our main "extend further than the brief" feature.

## Files you own
`app/traffic/ratelimit.py`, `app/traffic/idempotency.py`, `app/traffic/cache.py`, `Dockerfile`, `render.yaml`,
`docker-compose.yml`, `.github/workflows/ci.yml`, `tests/test_ratelimit.py`, `tests/test_idempotency.py`,
new `tests/test_cache.py`, new `tests/test_routing.py`. You steward the shared contracts and review others' `CONTRACT CHANGE:` PRs.

## Must-have tasks

### A. Rate limiter (turn `tests/test_ratelimit.py` green)
Contract: `check_rate_limit(db, key_id, plan) -> RateLimitResult(allowed, limit, remaining, reset_epoch)`; the pipeline sets
headers from `result.headers()` and raises 429 with `Retry-After` when not allowed. Fixed 60 s window (D3), one atomic upsert:
```python
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
stmt = (insert(RateLimitWindow).values(key_id=key_id, window_start=window_start, count=1)
        .on_conflict_do_update(index_elements=[RateLimitWindow.key_id, RateLimitWindow.window_start],
                               set_={"count": RateLimitWindow.count + 1})
        .returning(RateLimitWindow.count))
count = db.execute(stmt).scalar_one(); db.commit()
```
`allowed = count <= plan.rpm`, `remaining = max(0, plan.rpm - count)`. Prune windows older than 3 min opportunistically
(~1 % of calls). Tests: sixth free-plan call → 429 with `Retry-After ≥ 1` and `X-RateLimit-Remaining: 0`; remaining decrements;
per-key independence (two keys, one tenant); window reset (monkeypatch `time.time`).

### B. Idempotency hardening
Base: lookup/store/409 on body mismatch. Add a 24 h TTL with opportunistic cleanup and an **in-flight** policy: insert a
`pending` record before executing; a concurrent duplicate gets 409 `idempotency_in_progress` (new error code → add to the
`errors.py` table); on failure delete the pending row so a retry can proceed. Tests: TTL expiry (monkeypatch `utcnow`), pending
→ conflict, failure clears pending, replay still bills once.

### C. Response cache (`app/traffic/cache.py`, new table `response_cache`)
The pipeline already computes `cache_key(tenant, model, prompt_hash, style, max_words, instructions, text)` and calls
`lookup()` before the budget reserve and `store()` after a successful, non-withheld completion. Implement: table
(`tenant_id`, `cache_key` PK, `response_json`, `model`, `prompt_version`, `input_tokens`, `output_tokens`, `hits`, `created_at`,
`expires_at`); `lookup` returns a `CachedSummary` when fresh and increments `hits`; `store` upserts. **Billing policy** (D16, write it
into DESIGN §3): hits are free to the tenant and recorded as a `usage_ledger` row with `purpose="completion"`,
`cost_microusd=0`, `status="cached"` so usage counts stay honest — or a flat micro-fee; pick, justify. Metrics:
`ledgerllm_cache_total{result="hit|miss"}` (add to `app/observability/metrics.py` via a tiny `[observability]` PR or ask Yashraj).
TTL from `plans.yaml` (`cache_ttl_s`, 0 = off). Tests: second identical request is a hit (`usage.cached == True`, `cost_usd == 0`,
budget unchanged); different `max_words` is a miss; prompt-version bump invalidates; TTL expiry; a withheld summary is never cached.

### D. Plan-aware model routing
`plans.yaml`: add `default_model` per plan (free → `gemini-3.5-flash-lite`, pro/enterprise → `gemini-3.8-flash`); pipeline:
`model = payload.model or plan.default_model or settings.default_model` (`CONTRACT CHANGE:` on `app/plans.py` + one pipeline line).
Keep `allowed_models` enforcement. Test per plan. This is a clean "we route cheaper tiers to cheaper models" trade-off.

### E. Deployment
1. Neon free Postgres → connection string → `postgresql+psycopg://…?sslmode=require`.
2. Render → New → Blueprint → this repo (`render.yaml`). Set `DATABASE_URL`, `LLM_API_KEY` (the team's shared Gemini key).
   `ADMIN_TOKEN` is generated — copy it. Health check `/healthz`.
3. Seed prod from your laptop: `DATABASE_URL=<neon> python -m scripts.seed`; pin the keys in the group chat, never in git.
4. `scripts/demo.sh https://<service>.onrender.com`; URL into README. Free tier sleeps after 15 min idle (cold start 30–60 s) —
   Yashraj warms before measuring. Fallback: Hugging Face Spaces (Docker; our CMD honours `PORT`).
Rollout story: PR → CI (lint, tests ×2, two eval gates) → merge → auto-deploy; prompt rollback = flip `SUMMARIZE_PROMPT_VERSION`;
guardrail rollout = `GUARDRAILS_MODE=shadow` → `enforce`; code rollback = Render redeploy-previous; schema additive only.

### F. Repository, CI and reviews
Create the public repo, push `main`, add `LLM_API_KEY` secret, invite the team, branch protection requiring the four CI jobs.
Keep `tests/test_smoke.py` green. Review contract-change PRs within 12 h. Wed 8 Oct: redeploy, reseed, demo against prod.

## Good to have
- Tokens-per-minute limit per key (`tpm` in plans) checked after the estimate.
- Semantic (embedding) cache on top of exact-match, with a tunable threshold and false-hit measurement.
- Neon pooling tuned (pool_size 5).

## Numbers to produce
- Limiter µs per check (1,000 calls on Postgres); window-edge burst admitted vs rpm.
- Cache hit rate on a replayed request trace (Yashraj's Locust CSV re-played) and $ saved at list price.
- Cold-start seconds; merge-to-live minutes; image size.

## What to write
- README: the live URL; a short "Rate limits, idempotency, cache" section with your numbers.
- Slide 5 (2 min): limiter (D3) and D1; idempotency (D13); cache placement + hit rate + billing policy (D16); CI → deploy → rollback.
  Failure modes: DB down → fail closed; duplicate in flight → 409.

## Pitfalls
- Pick the insert constructor by dialect; SQLite `RETURNING` needs ≥ 3.35 (fine on Python 3.11+).
- Cache must be keyed on the **prompt hash** and model, or a prompt change serves stale summaries.
- Never cache withheld/blocked outputs; never cache across tenants (the key already includes tenant_id — keep it).
- Render's own Postgres expires after 30 days — hence Neon.

## Agent kickoff prompt
> Read `CONTRIBUTING.md`, `docs/DESIGN.md`, `docs/briefs/README.md` and this brief. In `app/traffic/`
> implement the fixed-window limiter with a dialect-aware upsert, idempotency TTL + in-flight pending records, and the
> exact-match response cache with a `response_cache` table; add `default_model` per plan and use it in the pipeline. Make
> `tests/test_ratelimit.py` pass, add `tests/test_cache.py` and `tests/test_routing.py`. Run `make lint test` on SQLite and
> Postgres. Do not edit other packages beyond the routing line and the cache metric.
