# Brief 03 — The feature, the model providers, and the quality eval — **Sai Venkatesh Alampally**

## Mission
You own what the customer buys (the summary), how it reaches a model (**`app/llm/` — providers and a fallback chain**), and the
proof it is good (**the hand-written golden set with an LLM judge gated in CI**). The eval set is the hard part and the part that makes the repo stand out; a screenshot of a pull request blocked by an
eval regression is the clearest proof that the gate works. Provider fallback on error/timeout is a resilience feature borrowed from the gateway project — a strong failure-mode story.

## Files you own
`app/feature/{fetch,prompts,summarize}.py`, `app/llm/**`, `prompts/*.yaml`, `evals/summarization/**`, `tests/test_fetch.py`,
`tests/test_llm_providers.py`, new `tests/test_prompts.py`, new `tests/test_fallback.py`.

## Must-have tasks
### A. SSRF guard (turn `tests/test_fetch.py` green)
`validate_url(url)`: scheme ∈ {http, https}; no userinfo; resolve `socket.getaddrinfo`; reject every address that is private /
loopback / link-local / multicast / reserved / unspecified or in `169.254.169.254/32`, `fd00::/8`; fetch with
`follow_redirects=False`, re-validate each `Location`, max 3 hops. `class FetchBlocked(FetchError)`; pipeline maps it to
`400 fetch_blocked` before the `FetchError → 422` branch (one-line spine edit, `CONTRACT CHANGE:`). Unit-test `validate_url` with
`10.0.0.1`, `192.168.1.1`, `127.0.0.1`, `::1`, `169.254.169.254`, `fd00::1`, `ftp://`, `http://user@host`. State the DNS-rebinding limitation.
### B. Long documents
Head 70 % + tail 30 % with a `[… N characters omitted …]` marker (`source.strategy: "head_tail"`, additive schema). Then **map-reduce**
for pro/enterprise when text exceeds the plan limit: chunk → per-chunk bullets → final summary; book every call to the ledger
(each is a `run_summary` call; the pipeline reserve must cover the total — coordinate the estimate with Naresh: estimate = chunks × per-chunk).
`source.strategy: "map_reduce"`, `usage` sums tokens. Test with the mock.
### C. Providers and fallback chain (`app/llm/`)
Base: `OpenAICompatibleProvider` (Gemini default; Groq/OpenAI/OpenRouter/Ollama presets), `AnthropicProvider`, `MockProvider`.
Add `FallbackProvider(primary, secondary)`: on `ProviderError(retryable=True)` (429, 5xx, timeout) call the secondary once; record
which provider answered (`LLMResult.provider` — additive) and a metric `ledgerllm_provider_fallbacks_total{from,to}`; the ledger row
uses the model that actually answered. Config: `LLM_FALLBACK_PROVIDER=groq`, `LLM_FALLBACK_API_KEY`, `LLM_FALLBACK_MODEL`
(e.g. `llama-3.1-8b-instant` — add its **list price** to `prices.yaml` when you add it). Gemini's free tier returns 429 at ~10 RPM,
so the fallback will genuinely trigger in the demo. Tests with `httpx.MockTransport`: primary 429 → secondary answers;
primary 400 → no fallback; both fail → 502.
### D. Golden set — ~30 hand-written documents
Store **text**, 300–3,000 words, mixed domains (engineering blog, news, policy text, product docs, abstract, how-to, finance,
transcript, changelog); 3–6 `key_points`, `must_not_include` (absent facts + canaries `PWNED` / `EVIL.COM` for the 4–5 docs with
embedded instructions); `tags`.
### E. Judge + calibration
Finish `judge()` (Gemini, strict JSON, retry once); means + worst three; run `make eval-summ PROVIDER=gemini` (respect ~10 RPM:
add a sleep between calls); commit `results/last_gemini.json` (allow that path in `.gitignore`) with the prompt hash. Hand-score 5
cases; report judge agreement.
### F. The CI screenshot + prompt v2
PR 1: a deliberately worse prompt → gate fails → screenshot → README. PR 2: `summarize_v2.yaml` improvement → eval diff → merge →
flip `SUMMARIZE_PROMPT_VERSION`.
### G. Prompt tests
`tests/test_prompts.py`: braces inside documents are never interpreted; styles map; `output_token_cap` respects the ceiling.

## Good to have
Exact token counting via the provider for the reservation (compare with len/4 — that's a number); PDF via `pypdf`; a second judge model
(Groq Llama) for disagreement reporting.

## Numbers to produce
Faithfulness/coverage per prompt version; key-point hit rate; judge agreement; tokens per request; fallback count in the latency sample;
extraction yield on 10 real pages.

## What to write
- README: "Summarization quality" (eval set, method, numbers, CI screenshot) and "Model providers" (presets, fallback, free-tier note).
- Slide 7 (2.5 min): D11 document-as-data; SSRF guard; map-reduce; fallback chain; golden set; judge; the blocked-PR screenshot.
  Trade-offs: free tier (Google may use the data) vs paid; one judge vs several; truncation vs map-reduce. Failure modes: provider
  outage → fallback → 502 with nothing billed; injected instruction followed → prompt + output moderation + leak checks.

## Pitfalls
Never `str.format` the document · store texts not URLs · print total tokens per eval run · Gemini 3 models cannot disable
"thinking" — reasoning tokens may appear in `completion_tokens`; note it next to the token numbers.

## Agent kickoff prompt
> Read `CONTRIBUTING.md`, `docs/DESIGN.md`, `docs/briefs/README.md` and this brief, then `app/feature/`, `app/llm/`
> and `evals/summarization/run.py`. Add `validate_url` with redirect re-validation and `FetchBlocked → 400 fetch_blocked`; implement
> head+tail truncation and map-reduce; add `FallbackProvider` with a `LLM_FALLBACK_*` config and MockTransport tests; finish the JSON
> judge with rate-limit-aware pacing. Run `make lint test` and `make eval-summ`. Do not edit other packages beyond the one
> `except FetchBlocked` line.
