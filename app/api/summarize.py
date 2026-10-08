"""POST /v1/summarize — the request pipeline. Each stage calls into exactly one package:

    1 auth (app/auth)  ->  2 idempotency replay (app/traffic)  ->  3 rate limit (app/traffic)
    ->  4 acquire content (app/feature)  ->  5 estimate + reserve budget (app/billing)
    ->  6 input guardrail (app/guardrails)  ->  7 LLM (app/llm)  ->  8 output guardrail (app/guardrails)
    ->  9 settle + ledger (app/billing)  ->  10 redacted log + audit (app/compliance), metrics (app/observability)

Stage order is a design decision (docs/DESIGN.md, D6): cheap checks first, so abusive or over-quota
traffic never costs an LLM call; the budget is reserved *before* the guardrail so classifier spend
can be booked to the tenant too. This file is the integration point — changes to it are reviewed by
the stream owner whose stage is affected (see CONTRIBUTING.md).
"""

from __future__ import annotations

import json
from time import perf_counter

import structlog
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth.dependency import AuthContext, get_auth_context
from app.billing import budget, ledger
from app.billing.pricing import compute_cost_microusd, estimate_cost_microusd, load_prices
from app.compliance import audit as audit_events
from app.compliance.audit import audit
from app.compliance.request_log import write_request_log
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import ApiError
from app.feature.fetch import FetchBlocked, FetchedPage, FetchError, fetch_url
from app.feature.prompts import load_prompt
from app.feature.summarize import build_user_prompt, output_token_cap, run_summary
from app.guardrails.input import classify_input
from app.guardrails.output import moderate_output
from app.guardrails.types import PASS, GuardrailVerdict
from app.llm import ProviderError, estimate_tokens, fallback_model, get_provider
from app.models import BudgetPeriod
from app.observability import metrics
from app.plans import load_plans, microusd_to_usd
from app.quality.online_judge import maybe_sample
from app.schemas import (
    BudgetInfo,
    GuardrailsInfo,
    SourceInfo,
    SummarizeRequest,
    SummarizeResponse,
    UsageInfo,
)
from app.traffic import cache, idempotency
from app.traffic.ratelimit import check_rate_limit

router = APIRouter(prefix="/v1", tags=["summarize"])
log = structlog.get_logger()

WITHHELD = "[summary withheld by output moderation]"


def _fail(
    db: Session,
    *,
    status: int,
    code: str,
    message: str,
    request_id: str,
    auth: AuthContext,
    latency_ms: int,
    audit_type: str | None,
    headers: dict[str, str] | None = None,
    details: dict | None = None,
    raw_input: str | None = None,
) -> ApiError:
    """Record a refused request consistently (metrics + audit + request log), then return the error."""
    metrics.REJECTIONS.labels(code).inc()
    if audit_type:
        audit(
            db,
            audit_type,
            tenant_id=auth.tenant.id,
            key_id=auth.api_key.id,
            request_id=request_id,
            details={"code": code, **(details or {})},
        )
    write_request_log(
        db,
        request_id=request_id,
        tenant_id=auth.tenant.id,
        key_id=auth.api_key.id,
        endpoint="/v1/summarize",
        status_code=status,
        latency_ms=latency_ms,
        raw_input=raw_input,
        error_code=code,
    )
    db.commit()
    return ApiError(status, code, message, headers=headers)


def _run_guardrail(
    db: Session,
    *,
    mode: str,
    stage: str,
    verdict: GuardrailVerdict,
    auth: AuthContext,
    request_id: str,
) -> bool:
    """Returns True when the request must be blocked (enforce mode only). Shadow mode only records."""
    metrics.GUARDRAIL.labels(stage, str(verdict.blocked).lower(), verdict.category or "none").inc()
    if not verdict.blocked or mode == "off":
        return False
    if mode == "shadow":
        audit(
            db,
            audit_events.GUARDRAIL_SHADOW,
            tenant_id=auth.tenant.id,
            key_id=auth.api_key.id,
            request_id=request_id,
            details={"stage": stage, **verdict.to_dict()},
        )
        return False
    return True


