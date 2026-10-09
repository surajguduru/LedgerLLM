"""Burst load test proving the budget hard-cutoff holds under concurrency.

    docker compose up -d db                 # Postgres: SQLite serialises writes and hides the race
    make seed                               # writes .seed_keys.json
    make dev                                # or point HOST at the deployment
    make loadtest
    python loadtest/verify_quota.py

Run against Postgres, not SQLite. Publish: p50/p99, req/s, the 200/402/429 split, and
the ledger total vs the budget (must never exceed). Keep LLM_PROVIDER=mock for the burst (free, and
the claim under test is our accounting, not the provider's latency -- decision D8).

WHY THIS IS NOT JUST "HAMMER ONE KEY"
Two things in the pipeline sit in front of the budget and will quietly absorb the whole burst:

  1. The rate limiter (stage 3) is per KEY, while the budget (stage 5) is per TENANT. Hammering one
     key means every request past `plan.rpm` is refused with 429 before the budget is ever consulted,
     so the budget cutoff is never exercised. The limiter shields it.
  2. The response cache (stage 4.5) is an exact match on the request body and returns BEFORE the
     budget reserve, at zero cost. Sending the same document every time means almost every request is
     a free cache hit, so the budget is never spent down.

Measured on 8 Oct: hammering one key with one fixed document produced 120 x 200 and 3,621 x 429 --
and not a single 402. The test passed `verify_quota.py` vacuously, because nothing had been spent.

So to put real concurrent pressure on ONE tenant budget:
  * one key per simulated user, all keys on the same tenant (budgets are per tenant, limits per key),
  * each user paced below its key's own rpm so the limiter stays out of the way,
  * a unique document per request so the cache cannot serve it for free.
Then every refusal is a 402 and the number means what it claims. Separate small user classes keep
exercising the cache and the limiter on purpose, so those paths still show up on the dashboard.
"""

from __future__ import annotations

import itertools
import json
import os
import uuid
from pathlib import Path

import httpx
from locust import HttpUser, between, events, task

KEYS_FILE = Path(__file__).resolve().parent.parent / ".seed_keys.json"
_keys = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else {}

STEADY_KEY = os.getenv("STEADY_KEY") or _keys.get("pro")
RATELIMIT_KEY = os.getenv("RATELIMIT_KEY") or _keys.get("free")  # free plan: rpm 5, easy to trip
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "dev-admin-token-change-me")
BURST_BUDGET_USD = float(os.getenv("BURST_BUDGET_USD", "0.02"))

# Filled in at test start: many keys on one small-budget tenant. Falls back to the single seeded
# 'burst' key if the admin API is not reachable (e.g. no admin token against a deployment).
BURST_KEYS: list[str] = []
_key_cycle: itertools.cycle | None = None

DOC = (
    "The quarterly report shows revenue growth across all regions, driven by strong demand "
    "for the new product line and improved retention in the enterprise segment. "
) * 6

STATUS_COUNTS: dict[int, int] = {}


def _count(resp) -> None:
    STATUS_COUNTS[resp.status_code] = STATUS_COUNTS.get(resp.status_code, 0) + 1
    # 402 (budget) and 429 (rate limit) are *expected* outcomes here, not failures.
    if resp.status_code in (200, 402, 429):
        resp.success()


