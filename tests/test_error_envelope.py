"""The error envelope on routes FastAPI answers itself (404, 405, bad JSON) and the request body cap (413)."""

from app.config import get_settings
from tests.conftest import summarize

JSON = {"Content-Type": "application/json"}


def code(r):
    return r.json()["error"]["code"]


def test_unknown_route_uses_the_error_envelope(client):
    r = client.get("/nope")
    assert r.status_code == 404 and code(r) == "not_found"
    assert r.json()["error"]["request_id"] == r.headers["X-Request-ID"]


def test_wrong_method_uses_the_error_envelope(client):
    r = client.get("/v1/summarize")
    assert r.status_code == 405 and code(r) == "method_not_allowed"
    assert "POST" in r.headers["Allow"]


def test_portal_redirects_still_work(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app"
    r = client.get("/app/", follow_redirects=False)
    assert r.status_code in (301, 302, 307, 308) and r.headers["location"].endswith("/app")


def test_malformed_json_says_so(client, api_key):
    r = client.post(
        "/v1/summarize",
        content='{"text": "abc",',
        headers={**JSON, "X-API-Key": api_key},
    )
    assert r.status_code == 422 and code(r) == "validation_error"
    assert r.json()["error"]["message"].startswith("invalid JSON body")


def test_field_validation_message_is_unchanged(client, api_key):
    r = summarize(client, api_key, max_words=5)
    assert r.status_code == 422 and r.json()["error"]["message"].startswith("max_words:")


def test_oversized_body_is_413_by_content_length(client, api_key, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_request_bytes", 1000)
    r = summarize(client, api_key, text="x" * 2000)
    assert r.status_code == 413 and code(r) == "content_too_large"
    assert r.json()["error"]["request_id"]


def test_oversized_streamed_body_is_413(client, api_key, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_request_bytes", 1000)

    def chunks():  # no Content-Length: the cap must hold on the bytes actually read
        yield b'{"text": "'
        for _ in range(10):
            yield b"x" * 200
        yield b'"}'

    r = client.post(
        "/v1/summarize",
        content=chunks(),
        headers={**JSON, "X-API-Key": api_key},
    )
    assert r.status_code == 413 and code(r) == "content_too_large"


def test_default_cap_fits_the_largest_plan():
    from app.plans import load_plans

    longest = max(p.map_reduce_max_chars or p.max_input_chars for p in load_plans().plans.values())
    # UTF-8 needs up to 4 bytes per character; JSON escaping of a BMP character takes 6.
    assert get_settings().max_request_bytes >= 6 * longest
