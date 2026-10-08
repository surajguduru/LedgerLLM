from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(tags=["meta"])


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    s = get_settings()
    return {
        "status": "ok",
        "provider": s.llm_provider,
        "guardrails_mode": s.guardrails_mode,
        "version": s.git_commit[:7]
        if s.git_commit
        else None,  # which commit is live, after a deploy
    }
