"""After a burst run: assert the burst tenant's booked spend never exceeded its hard limit."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

host = os.getenv("HOST", "http://localhost:8000")
keys = json.loads((Path(__file__).resolve().parent.parent / ".seed_keys.json").read_text())
u = httpx.get(f"{host}/v1/usage", headers={"X-API-Key": keys["burst"]}).json()
print(json.dumps(u, indent=2))
ok = u["spent_usd"] <= u["limit_usd"] and u["reserved_usd"] == 0
print("quota enforcement:", "HOLDS" if ok else "VIOLATED")
sys.exit(0 if ok else 1)
