"""Request validation: invalid Unicode, whitespace-only text and admin budget bounds are 422, never 500."""

import json

import pytest

from tests.conftest import ADMIN, SAMPLE_TEXT, make_tenant, summarize

ORIGIN = {"Origin": "http://testserver"}
JSON = {"Content-Type": "application/json"}


def code(r):
    return r.json()["error"]["code"]


@pytest.mark.parametrize("field", ["text", "title", "instructions", "model"])
def test_lone_surrogate_in_any_string_field_is_a_422(client, api_key, field):
    body = json.dumps({"text": SAMPLE_TEXT, field: "abc\ud800def"})  # sent escaped, as \ud800
    r = client.post("/v1/summarize", content=body, headers={**JSON, "X-API-Key": api_key})
    assert r.status_code == 422 and code(r) == "validation_error", r.text


def test_lone_surrogate_in_the_playground_is_a_422(client):
    client.post(
        "/app/api/signup",
        json={"email": "u@example.com", "password": "correct horse battery"},
        headers=ORIGIN,
    )
    key_id = client.post("/app/api/keys", json={"name": "k"}, headers=ORIGIN).json()["key"][
        "key_id"
    ]
    r = client.post(
        "/app/api/playground/summarize",
        content=json.dumps({"text": "abc\ud800def", "key_id": key_id}),
        headers={**ORIGIN, **JSON},
    )
    assert r.status_code == 422 and code(r) == "validation_error", r.text


def test_whitespace_only_text_is_refused_and_not_billed(client, api_key):
    r = summarize(client, api_key, text="   \n\t ")
    assert r.status_code == 422 and code(r) == "validation_error"
    assert "no extractable text" in r.json()["error"]["message"]
    usage = client.get("/v1/usage", headers={"X-API-Key": api_key}).json()
    assert usage["requests"] == 0 and usage["spent_usd"] == 0


@pytest.mark.parametrize("value", [1e30, "Infinity", 1_000_001])
def test_budget_override_must_be_finite_and_bounded(client, value):
    tenant_id = make_tenant(client)["tenant"]["id"]
    raw = f'{{"budget_override_usd": {value}}}'
    r = client.patch(
        f"/admin/tenants/{tenant_id}",
        content=raw,
        headers={**ADMIN, **JSON},
    )
    assert r.status_code == 422 and code(r) == "validation_error", r.text
    r = client.post(
        "/admin/tenants",
        content=f'{{"name": "x", "budget_override_usd": {value}}}',
        headers={**ADMIN, **JSON},
    )
    assert r.status_code == 422 and code(r) == "validation_error", r.text
