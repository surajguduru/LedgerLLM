# Brief 01 — Identity, admin and compliance — **Loukik**

## Mission
You own who the caller is and what we keep about them: **API keys and tenant resolution**, the **admin API**, the **audit log**,
the **redacted request log** and **PII redaction**. This area covers per-key auth, request/response logging with PII redaction and the structured audit log.
You also open the presentation (the problem statement — DESIGN §1 is written; present it).

## Files you own
`app/auth/{keys,dependency}.py`, `app/api/admin.py`, `app/compliance/{redaction,request_log,audit}.py`,
`evals/redaction/cases.jsonl`, `tests/test_redaction.py`, new `tests/test_admin.py`, new `tests/test_isolation.py`.
Admin schemas (the `# --- admin` section of `app/schemas.py`) are yours to extend (`CONTRACT CHANGE: additive admin schemas`).

## Must-have tasks
### A. PII redaction (turn `tests/test_redaction.py` fully green)
Tags are `[TYPE]` from the case's `expected_types` (`credit_card → [CREDIT_CARD]`, `aadhaar`, `pan`, `secret`, `ipv4`, plus `email`,
`phone`). Update `IMPLEMENTED` in the test as you add each. **Order specific → generic** (card → aadhaar → pan → secret → ipv4 →
email → phone) or the phone regex eats the digits. Card: candidate `\b(?:\d[ -]?){13,19}\b` then **Luhn**; Aadhaar
`\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b`; PAN `\b[A-Z]{5}\d{4}[A-Z]\b`; secrets `\b(sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|llk_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{8,})\b`
(include our own key format and Gemini's `AIza…`); IPv4 `\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b`.
Add ≥ 3 cases per type incl. negatives (`must_keep`, e.g. a 13-digit order id that fails Luhn). Add `redact_dict()` and use it on
audit `details` strings.
### B. Admin API (`tests/test_admin.py`)
`GET /admin/tenants/{id}/keys`; `DELETE /admin/keys/{key_id}` (revoke → audit `key.revoked`; key then returns 401);
`POST /admin/keys/{key_id}/rotate` (new key, revoke old, raw shown once); `PATCH /admin/tenants/{id}` (plan ∈ plans, status
`active|suspended`, `budget_override_usd`; audit `tenant.updated` with before/after; suspended → 403 `tenant_suspended`);
`GET /admin/audit?tenant_id=&event_type=&limit=50&before=` newest-first — the demo of "why was I refused".
### C. Audit policy
Two refusal paths in the pipeline pass `audit_type=None` (`model_not_allowed`, `fetch_failed`). Policy: refusals caused by the
*tenant* are audited; infrastructure noise is logged only. Implement + document in the `audit.py` docstring (`CONTRACT CHANGE:` line).
### D. Isolation tests
Tenant A cannot read B's usage, cannot post feedback on B's request id, idempotency keys don't collide across tenants, admin routes
need the admin token, a revoked key cannot replay an idempotent request.

## Good to have
Request-log retention (delete older than N days); per-key scopes; batching `last_used_at` writes (the TODO in `dependency.py` — also
a 10× bottleneck).

## Numbers to produce
Redaction cases passing / total and per type; redaction latency per KB; audit events per refusal type.

## What to write
- README: "Security & compliance" paragraph (hashed keys, what is logged, what is redacted, audit queries).
- Slides 1–2 (framing, 2 min) and 6 (identity & compliance, 1 min). Trade-offs: hash-only keys (D12) vs retrievable; regex redaction
  (fast, deterministic, misses names) vs NER. Failure mode: key leak → revoke, `last_used_at`, audit trail.
- The project summary line for the README (2–3 sentences, resume style) → `docs/MEASUREMENTS.md` until numbers land.

## Pitfalls
Phone regex last · never log raw bodies · keep `secrets.compare_digest` for the admin token.

## Agent kickoff prompt
> Read `CONTRIBUTING.md`, `docs/DESIGN.md`, `docs/briefs/README.md` and this brief. In `app/compliance/redaction.py`
> add Luhn-validated credit-card, Aadhaar, PAN, secret-token (incl. `AIza…` and `llk_…`) and IPv4 patterns ordered specific-to-generic
> and make every case in `evals/redaction/cases.jsonl` pass; add key revoke/rotate/list, tenant PATCH and a paginated `/admin/audit`
> with tests in `tests/test_admin.py`; add `tests/test_isolation.py`. Run `make lint test`. Do not edit other packages except the
> additive admin schemas.
