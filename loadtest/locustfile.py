"""Burst load test proving quota enforcement holds.

    make seed                      # writes .seed_keys.json with a 'burst' tenant whose budget is tiny
    make dev                       # or point HOST at the deployment
    make loadtest                  # 50 users, 60s, CSV in loadtest/results*
    python loadtest/verify_quota.py

OWNER: Yashraj. Run against Postgres (docker compose / Neon), not SQLite. Publish: p50/p99, req/s,
count of 200 vs 402 vs 429, and the ledger total vs the budget (must never exceed).
Use LLM_PROVIDER=mock on the server for the burst run (free); measure real-provider latency separately
with a small sample.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from locust import HttpUser, between, events, task

KEYS_FILE = Path(__file__).resolve().parent.parent / ".seed_keys.json"
_keys = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else {}
BURST_KEY = os.getenv("BURST_KEY") or _keys.get("burst")
STEADY_KEY = os.getenv("STEADY_KEY") or _keys.get("pro")

DOC = (
    "The quarterly report shows revenue growth across all regions, driven by strong demand "
    "for the new product line and improved retention in the enterprise segment. "
) * 6

STATUS_COUNTS: dict[int, int] = {}


def _count(resp):
    STATUS_COUNTS[resp.status_code] = STATUS_COUNTS.get(resp.status_code, 0) + 1
    # 402 (budget) and 429 (rate limit) are *expected* outcomes here, not failures.
    if resp.status_code in (200, 402, 429):
        resp.success()


class BurstUser(HttpUser):
    """Hammers a tenant with a tiny monthly budget. Expect: a few 200s, then only 402s."""

    weight = 3
    wait_time = between(0, 0.05)

    @task
    def summarize(self):
        if not BURST_KEY:
            return
        with self.client.post(
            "/v1/summarize",
            json={"text": DOC, "max_words": 80},
            headers={"X-API-Key": BURST_KEY},
            catch_response=True,
            name="burst",
        ) as r:
            _count(r)


class SteadyUser(HttpUser):
    """Normal pro-plan traffic, for latency/throughput numbers."""

    weight = 1
    wait_time = between(0.5, 1.5)

    @task
    def summarize(self):
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


@events.quitting.add_listener
def _report(environment, **kw):
    print("\nstatus code distribution:", dict(sorted(STATUS_COUNTS.items())))
