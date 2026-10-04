# Brief 05 — Guardrails, red-team eval, online quality sampling — **Thrishal**

## Mission
You own **abuse protection** — the injection/jailbreak classifier on instructions *and* fetched documents, output moderation,
shadow rollout, the **100-case red-team set** gating every PR — and **online quality sampling**: an asynchronous LLM judge that
scores a share of live summaries and turns quality into a metric per prompt version. That second part completes the
"offline *and* online evaluation" story.

## Files you own
`app/guardrails/**`, `app/quality/**`, `evals/redteam/**`, `tests/test_guardrails.py`, new `tests/test_online_judge.py`,
new `evals/redteam/shadow_report.py`. Read-only: `app/compliance/redaction.py`.

## How the pipeline uses you
`classify_input(text, source)` and `moderate_output(text)` return `GuardrailVerdict`. A verdict with `model`, `input_tokens`,
`output_tokens` set is **billed to the tenant** as a `purpose="guardrail"` ledger row (model must be in `prices.yaml`). `blocked` →
400 in `enforce`, audit-only in `shadow`, skipped in `off`. After every successful response the pipeline calls
`app.quality.online_judge.maybe_sample(request_id, tenant_id, prompt_version, source_text, summary)` (currently a no-op).

## Must-have tasks
### A. Second detection layer (turn `test_paraphrased_injection_is_blocked` green)
Cascade: heuristics → if uncertain (score between 0.3 and 0.9) call an **LLM classifier on `gemini-3.5-flash-lite`** (cheapest, fast):
JSON-only verdict `{injection, category, confidence}`, `max_tokens=60`; set `verdict.model/input_tokens/output_tokens` so the cost is
billed; thresholds 0.8 (instructions) / 0.9 (documents); in-process LRU cache by `sha256(text)`; `GUARDRAIL_LLM=on|off` env flag
(tests and load tests run `off`; the eval prints which configuration it measured). Turn bare keyword rules (e.g. `jailbreak`) into
signals, not hard blocks — fixes the `ben-004` false positive.
### B. Output moderation (turn `test_output_with_pii_is_withheld_or_redacted` green)
PII leak via `redact()` → decide **redact-and-return** (`details={"redacted": True, "text": …}`, pipeline uses it — one-line
`CONTRACT CHANGE:`) or withhold; canary / foreign-URL check (`category="instruction_followed"`, withhold); small toxicity list.
Document the policy in the docstring and DESIGN §3.
### C. Red-team set — ~50 attacks + ~50 benign
Direct 15 · indirect/document 15 (HTML comments, markdown links, "Note to the AI:", fake system blocks, table cells) · persona 8 ·
obfuscation 6 (base64, leetspeak, zero-width, homoglyphs) · multilingual 6. Benign 50: look-alikes with trigger words in normal
context, ordinary instructions and documents. Raise thresholds to catch ≥ 0.9 / FPR ≤ 0.05 once met.
### D. Shadow report
`python -m evals.redteam.shadow_report`: `audit_events` where `event_type='guardrail.shadow_block'`, grouped by category/method,
10 most recent redacted snippets.
### E. Online quality sampling (`app/quality/online_judge.py`)
`maybe_sample`: with probability `QUALITY_SAMPLE_RATE` (default 0.05) enqueue to a background `ThreadPoolExecutor(1)`; the worker
calls the judge (reuse the summarization judge prompt via `evals/summarization/run.py` or copy it), records
`ledgerllm_quality_score{prompt_version}` (histogram, add to metrics via a tiny `[observability]` PR), writes a `usage_ledger` row
with `purpose="judge"` **not** settled against the tenant (platform cost — D17, write it), and stores the score in a new
`quality_samples` table (request_id, prompt_version, faithfulness, coverage, judged_at). `GET /admin/quality` returns recent samples
and means per prompt version (admin route — small `[admin]` PR or ask Loukik). Tests: sampling rate honoured (seeded RNG), worker
runs with the mock provider, nothing blocks the response path.

## Good to have
Per-plan guardrail policy in `plans.yaml`; secret-leak check on outputs; drift alert when the sampled quality mean drops.

## Numbers to produce
Catch rate / FPR for heuristics-only vs cascade (two rows); added p50/p99 latency; $ per 1,000 requests of classifier at list
price; share blocked by source; sampled quality mean per prompt version + judge cost per 1,000 requests.

## What to write
- README: "Abuse protection" and "Online quality monitoring" sections.
- Slide 8 (2 min): layered detection; shadow → enforce (D9); catch vs FPR table; who pays (classifier → tenant, judge → platform);
  quality-per-prompt-version panel. Failure mode: false positive on a paying customer → shadow first, FPR gate, nothing billed on block.

## Pitfalls
Heuristic path sub-millisecond · model on a verdict must exist in `prices.yaml` · redact attack strings before auditing · Gemini free
tier ~10 RPM: the judge worker must pace itself (and the eval must run heuristics-only without a key).

## Agent kickoff prompt
> Read `CONTRIBUTING.md`, `docs/DESIGN.md`, `docs/briefs/README.md` and this brief, then `app/guardrails/`,
> `app/quality/online_judge.py` and `app/api/summarize.py`. Turn regex rules into scored signals, add a cascaded Gemini Flash-Lite
> JSON classifier behind `GUARDRAIL_LLM` that fills `verdict.model/input_tokens/output_tokens`, implement output moderation, grow
> `evals/redteam/cases.jsonl` to ~50/50, and implement background quality sampling with a `quality_samples` table and a histogram
> metric. Run `make lint test eval-redteam`. Do not edit other packages beyond the documented one-line hooks.