@events.test_start.add_listener
def provision_burst_tenant(environment, **kw) -> None:
    """One tenant with a tiny budget and one key per user, so the budget is the binding constraint.

    The tenant is created on the enterprise plan for its high per-key rpm (600). That is not to avoid
    testing the limiter -- RateLimitUser does that deliberately -- but so the limiter does not mask
    the budget cutoff this test exists to measure.
    """
    global BURST_KEYS, _key_cycle
    host = environment.host or "http://localhost:8000"
    wanted = getattr(environment.parsed_options, "num_users", None) or 50
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    try:
        with httpx.Client(base_url=host, timeout=30.0) as c:
            r = c.post(
                "/admin/tenants",
                json={
                    "name": f"burst-{uuid.uuid4().hex[:8]}",
                    "plan": "enterprise",
                    "budget_override_usd": BURST_BUDGET_USD,
                },
                headers=admin,
            )
            r.raise_for_status()
            created = r.json()
            tenant_id = created["tenant"]["id"]
            BURST_KEYS = [created["api_key"]]
            for i in range(wanted - 1):
                k = c.post(
                    f"/admin/tenants/{tenant_id}/keys",
                    json={"name": f"burst-{i}"},
                    headers=admin,
                )
                k.raise_for_status()
                BURST_KEYS.append(k.json()["api_key"])
        # verify_quota.py reads this to check the tenant it should audit.
        Path(__file__).resolve().parent.joinpath("burst_tenant.json").write_text(
            json.dumps({"tenant_id": tenant_id, "api_key": BURST_KEYS[0]}, indent=2) + "\n"
        )
        print(
            f"\nprovisioned burst tenant {tenant_id} "
            f"({len(BURST_KEYS)} keys, ${BURST_BUDGET_USD} budget, enterprise plan)\n"
        )
    except Exception as exc:  # noqa: BLE001 - any failure here degrades, it does not stop the run
        BURST_KEYS = [k for k in [_keys.get("burst")] if k]
        print(
            f"\nWARNING could not provision a burst tenant ({type(exc).__name__}: {exc}).\n"
            f"Falling back to the single seeded 'burst' key. With one key the rate limiter will\n"
            f"refuse most requests with 429 before the budget is reached, so the 402 count and\n"
            f"verify_quota.py will NOT be a meaningful test of the budget cutoff.\n"
        )

    _key_cycle = itertools.cycle(BURST_KEYS) if BURST_KEYS else None


class BudgetBurstUser(HttpUser):
    """Races other users for one shared tenant budget. Expect: a few 200s, then only 402s."""

    weight = 8
    # Enterprise allows 600 rpm = 10 req/s per key; ~5 req/s per user keeps the limiter out of it.
    wait_time = between(0.15, 0.25)

    def on_start(self) -> None:
        self.key = next(_key_cycle) if _key_cycle else None

    @task
    def summarize(self) -> None:
        if not self.key:
            return
        # Unique suffix per request: an exact-match cache hit would be free and would never touch
        # the budget, which is the thing under test.
        body = {"text": f"{DOC} [req {uuid.uuid4().hex}]", "max_words": 80}
        with self.client.post(
            "/v1/summarize",
            json=body,
            headers={"X-API-Key": self.key},
            catch_response=True,
            name="burst",
        ) as r:
            _count(r)


class SteadyUser(HttpUser):
    """Normal pro-plan traffic on a repeated document: latency numbers plus real cache hits."""

    weight = 1
    wait_time = between(0.5, 1.5)

    @task
    def summarize(self) -> None:
        if not STEADY_KEY:
            return
        with self.client.post(
            "/v1/summarize",
            json={"text": DOC, "max_words": 80},
            headers={"X-API-Key": STEADY_KEY},
            catch_response=True,
            name="steady",
        ) as r:
            _count(r)


class RateLimitUser(HttpUser):
    """Deliberately trips the limiter on a free-plan key (rpm 5) so 429s are exercised too."""

    weight = 1
    wait_time = between(0, 0.05)

    @task
    def summarize(self) -> None:
        if not RATELIMIT_KEY:
            return
        with self.client.post(
            "/v1/summarize",
            json={"text": DOC, "max_words": 80},
            headers={"X-API-Key": RATELIMIT_KEY},
            catch_response=True,
            name="ratelimit",
        ) as r:
            _count(r)


@events.quitting.add_listener
def _report(environment, **kw) -> None:
    counts = dict(sorted(STATUS_COUNTS.items()))
    print("\nstatus code distribution:", counts)
    if not counts.get(402):
        print(
            "NOTE no 402s: the tenant budget was never exhausted, so this run does not test the\n"
            "     hard cutoff. Check the provisioning warning above."
        )
    # Hand the status split to verify_quota.py. The 402 count is the direct evidence that the hard
    # cutoff actually refused something; spend alone cannot show that, because reserve-then-settle
    # deliberately stops short of the limit (D2) and that shortfall is indistinguishable from
    # "never reached the limit".
    out = Path(__file__).resolve().parent / "burst_tenant.json"
    record = json.loads(out.read_text()) if out.exists() else {}
    record["status_counts"] = {str(k): v for k, v in counts.items()}
    out.write_text(json.dumps(record, indent=2) + "\n")
