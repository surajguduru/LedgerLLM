"""Online quality sampling: judge a small share of live summaries asynchronously.

The pipeline calls `maybe_sample(...)` after every successful, non-withheld response. With probability
`QUALITY_SAMPLE_RATE` (default 5 %) the request is queued to a single background worker thread; the
response is never delayed, and nothing here can raise into the request path. The worker:

1. paces itself — at least `QUALITY_JUDGE_MIN_INTERVAL_S` between judge calls (Gemini free tier ≈ 10 RPM);
2. asks the judge model for `{"faithfulness": 1-5, "coverage": 1-5, "issues": [...]}` (the same prompt the
   offline eval in evals/summarization uses, copied here because `evals/` is not part of the deployed
   package — keep the two in sync);
3. books a `usage_ledger` row with `purpose="judge"` under the tenant for attribution but does **not**
   settle it against the tenant's budget: quality monitoring is a platform cost (DESIGN.md, D17);
4. records `ledgerllm_quality_score{prompt_version,dimension}` and inserts a `quality_samples` row.

A judge reply that is not JSON still costs a call, so the ledger row is written and the sample is stored
with null scores and an `issues` note; it is excluded from the means. A provider error is logged and
dropped — the sample is simply lost, which at a 5 % rate is fine.

Prompt versions carry a content hash (`summarize_v1@abcd1234`), so the per-version mean read off
`quality_samples` or the histogram is the online half of the "offline and online evaluation" story:
`GET /admin/quality` returns both.
"""

from __future__ import annotations

import atexit
import json
import random
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from time import monotonic, perf_counter

import structlog

from app.billing import ledger
from app.billing.pricing import compute_cost_microusd, load_prices
from app.compliance.redaction import redact
from app.config import get_settings
from app.llm import ProviderError, get_provider
from app.models import QualitySample
from app.observability import metrics

log = structlog.get_logger()

JUDGE_SYSTEM = (
    "You are a strict evaluator of summaries. Score the SUMMARY against the SOURCE on two 1-5 scales: "
    "faithfulness (5 = every claim is supported by the source, no invented facts) and coverage "
    "(5 = all key points of the source are present). Reply with JSON only: "
    '{"faithfulness": <int>, "coverage": <int>, "issues": ["<short>", ...]}'
)
JUDGE_MAX_TOKENS = 300
MAX_SOURCE_CHARS = 12_000  # the judge does not need the whole 2 MB page to score a 150-word summary

_rng = random.Random()
_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_pending: set[Future] = set()
_last_call_at = 0.0
_stop = threading.Event()


def seed(value: int) -> None:
    """Deterministic sampling for tests."""
    _rng.seed(value)


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quality-judge")
            atexit.register(shutdown)
        return _executor


def pending() -> int:
    with _lock:
        return len(_pending)


def drain(timeout_s: float = 30.0) -> None:
    """Block until queued judge jobs finish. For tests and graceful shutdown; never called by the pipeline."""
    with _lock:
        futures = list(_pending)
    for f in futures:
        try:
            f.result(timeout=timeout_s)
        except Exception:  # noqa: BLE001 — a failed job already logged itself
            pass


def shutdown() -> None:
    global _executor
    _stop.set()
    with _lock:
        ex, _executor = _executor, None
    if ex is not None:
        ex.shutdown(wait=True, cancel_futures=True)
    _stop.clear()


def maybe_sample(
    *, request_id: str, tenant_id: str, prompt_version: str, source_text: str, summary: str
) -> bool:
    """Queue this response for judging with probability QUALITY_SAMPLE_RATE. Returns whether it was."""
    rate = get_settings().quality_sample_rate
    if rate <= 0 or not summary.strip():
        return False
    try:
        if _rng.random() >= rate:
            return False
        job = _JudgeJob(
            request_id=request_id,
            tenant_id=tenant_id,
            prompt_version=prompt_version,
            source_text=source_text[:MAX_SOURCE_CHARS],
            summary=summary,
        )
        fut = _get_executor().submit(job.run)
        with _lock:
            _pending.add(fut)
        fut.add_done_callback(_forget)
        return True
    except Exception as exc:  # noqa: BLE001 — sampling must never affect the response
        log.warning("quality_sample_enqueue_failed", request_id=request_id, error=str(exc))
        return False


def _forget(fut: Future) -> None:
    with _lock:
        _pending.discard(fut)


def _pace() -> None:
    global _last_call_at
    wait = get_settings().quality_judge_min_interval_s
    with _lock:
        delay = max(0.0, _last_call_at + wait - monotonic())
    if delay:
        _stop.wait(delay)
    with _lock:
        _last_call_at = monotonic()


def _parse(text: str) -> tuple[int | None, int | None, list]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None, None, ["judge returned non-JSON"]
    try:
        data = json.loads(text[start : end + 1])
        f = int(data["faithfulness"])
        c = int(data["coverage"])
    except (ValueError, KeyError, TypeError):
        return None, None, ["judge returned non-JSON"]
    clamp = lambda x: max(1, min(5, x))  # noqa: E731
    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    # The judge quotes the source when it explains an issue, so this can carry PII: redact before storing.
    return clamp(f), clamp(c), [redact(str(i)[:200]).text for i in issues][:10]


class _JudgeJob:
    def __init__(self, **kw: str) -> None:
        self.__dict__.update(kw)

    def run(self) -> None:
        settings = get_settings()
        model = settings.quality_judge_model or settings.default_model
        _pace()
        if _stop.is_set():
            return
        user = f"<source>\n{self.source_text}\n</source>\n<summary>\n{self.summary}\n</summary>"
        t0 = perf_counter()
        try:
            res = get_provider().complete(
                model=model, system=JUDGE_SYSTEM, user=user, max_tokens=JUDGE_MAX_TOKENS
            )
        except ProviderError as exc:
            log.warning("quality_judge_failed", request_id=self.request_id, error=str(exc))
            return
        latency_ms = int((perf_counter() - t0) * 1000)
        faith, cov, issues = _parse(res.text)
        cost = compute_cost_microusd(model, res.input_tokens, res.output_tokens)

        from app.db import SessionLocal  # local import: the worker owns its own session

        with SessionLocal() as db:
            # Attribution only — not settled against the tenant's budget (D17).
            ledger.book(
                db,
                tenant_id=self.tenant_id,
                key_id=None,
                request_id=self.request_id,
                purpose="judge",
                model=model,
                input_tokens=res.input_tokens,
                output_tokens=res.output_tokens,
                cost_microusd=cost,
                price_version=load_prices().version,
                prompt_version=self.prompt_version,
                latency_ms=latency_ms,
            )
            db.add(
                QualitySample(
                    request_id=self.request_id,
                    tenant_id=self.tenant_id,
                    prompt_version=self.prompt_version,
                    judge_model=model,
                    faithfulness=faith,
                    coverage=cov,
                    issues=issues,
                    judge_latency_ms=latency_ms,
                )
            )
            db.commit()
        metrics.record_booking(
            tenant=self.tenant_id,
            model=model,
            purpose="judge",
            input_tokens=res.input_tokens,
            output_tokens=res.output_tokens,
            cost_microusd=cost,
        )
        if faith is not None and cov is not None:
            metrics.QUALITY_SCORE.labels(self.prompt_version, "faithfulness").observe(faith)
            metrics.QUALITY_SCORE.labels(self.prompt_version, "coverage").observe(cov)
        log.info(
            "quality_sample",
            request_id=self.request_id,
            prompt_version=self.prompt_version,
            faithfulness=faith,
            coverage=cov,
            judge_ms=latency_ms,
            cost_microusd=cost,
        )
