"""Guardrails. `rules_version()` fingerprints every rule that decides a verdict, for cache keys."""

from __future__ import annotations

import hashlib
from functools import lru_cache


@lru_cache
def rules_version() -> str:
    """Hash of the input signals, thresholds and output patterns. Changes whenever a rule does."""
    from app.guardrails import input as gin
    from app.guardrails import output as gout

    parts = [
        gin.METHOD,
        gin.CASCADE_METHOD,
        repr(sorted(gin.THRESHOLDS.items())),
        repr(gin.UNCERTAIN_BAND),
    ]
    parts += [f"{s.name}|{s.weight}|{s.pattern.pattern}" for s in gin.SIGNALS]
    parts += [gout.METHOD, gout._CANARIES.pattern, gout._TOXIC.pattern]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]
