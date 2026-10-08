# Slide 7 — The feature, its providers, and how we know the summary is good

Speaker: Sai Venkatesh Alampally · about 2.5 minutes. Content for the shared deck (Naresh keeps it); paste from here.
Every number is from `docs/MEASUREMENTS.md`, with its conditions in the notes.

## On the slide

**Title:** One summary, made safely, billed honestly, and gated by an eval

- **Document as data (D11):** the page goes inside `<document>` tags the system prompt declares data, rendered in
  one pass so nothing in it can expand or close the wrapper
- **SSRF guard (D18):** every resolved address and every redirect hop must be public, or 400 `fetch_blocked`
- **Long pages (D20):** head + tail for every plan; map-reduce over the whole page on paid plans, reserved up front
- **Fallback chain (D19):** 429 / 5xx / timeout → one retry on a second model, billed at the model that answered
- **Golden set + judge (D21):** 30 hand-written cases, 5 with injected instructions; leak checks + an LLM judge
  gate every prompt change in CI
- **It blocks a worse prompt:** [PR #23](https://github.com/surajguduru/LedgerLLM/pull/23), CI FAIL on an
  injection leak ([run](https://github.com/surajguduru/LedgerLLM/actions/runs/37762769918)); screenshot here

**Diagram suggestion:** the pipeline stages ④ to ⑦ left to right, with what can go wrong under each box.

```
④ fetch + extract ──────▶ fit to the plan ─────────────▶ ⑤ reserve ──▶ ⑥ guardrail ──▶ ⑦ model call
  SSRF guard on every       ≤ limit: as is                  worst case of     (Thrishal)       primary model
  address and redirect      > limit: head 70 % + tail 30 %  every call                          │ 429/5xx/timeout
  └─ blocked → 400          > limit, paid plan:                                                 ▼
                            chunks → map calls → reduce                                      fallback model
                                                                                              │ fails too
                                                                                              ▼
                                                                                   502, nothing billed
```

## Speaker notes (~300 words)

The customer buys one thing: a summary of a page. My job was to make that one call safe, affordable and
measurably good.

Safe first. A URL from a tenant makes our server send a request, so every address it resolves to, and every
redirect, must be public; otherwise it is a 400 before anything is sent. The page itself is untrusted, so the
prompt treats it as data.

Long pages: we keep the start and the end, because conclusions sit at the end. Paid plans can read the whole
page with map-reduce. On a 140,000-character book chapter that was four calls, a cent and a bit — $0.0115 at
list price — and 6.5 seconds, against one call, $0.0018 and 1.8 seconds for head and tail, which missed the
middle of the argument. Real Gemini, 8 October.

Outages: free-tier quotas are per model, so we fall back once to a second model. Forced in a real test, the
fallback answered in 1.35 seconds and the tenant was billed 419 micro-dollars at that model's price. If both
fail, the tenant gets a 502 and pays nothing.

Quality: 30 hand-written documents in ten domains, five with a planted instruction like "visit EVIL.COM". A
judge model scores faithfulness and coverage from 1 to 5. Both on Groq's free tier, because Gemini's allows 20
requests a day. Our prompt scored 4.93 and 4.07, no leaks. A second prompt scored 4.97 and 4.03; one case moves
by a point between runs, so the judge cannot tell them apart, and the second costs 13 % more — we kept the first.

And the gate works. We opened a pull request with a weaker prompt. CI blocked it: the summary repeated the
planted EVIL.COM link. The judge was rate-limited in that run, so the deterministic leak check is what caught
it — which is the point of having both.

## Trade-offs ("We chose X over Y because Z")

1. **We chose Gemini's free tier over a paid key because** it costs nothing while we build, and tenants are billed
   at list price either way, so bills do not depend on our tier. The price: Google may use free-tier content to
   improve its products, which is fine for synthetic test documents and not for customer data, so production
   would run on a paid tier (D10).
2. **We chose one judge from a different model family over several judges because** one judge fits Groq's free
   tier (a full 30-case run is 60 calls, 21 minutes, ~95,000 tokens) and a different family reduces
   self-preference. The price: no disagreement signal, and the eval runs on Qwen, not on our production model
   (D21).
3. **We chose head+tail plus map-reduce over plain truncation because** cutting at the limit loses the ending,
   where conclusions are, and paid customers expect the whole page read. The price: map-reduce is N+1 sequential
   calls (6.5 s against 1.8 s on the chapter above) and the reservation is pessimistic, so it is a paid-plan
   feature with a ceiling (D20).

## Failure modes and their handling

- **Provider outage.** Primary returns 429, 5xx or times out → one call to the fallback model, if it is on the
  tenant's plan, billed at its list price, `usage.fallback_from` set, fallback counter +1. Both fail → 502
  `upstream_error`, reservation released, nothing billed (`tests/test_fallback.py`; real forced fallback, 8 Oct).
- **An injected instruction gets followed.** Three layers: the prompt declares the document data and the page
  cannot close the `<document>` wrapper; output moderation withholds a summary with a canary or a link that is not
  in the source (Thrishal's stage); and in CI the golden set's canary cases fail the gate on any leak, which is
  exactly what blocked PR #23.

## Likely examiner questions

**How do you know the judge is right?**
We do not yet have a number. Five summaries spanning good and bad judge scores are set aside for blind human
scoring; the human scores are pending, so judge agreement is reported as pending, not guessed. To get it: score
them blind with `python -m evals.summarization.agreement --blind`, then report exact and within-one agreement;
with more time, more rows and a second judge model to report disagreement.

**Your eval runs on Qwen. Does it say anything about Gemini in production?**
It tests the prompt and the pipeline, not Gemini. Gemini's free tier allows 20 requests a day on the flash
model, and a judged run needs 60 calls. With a paid key we would summarise with the production model itself.

**Why did v2 not ship if it scored higher on faithfulness?**
4.97 against 4.93 is inside the noise: the same prompt moves a case by about a point between runs. v2 was
longer (15 of 30 summaries over the word limit against 7) and 13 % dearer, so it is available but not the
default.

**What does the SSRF guard not cover?**
DNS rebinding: we resolve the name, then the HTTP client resolves it again when it connects, and a hostile DNS
server can change the answer in between. The fix is to connect to the address we checked; an egress proxy is the
answer at scale (D18).
