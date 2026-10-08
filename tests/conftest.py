"""Test fixtures. Env is pinned BEFORE importing the app so settings/engine pick up the in-memory DB."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")  # CI sets a Postgres URL
os.environ["LLM_PROVIDER"] = "mock"
os.environ["ADMIN_TOKEN"] = "test-admin"
os.environ["APP_ENV"] = "test"
os.environ["GUARDRAILS_MODE"] = "enforce"
# Most tests repeat one identical request on purpose; tests/test_cache.py switches the cache back on.
os.environ["RESPONSE_CACHE_ENABLED"] = "false"
os.environ["GUARDRAIL_LLM"] = "off"
os.environ["QUALITY_SAMPLE_RATE"] = "0"  # tests/test_online_judge.py opts in explicitly

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402
from app.db import engine  # noqa: E402
from app.main import create_app  # noqa: E402

ADMIN = {"Authorization": "Bearer test-admin"}

SAMPLE_TEXT = (
    "LedgerLLM is a multi-tenant API that exposes a summarization feature behind API keys. "
    "Each tenant has a plan with a requests-per-minute limit and a monthly budget in US dollars. "
    "Every request's token cost is booked to a ledger, and a dashboard shows per-tenant spend. "
    "Inputs are screened for prompt injection, outputs are moderated, and logs are redacted. "
) * 3


@pytest.fixture(autouse=True)
def fresh_db():
    models.Base.metadata.drop_all(bind=engine)
    models.Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def make_tenant(client, plan="pro", budget_override_usd=None, name="t"):
    body = {"name": name, "plan": plan}
    if budget_override_usd is not None:
        body["budget_override_usd"] = budget_override_usd
    r = client.post("/admin/tenants", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def api_key(client):
    return make_tenant(client)["api_key"]


class FixedProvider:
    """Provider stub that answers every call with the same text (e.g. a canary the source lacks)."""

    name = "mock"

    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, *, model, system, user, max_tokens):
        from app.llm.base import LLMResult

        return LLMResult(
            text=self.text, model=model, input_tokens=50, output_tokens=10, latency_ms=1
        )


def use_provider(monkeypatch, provider):
    """Route the pipeline's model calls to `provider` for one test."""
    import app.api.summarize as pipeline

    monkeypatch.setattr(pipeline, "get_provider", lambda: provider)


def summarize(client, key, **overrides):
    body = {"text": SAMPLE_TEXT, "style": "bullets", "max_words": 100}
    body.update(overrides)
    headers = {"X-API-Key": key}
    headers.update(overrides.pop("_headers", {}) if "_headers" in overrides else {})
    return client.post("/v1/summarize", json=body, headers=headers)
