"""After a burst run: assert the burst tenant's booked spend never exceeded its hard limit.

Reads loadtest/burst_tenant.json, written by the locustfile when it provisions the burst tenant, and
falls back to the seeded 'burst' key. Exit code 0 means the quota held.

Two things are checked, and the second is the one people forget:
  * spent <= limit            -- the hard cutoff admitted only what fit
  * reserved == 0             -- every reservation was settled or released, so none leaked

A run with no 402s at all passes both trivially while proving nothing, so that is reported as
INCONCLUSIVE rather than as a pass.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
host = os.getenv("HOST", "http://localhost:8000")

provisioned = HERE / "burst_tenant.json"
record: dict = {}
if provisioned.exists():
    record = json.loads(provisioned.read_text())
    key = record["api_key"]
    source = "loadtest/burst_tenant.json"
else:
    key = json.loads((HERE.parent / ".seed_keys.json").read_text())["burst"]
    source = ".seed_keys.json (seeded 'burst' tenant)"

u = httpx.get(f"{host}/v1/usage", headers={"X-API-Key": key}).json()
print(f"usage for {source}:")
print(json.dumps(u, indent=2))

within_limit = u["spent_usd"] <= u["limit_usd"]
nothing_leaked = u["reserved_usd"] == 0

# Did the cutoff actually refuse anything? Use the 402 count from the run, not the spend level:
# reserve-then-settle books worst-case cost and settles for less, so spend deliberately stops short
# of the limit (D2) and a spend threshold cannot tell that apart from "the limit was never reached".
refused = int(record.get("status_counts", {}).get("402", 0))
have_counts = "status_counts" in record

print(f"\n  spent <= limit : {within_limit}  (${u['spent_usd']:.6f} of ${u['limit_usd']:.6f})")
print(f"  reserved == 0  : {nothing_leaked}  (${u['reserved_usd']:.6f})")
if have_counts:
    print(f"  402 refusals   : {refused}")
    headroom = u["limit_usd"] - u["spent_usd"]
    per_request = u["spent_usd"] / u["requests"] if u["requests"] else 0
    if per_request:
        print(
            f"  unspent        : ${headroom:.6f} "
            f"({100 * headroom / u['limit_usd']:.1f}% of budget, "
            f"{headroom / per_request:.1f}x the average request) — reservation pessimism, D2"
        )
else:
    print("  402 refusals   : unknown (no status_counts; run via `make loadtest`)")

if have_counts and refused == 0:
    print(
        "\nquota enforcement: INCONCLUSIVE — nothing was refused with 402, so the hard cutoff was\n"
        "never exercised. Spend staying under the limit proves nothing here. Re-run with more load,\n"
        "or check that the locustfile provisioned a multi-key burst tenant."
    )
    sys.exit(2)

ok = within_limit and nothing_leaked
print("\nquota enforcement:", "HOLDS" if ok else "VIOLATED")
sys.exit(0 if ok else 1)
