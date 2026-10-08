"""No request holds a database connection while it waits on the network or for a thread.

The pipeline makes calls that take seconds: the URL fetch (stage 4), the guardrail classifier
(stage 6) and the model (stage 7, once per map-reduce chunk, plus a fallback). A transaction left
open across one of them keeps the request's pooled connection checked out for the whole call. With
10 connections per process, the pool then caps concurrent requests at 10, and every request behind
them, including ones that would be rate-limited or served from cache, waits for a connection and
fails with 500 (docs/MEASUREMENTS.md). The same goes for the gap between the auth dependency and the
endpoint, which run on different worker threads: under load the endpoint waits for a thread.

Each stub here notes how many connections are checked out at the moment it is called; tests send
one request at a time, so anything but 0 is that request's.
"""

from dataclasses import replace

import pytest
from sqlalchemy import event

from app.config import get_settings
from app.db import engine
from app.feature.fetch import FetchedPage
from app.guardrails import llm_classifier
from app.guardrails.input import CASCADE_METHOD
from app.llm.base import ProviderError
from app.llm.fallback import FallbackProvider
from app.llm.mock import MockProvider
from tests.conftest import SAMPLE_TEXT, make_tenant, summarize
from tests.test_guardrail_llm import UNCERTAIN
from tests.test_longdoc import _paragraphs

PRO_LONG = _paragraphs(450)  # ~143k chars: 3 map calls and a reduce on the pro plan
SECONDARY = "gemini-3.5-flash-lite"  # on the pro plan


@pytest.fixture
def held():
    """How many pooled connections are checked out right now."""
    count = [0]

    def out(*_):
        count[0] += 1

    def back(*_):
        count[0] -= 1

    event.listen(engine, "checkout", out)
    event.listen(engine, "checkin", back)
    yield lambda: count[0]
    event.remove(engine, "checkout", out)
    event.remove(engine, "checkin", back)


class _Probe(MockProvider):
    """The mock provider, noting the checked-out connection count every time it is called."""

    def __init__(self, held, *, reply: str | None = None, fail: bool = False) -> None:
        super().__init__(latency_ms=0)
        self.held, self.reply, self.fail = held, reply, fail
        self.seen: list[int] = []

    def complete(self, **kwargs):
        self.seen.append(self.held())
        if self.fail:
            raise ProviderError("primary down", retryable=True)
        result = super().complete(**kwargs)
        return result if self.reply is None else replace(result, text=self.reply)


@pytest.fixture
def model(monkeypatch, held):
    p = _Probe(held)
    monkeypatch.setattr("app.api.summarize.get_provider", lambda: p)
    return p


def test_endpoint_starts_with_no_connection_held(client, api_key, model, monkeypatch, held):
    """Auth runs as a dependency, on another thread than the endpoint that follows it."""
    import app.api.summarize as pipeline

    seen: list[int] = []
    real = pipeline._pipeline

    def spy(*args, **kwargs):
        seen.append(held())
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_pipeline", spy)
    assert summarize(client, api_key).status_code == 200
    assert seen == [0]


def test_model_call_holds_no_connection(client, api_key, model):
    assert summarize(client, api_key).status_code == 200
    assert model.seen == [0]


def test_no_map_reduce_call_holds_a_connection(client, api_key, model):
    r = summarize(client, api_key, text=PRO_LONG)
    assert r.status_code == 200, r.text
    assert r.json()["source"]["strategy"] == "map_reduce"
    assert len(model.seen) > 1 and set(model.seen) == {0}


def test_fallback_call_holds_no_connection(client, api_key, monkeypatch, held):
    primary, secondary = _Probe(held, fail=True), _Probe(held)
    chain = FallbackProvider(primary, secondary, secondary_model=SECONDARY)
    monkeypatch.setattr("app.api.summarize.get_provider", lambda: chain)
    monkeypatch.setattr("app.api.summarize.fallback_model", lambda: SECONDARY)
    r = summarize(client, api_key)
    assert r.status_code == 200, r.text
    assert r.json()["usage"]["fallback_from"] is not None
    assert primary.seen == [0] and secondary.seen == [0]


def test_guardrail_classifier_call_holds_no_connection(client, api_key, model, monkeypatch, held):
    monkeypatch.setattr(get_settings(), "guardrail_llm", "on")
    llm_classifier.clear_cache()
    classifier = _Probe(held, reply='{"injection": false, "category": "none", "confidence": 0.9}')
    monkeypatch.setattr(llm_classifier, "get_provider", lambda: classifier)
    try:
        r = summarize(client, api_key, instructions=UNCERTAIN)
    finally:
        llm_classifier.clear_cache()
    assert r.status_code == 200, r.text
    assert r.json()["guardrails"]["input"]["method"] == CASCADE_METHOD
    assert classifier.seen and set(classifier.seen) == {0}
    assert model.seen == [0]


def test_url_fetch_holds_no_connection(client, api_key, model, monkeypatch, held):
    seen: list[int] = []

    def fetch(url, settings):
        seen.append(held())
        return FetchedPage(
            url=url,
            final_url=url,
            title="t",
            text=SAMPLE_TEXT,
            content_type="text/html",
            fetched_ms=1,
        )

    monkeypatch.setattr("app.api.summarize.fetch_url", fetch)
    r = client.post(
        "/v1/summarize",
        json={"url": "https://example.com/a", "style": "bullets", "max_words": 100},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 200, r.text
    assert seen == [0] and model.seen == [0]


def test_request_that_crosses_the_soft_warning_holds_no_connection(client, model):
    """Crossing 80 % takes the soft-warning claim, a second write inside `reserve`."""
    key = make_tenant(client, plan="pro", budget_override_usd=0.004)["api_key"]
    warned = False
    for _ in range(10):
        r = summarize(client, key)
        if r.status_code != 200:
            break
        warned = warned or "X-Budget-Warning" in r.headers
    assert warned
    assert set(model.seen) == {0}