def _book_guardrail(
    db: Session,
    *,
    verdict: GuardrailVerdict,
    auth: AuthContext,
    request_id: str,
    period: str,
    stage: str,
) -> int:
    """If a guardrail verdict came from a paid model call, bill it to the tenant. Returns micro-USD."""
    if not verdict.model or (verdict.input_tokens + verdict.output_tokens) == 0:
        return 0
    cost = compute_cost_microusd(verdict.model, verdict.input_tokens, verdict.output_tokens)
    ledger.book(
        db,
        tenant_id=auth.tenant.id,
        key_id=auth.api_key.id,
        request_id=request_id,
        purpose="guardrail",
        model=verdict.model,
        input_tokens=verdict.input_tokens,
        output_tokens=verdict.output_tokens,
        cost_microusd=cost,
        price_version=load_prices().version,
        prompt_version=f"guardrail:{stage}:{verdict.method}",
        latency_ms=verdict.latency_ms,
    )
    budget.settle(
        db, auth.tenant.id, period, 0, cost
    )  # adds to spent; leaves the completion reservation intact
    metrics.record_booking(
        tenant=auth.tenant.id,
        model=verdict.model,
        purpose="guardrail",
        input_tokens=verdict.input_tokens,
        output_tokens=verdict.output_tokens,
        cost_microusd=cost,
    )
    return cost


@router.post("/summarize", response_model=SummarizeResponse)
def summarize(
    payload: SummarizeRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=128),
    cache_control: str | None = Header(None, alias="Cache-Control"),
):
    request_id: str = request.state.request_id
    bypass_cache = "no-cache" in (cache_control or "").lower()
    t0 = perf_counter()
    elapsed = lambda: int((perf_counter() - t0) * 1000)  # noqa: E731

    # 2. idempotency replay / claim ------------------------------------------------------------
    request_hash = idempotency.hash_request(payload)
    if not idempotency_key:
        return _pipeline(
            payload, request_id, response, db, auth, settings, None, request_hash, bypass_cache, t0
        )
    existing = idempotency.lookup(db, auth.tenant.id, idempotency_key)
    if existing is not None and existing.request_hash != request_hash:
        raise _fail(
            db,
            status=409,
            code="idempotency_conflict",
            message="Idempotency-Key was already used with a different request body",
            request_id=request_id,
            auth=auth,
            latency_ms=elapsed(),
            audit_type=audit_events.IDEMPOTENCY_CONFLICT,
        )
    if existing is not None and not idempotency.is_pending(existing):
        return JSONResponse(
            status_code=existing.status_code,
            content=json.loads(existing.response_json),
            headers={"Idempotent-Replayed": "true", "X-Request-ID": request_id},
        )
    if existing is not None or not idempotency.claim(
        db, tenant_id=auth.tenant.id, key=idempotency_key, request_hash=request_hash
    ):
        raise _fail(
            db,
            status=409,
            code="idempotency_in_progress",
            message="a request with this Idempotency-Key is still being processed; retry shortly",
            request_id=request_id,
            auth=auth,
            latency_ms=elapsed(),
            audit_type=None,
            headers={"Retry-After": "1"},
        )
    try:
        return _pipeline(
            payload,
            request_id,
            response,
            db,
            auth,
            settings,
            idempotency_key,
            request_hash,
            bypass_cache,
            t0,
        )
    except BaseException:
        db.rollback()
        idempotency.release(db, auth.tenant.id, idempotency_key)
        raise


