"""Billing numbers: cost per request, reservation pessimism, and a concurrent burst against a budget.

    python -m scripts.bench_billing costs     # LLM_PROVIDER=gemini LLM_API_KEY=… for real-model costs
    DATABASE_URL=postgresql+psycopg://ledger:ledger@localhost:5432/ledger python -m scripts.bench_billing burst

`costs` sends one ~3k-token document in every style × max_words combination and reads cost and
estimate back from the ledger. `burst` fires 50 concurrent requests at a $0.004 budget through a real
uvicorn server (mock provider), first with the atomic reserve, then with a check of `spent` and no
reservation (the design D2 rejects), and prints admitted / 402 / spent vs limit for both. It needs Postgres:
SQLite serialises writers, so it cannot show the race. Both commands use a fresh set of tables.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("ADMIN_TOKEN", "bench-admin")
os.environ["RESPONSE_CACHE_ENABLED"] = "false"

import json  # noqa: E402
import statistics  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app import models  # noqa: E402
from app.billing import budget  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.llm import get_provider  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import BudgetPeriod, UsageLedger  # noqa: E402
from app.plans import microusd_to_usd  # noqa: E402

ADMIN = {"Authorization": f"Bearer {os.environ['ADMIN_TOKEN']}"}
GOLDEN = Path(__file__).resolve().parents[1] / "evals" / "summarization" / "golden.jsonl"
STYLES = ("bullets", "paragraph", "tldr")
MAX_WORDS = (50, 150, 300)
BURST = 50
BURST_BUDGET_USD = 0.004


def _reset_tables() -> None:
    models.Base.metadata.drop_all(bind=engine)
    models.Base.metadata.create_all(bind=engine)


def _document(target_chars: int = 12_000) -> str:
    texts = [json.loads(line)["text"] for line in GOLDEN.read_text().splitlines() if line.strip()]
    doc = ""
    while len(doc) < target_chars:
        doc += " ".join(texts) + "\n\n"
    return doc[:target_chars]


def _tenant(http, plan: str, budget_usd: float | None = None) -> str:
    body = {"name": f"bench-{plan}", "plan": plan}
    if budget_usd is not None:
        body["budget_override_usd"] = budget_usd
    r = http.post("/admin/tenants", json=body, headers=ADMIN)
    r.raise_for_status()
    return r.json()["api_key"]


def costs() -> None:
    _reset_tables()
    doc = _document()
    with TestClient(create_app()) as http:
        key = _tenant(http, "enterprise")
        print(f"provider {get_settings().llm_provider}, document {len(doc)} chars")
        print("style      max_words  model             in_tok  out_tok  cost_usd   estimate_usd")
        for style in STYLES:
            for max_words in MAX_WORDS:
                r = http.post(
                    "/v1/summarize",
                    json={"text": doc, "style": style, "max_words": max_words},
                    headers={"X-API-Key": key},
                )
                r.raise_for_status()
                with SessionLocal() as db:
                    model, tokens_in, tokens_out, cost, estimate = db.execute(
                        select(
                            func.max(UsageLedger.model),
                            func.sum(UsageLedger.input_tokens),
                            func.sum(UsageLedger.output_tokens),
                            func.sum(UsageLedger.cost_microusd),
                            func.sum(UsageLedger.estimate_microusd),
                        ).where(UsageLedger.request_id == r.json()["request_id"])
                    ).one()
                print(
                    f"{style:<10} {max_words:>9}  {model:<16} {int(tokens_in):>7} "
                    f"{int(tokens_out):>8}  {microusd_to_usd(int(cost)):.6f}   "
                    f"{microusd_to_usd(int(estimate)):.6f}"
                )
    with SessionLocal() as db:
        # one reservation per request, possibly several completion rows (map-reduce, fallback)
        ratios = sorted(
            int(est) / int(cost)
            for est, cost in db.execute(
                select(func.sum(UsageLedger.estimate_microusd), func.sum(UsageLedger.cost_microusd))
                .where(UsageLedger.purpose == "completion")
                .group_by(UsageLedger.request_id)
                .having(func.sum(UsageLedger.cost_microusd) > 0)
            )
        )
        by_purpose = dict(
            db.execute(
                select(UsageLedger.purpose, func.sum(UsageLedger.cost_microusd)).group_by(
                    UsageLedger.purpose
                )
            ).all()
        )
    p95 = ratios[min(len(ratios) - 1, int(0.95 * len(ratios)))]
    total = sum(int(v) for v in by_purpose.values())
    print(
        f"reservation pessimism (estimate / actual) over {len(ratios)} requests: "
        f"median {statistics.median(ratios):.2f}x, p95 {p95:.2f}x"
    )
    print(f"guardrail share of spend: {int(by_purpose.get('guardrail', 0)) / total:.1%}")


def _check_then_call(db, tenant, plan, est_microusd):
    """D2's rejected design: check `spent` against the limit, reserve nothing, settle afterwards.
    Every request in flight decides on the same stale `spent`."""
    period = budget.current_period()
    bp = budget._ensure_period(db, tenant.id, period, budget.limit_for(tenant, plan))
    db.refresh(bp)
    return budget.BudgetDecision(
        allowed=bp.spent_microusd + est_microusd <= bp.hard_limit_microusd,
        period=period,
        limit_microusd=bp.hard_limit_microusd,
        spent_microusd=bp.spent_microusd,
        reserved_microusd=bp.reserved_microusd,
        warning=False,
    )


def _burst_once(label: str) -> None:
    _reset_tables()
    get_provider().latency_ms = 50  # a realistic gap between reserve and settle
    with LiveServer() as base_url, httpx.Client(base_url=base_url, timeout=60) as http:
        key = _tenant(http, "pro", BURST_BUDGET_USD)
        body = {"text": _document(1_000), "style": "bullets", "max_words": 100}
        with ThreadPoolExecutor(max_workers=BURST) as pool:
            statuses = list(
                pool.map(
                    lambda _: (
                        http.post(
                            "/v1/summarize", json=body, headers={"X-API-Key": key}
                        ).status_code
                    ),
                    range(BURST),
                )
            )
    with SessionLocal() as db:
        bp = db.scalars(select(BudgetPeriod)).one()
        ledger_total = int(db.scalar(select(func.sum(UsageLedger.cost_microusd))) or 0)
    print(
        f"{label:<22} admitted {statuses.count(200):>2}, 402 {statuses.count(402):>2}, "
        f"other {BURST - statuses.count(200) - statuses.count(402)}; "
        f"spent ${microusd_to_usd(bp.spent_microusd):.6f} of ${microusd_to_usd(bp.hard_limit_microusd):.6f} "
        f"({bp.spent_microusd / bp.hard_limit_microusd:.0%}), ledger ${microusd_to_usd(ledger_total):.6f}, "
        f"reserved {bp.reserved_microusd}"
    )


class LiveServer:
    """uvicorn on a free local port in a background thread."""

    def __enter__(self) -> str:
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.05)
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def burst() -> None:
    if engine.dialect.name != "postgresql":
        sys.exit("burst needs Postgres (SQLite serialises writers); set DATABASE_URL")
    if get_settings().llm_provider != "mock":
        sys.exit("burst uses the mock provider only; unset LLM_PROVIDER")
    print(f"{BURST} concurrent requests, ${BURST_BUDGET_USD} budget, Postgres")
    _burst_once("atomic reserve (D2)")
    atomic = budget.reserve
    budget.reserve = _check_then_call
    try:
        _burst_once("check spent, then call")
    finally:
        budget.reserve = atomic


if __name__ == "__main__":
    {"costs": costs, "burst": burst}[sys.argv[1] if len(sys.argv) > 1 else "costs"]()
