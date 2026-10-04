"""Create demo tenants + keys. Writes raw keys to .seed_keys.json (gitignored) and prints them.

python -m scripts.seed
"""

from __future__ import annotations

import json
from pathlib import Path

from app.auth.keys import generate_key
from app.compliance import audit as audit_events
from app.compliance.audit import audit
from app.db import SessionLocal, init_db
from app.models import ApiKey, Tenant
from app.plans import usd_to_microusd

SEED = [
    ("free", "Acme (free)", "free", None),
    ("pro", "Globex (pro)", "pro", None),
    ("enterprise", "Initech (enterprise)", "enterprise", None),
    (
        "burst",
        "Burst-test tenant",
        "pro",
        0.02,
    ),  # $0.02 budget: exhausts after a handful of requests
]


def main() -> None:
    init_db()
    out: dict[str, str] = {}
    with SessionLocal() as db:
        for alias, name, plan, override in SEED:
            t = Tenant(
                name=name,
                plan=plan,
                budget_override_microusd=usd_to_microusd(override)
                if override is not None
                else None,
            )
            db.add(t)
            db.flush()
            raw, prefix, digest = generate_key()
            k = ApiKey(tenant_id=t.id, name="seed", key_prefix=prefix, key_hash=digest)
            db.add(k)
            db.flush()
            audit(
                db,
                audit_events.TENANT_CREATED,
                tenant_id=t.id,
                actor="admin",
                details={"seed": True},
            )
            out[alias] = raw
            print(f"{alias:11s} {plan:11s} {t.id}  {raw}")
        db.commit()
    Path(".seed_keys.json").write_text(json.dumps(out, indent=2))
    print("\nwrote .seed_keys.json")


if __name__ == "__main__":
    main()