def _pipeline(
    payload: SummarizeRequest,
    request_id: str,
    response: Response,
    db: Session,
    auth: AuthContext,
    settings: Settings,
    idempotency_key: str | None,
    request_hash: str,
    bypass_cache: bool,
    t0: float,
):
    """Stages 3-10. Runs at most once per idempotency key at a time (see `summarize`)."""
    elapsed = lambda: int((perf_counter() - t0) * 1000)  # noqa: E731
    tenant, key, plan = auth.tenant, auth.api_key, auth.plan
    raw_for_log = payload.text or str(payload.url)

    # 3. rate limit ----------------------------------------------------------------------------
    rl = check_rate_limit(db, key.id, plan)
    response.headers.update(rl.headers())
    if not rl.allowed:
        raise _fail(
            db,
            status=429,
            code="rate_limited",
            message=f"plan '{plan.name}' allows {plan.rpm} requests/minute",
            request_id=request_id,
            auth=auth,
            latency_ms=elapsed(),
            audit_type=audit_events.RATE_LIMITED,
            headers={"Retry-After": str(rl.retry_after_s), **rl.headers()},
        )

    # 4. acquire content -----------------------------------------------------------------------
    model = payload.model or plan.default_model or settings.default_model
    if model not in plan.allowed_models:
        raise _fail(
            db,
            status=403,
            code="model_not_allowed",
            message=f"model '{model}' is not available on plan '{plan.name}'",
            request_id=request_id,
            auth=auth,
            latency_ms=elapsed(),
            audit_type=None,
        )
    if payload.url:
        try:
            page = fetch_url(str(payload.url), settings)
        except FetchBlocked as exc:
            # SSRF guard refusal. TODO(Loukik): a fetch.blocked audit event in app/compliance/audit.py
            raise _fail(
                db,
                status=400,
                code="fetch_blocked",
                message=str(exc),
                request_id=request_id,
                auth=auth,
                latency_ms=elapsed(),
                audit_type=None,
                raw_input=raw_for_log,
            ) from exc
        except FetchError as exc:
            raise _fail(
                db,
                status=422,
                code="fetch_failed",
                message=str(exc),
                request_id=request_id,
                auth=auth,
                latency_ms=elapsed(),
                audit_type=None,
                raw_input=raw_for_log,
            ) from exc
    else:
        page = FetchedPage(
            url=None,
            final_url=None,
            title=payload.title or "",
            text=payload.text or "",
            content_type="text/plain",
            fetched_ms=0,
        )
    truncated = len(page.text) > plan.max_input_chars
    if truncated:
        page.text = page.text[: plan.max_input_chars]

    # 4½. response cache (owner: Suraj) — a hit costs nothing and skips the budget entirely (D16)
    prompt = load_prompt(settings.summarize_prompt_version)
    ckey = cache.cache_key(
        tenant_id=tenant.id,
        model=model,
        prompt_hash=prompt.content_hash,
        style=payload.style,
        max_words=payload.max_words,
        instructions=payload.instructions,
        text=page.text,
    )
    use_cache = settings.response_cache_enabled and plan.cache_ttl_s > 0
    cached = None
    if use_cache and bypass_cache:
        metrics.CACHE.labels("bypass").inc()
    elif use_cache:
        cached = cache.lookup(db, tenant.id, ckey)
        metrics.CACHE.labels("hit" if cached is not None else "miss").inc()
    if cached is not None:
        period = budget.current_period()
        limit = budget.limit_for(tenant, plan)
        bp = db.get(BudgetPeriod, (tenant.id, period))
        spent = bp.spent_microusd if bp else 0
        committed = spent + (bp.reserved_microusd if bp else 0)
        warning = committed >= limit * load_plans().soft_warning_fraction
        response.headers.update(
            {
                "X-Budget-Limit-USD": f"{microusd_to_usd(limit):.6f}",
                "X-Budget-Spent-USD": f"{microusd_to_usd(spent):.6f}",
            }
        )
        body = SummarizeResponse(
            request_id=request_id,
            summary=cached.text,
            source=SourceInfo(
                url=page.url, title=page.title, chars=len(page.text), truncated=truncated
            ),
            usage=UsageInfo(
                model=cached.model,
                prompt_version=cached.prompt_version,
                price_version=load_prices().version,
                input_tokens=cached.input_tokens,
                output_tokens=cached.output_tokens,
                cost_usd=0.0,
                latency_ms=0,
                cached=True,
            ),
            budget=BudgetInfo(
                period=period,
                limit_usd=microusd_to_usd(limit),
                spent_usd=microusd_to_usd(spent),
                remaining_usd=microusd_to_usd(max(0, limit - spent)),
                warning=warning,
            ),
            guardrails=GuardrailsInfo(mode=settings.guardrails_mode, input={}, output={}),
        )
        ledger.book(
            db,
            tenant_id=tenant.id,
            key_id=key.id,
            request_id=request_id,
            purpose="completion",
            model=cached.model,
            input_tokens=0,
            output_tokens=0,
            cost_microusd=0,
            price_version=load_prices().version,
            prompt_version=cached.prompt_version,
            latency_ms=0,
            status="cached",
        )
        write_request_log(
            db,
            request_id=request_id,
            tenant_id=tenant.id,
            key_id=key.id,
            endpoint="/v1/summarize",
            status_code=200,
            latency_ms=elapsed(),
            model=cached.model,
            raw_input=raw_for_log,
            raw_output=cached.text,
            guardrails={"cached": True},
        )
        if idempotency_key:
            idempotency.store(
                db,
                tenant_id=tenant.id,
                key=idempotency_key,
                request_hash=request_hash,
                status_code=200,
                response_json=body.model_dump_json(),
            )
        db.commit()
        return body

    # 5. estimate + reserve budget -------------------------------------------------------------
    user_prompt = build_user_prompt(
        prompt,
        page,
        style=payload.style,
        max_words=payload.max_words,
        instructions=payload.instructions,
    )
    max_tokens = output_token_cap(prompt, payload.max_words)
    # Fallback chain (D19): only to a model on the tenant's plan, and the reservation covers the
    # priciest model that may answer, so settling can never exceed it.
    fallback = fallback_model()
    allow_fallback = fallback is not None and fallback != model and fallback in plan.allowed_models
    est_input = estimate_tokens(prompt.system) + estimate_tokens(user_prompt)
    est_microusd = max(
        estimate_cost_microusd(m, est_input, max_tokens)
        for m in ([model, fallback] if allow_fallback else [model])
    )
    decision = budget.reserve(db, tenant, plan, est_microusd)
    budget_headers = {
        "X-Budget-Limit-USD": f"{microusd_to_usd(decision.limit_microusd):.6f}",
        "X-Budget-Spent-USD": f"{microusd_to_usd(decision.spent_microusd):.6f}",
    }
    response.headers.update(budget_headers)
    if not decision.allowed:
        raise _fail(
            db,
            status=402,
            code="budget_exceeded",
            message=f"monthly budget of ${microusd_to_usd(decision.limit_microusd):.2f} exhausted for {decision.period}",
            request_id=request_id,
            auth=auth,
            latency_ms=elapsed(),
            audit_type=audit_events.BUDGET_EXCEEDED,
            headers=budget_headers,
            details={
                "period": decision.period,
                "limit_microusd": decision.limit_microusd,
                "spent_microusd": decision.spent_microusd,
                "estimate_microusd": est_microusd,
            },
        )
    if decision.warning:
        response.headers["X-Budget-Warning"] = (
            f"{microusd_to_usd(decision.spent_microusd + decision.reserved_microusd):.4f} of "
            f"{microusd_to_usd(decision.limit_microusd):.2f} USD committed this period"
        )
        # TODO(Naresh): emit audit_events.BUDGET_SOFT_WARNING exactly once per period (budget_periods.soft_warned_at)

    # 6. input guardrail -----------------------------------------------------------------------
    mode = settings.guardrails_mode
    verdict_in = PASS
    if mode != "off":
        verdict_in = classify_input(payload.instructions or "", source="instructions")
        if not verdict_in.blocked:
            verdict_in = classify_input(page.text, source="document")
    guardrail_cost = _book_guardrail(
        db,
        verdict=verdict_in,
        auth=auth,
        request_id=request_id,
        period=decision.period,
        stage="input",
    )
    if _run_guardrail(
        db, mode=mode, stage="input", verdict=verdict_in, auth=auth, request_id=request_id
    ):
        budget.release(db, tenant.id, decision.period, est_microusd)
        raise _fail(
            db,
            status=400,
            code="blocked_input",
            message=f"request blocked by input guardrail ({verdict_in.category})",
            request_id=request_id,
            auth=auth,
            latency_ms=elapsed(),
            audit_type=audit_events.BLOCKED_INPUT,
            details=verdict_in.to_dict(),
            raw_input=raw_for_log,
        )

    # 7. LLM -----------------------------------------------------------------------------------
    provider = get_provider()
    try:
        result = run_summary(
            provider,
            model=model,
            prompt=prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            allow_fallback=allow_fallback,
        )
    except ProviderError as exc:
        budget.release(db, tenant.id, decision.period, est_microusd)
        metrics.UPSTREAM_ERRORS.labels(model, str(exc.retryable).lower()).inc()
        raise _fail(
            db,
            status=502,
            code=exc.code,
            message=str(exc),
            request_id=request_id,
            auth=auth,
            latency_ms=elapsed(),
            audit_type=audit_events.UPSTREAM_ERROR,
            details={"retryable": exc.retryable},
            headers={"Retry-After": "2"} if exc.retryable else None,
            raw_input=raw_for_log,
        ) from exc
    answered = result.model  # the fallback model if the chain was used; bill and label by it
    metrics.LLM_LATENCY.labels(answered).observe(result.latency_ms / 1000)

    # 8. output guardrail ----------------------------------------------------------------------
    verdict_out = moderate_output(result.text) if mode != "off" else PASS
    guardrail_cost += _book_guardrail(
        db,
        verdict=verdict_out,
        auth=auth,
        request_id=request_id,
        period=decision.period,
        stage="output",
    )
    withheld = _run_guardrail(
        db, mode=mode, stage="output", verdict=verdict_out, auth=auth, request_id=request_id
    )
    summary_text = WITHHELD if withheld else result.text
    if withheld:
        audit(
            db,
            audit_events.OUTPUT_MODERATED,
            tenant_id=tenant.id,
            key_id=key.id,
            request_id=request_id,
            details=verdict_out.to_dict(),
        )

    # 9. settle + ledger -----------------------------------------------------------------------
    actual_microusd = compute_cost_microusd(answered, result.input_tokens, result.output_tokens)
    budget.settle(db, tenant.id, decision.period, est_microusd, actual_microusd)
    ledger.book(
        db,
        tenant_id=tenant.id,
        key_id=key.id,
        request_id=request_id,
        purpose="completion",
        model=answered,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_microusd=actual_microusd,
        price_version=load_prices().version,
        prompt_version=f"{result.prompt_version}@{result.prompt_hash}",
        latency_ms=result.latency_ms,
    )
    metrics.record_booking(
        tenant=tenant.id,
        model=answered,
        purpose="completion",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_microusd=actual_microusd,
    )

    # 10. response, log, audit, idempotency store ----------------------------------------------
    spent_after = decision.spent_microusd + actual_microusd + guardrail_cost
    body = SummarizeResponse(
        request_id=request_id,
        summary=summary_text,
        source=SourceInfo(
            url=page.url, title=page.title, chars=len(page.text), truncated=truncated
        ),
        usage=UsageInfo(
            model=answered,
            prompt_version=f"{result.prompt_version}@{result.prompt_hash}",
            price_version=load_prices().version,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=microusd_to_usd(actual_microusd),
            latency_ms=result.latency_ms,
            provider=result.provider,
            fallback_from=result.fallback_from,
        ),
        budget=BudgetInfo(
            period=decision.period,
            limit_usd=microusd_to_usd(decision.limit_microusd),
            spent_usd=microusd_to_usd(spent_after),
            remaining_usd=microusd_to_usd(max(0, decision.limit_microusd - spent_after)),
            warning=decision.warning,
        ),
        guardrails=GuardrailsInfo(
            mode=mode, input=verdict_in.to_dict(), output=verdict_out.to_dict()
        ),
    )
    body_json = body.model_dump_json()
    write_request_log(
        db,
        request_id=request_id,
        tenant_id=tenant.id,
        key_id=key.id,
        endpoint="/v1/summarize",
        status_code=200,
        latency_ms=elapsed(),
        model=answered,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_microusd=actual_microusd,
        raw_input=raw_for_log,
        raw_output=result.text,
        guardrails={"input": verdict_in.to_dict(), "output": verdict_out.to_dict()},
    )
    if use_cache and not (withheld or verdict_in.blocked or verdict_out.blocked):
        # anything a guardrail flagged, even in shadow mode, is never served again from cache
        cache.store(
            db,
            tenant.id,
            ckey,
            cache.CachedSummary(
                text=result.text,
                model=answered,
                prompt_version=f"{result.prompt_version}@{result.prompt_hash}",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            ),
            ttl_s=plan.cache_ttl_s,
        )
    if not withheld:
        maybe_sample(
            request_id=request_id,
            tenant_id=tenant.id,
            prompt_version=f"{result.prompt_version}@{result.prompt_hash}",
            source_text=page.text,
            summary=result.text,
        )
    if idempotency_key:
        idempotency.store(
            db,
            tenant_id=tenant.id,
            key=idempotency_key,
            request_hash=request_hash,
            status_code=200,
            response_json=body_json,
        )
    db.commit()
    log.info(
        "summarize_ok",
        tenant=tenant.id,
        model=answered,
        fallback_from=result.fallback_from,
        cost_microusd=actual_microusd,
        tokens_in=result.input_tokens,
        tokens_out=result.output_tokens,
        llm_ms=result.latency_ms,
    )
    return body
