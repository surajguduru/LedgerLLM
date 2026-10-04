"""Fetch a URL and extract its main text.

OWNER: Sai. Base ships a plain fetch + trafilatura extraction. Sai owns:
- SSRF guard: reject non-http(s) schemes, resolve the host and refuse loopback / private / link-local /
  cloud-metadata (169.254.169.254) addresses, re-check on every redirect hop, cap redirects at 3.
  Raise ApiError(400, "fetch_blocked", ...) — see tests/test_fetch.py (xfail).
- Truncation strategy for long pages (head + tail, or map-reduce as a stretch goal).
- Content-type handling (PDF via pypdf is a stretch goal).
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

import httpx
import trafilatura

from app.config import Settings


class FetchError(Exception):
    pass


@dataclass
class FetchedPage:
    url: str | None
    final_url: str | None
    title: str
    text: str
    content_type: str | None
    fetched_ms: int


def _extract(html: str) -> tuple[str, str]:
    doc = trafilatura.bare_extraction(html, with_metadata=True, include_comments=False)
    if doc is None:
        return "", ""
    title = getattr(doc, "title", None) or (doc.get("title") if isinstance(doc, dict) else "") or ""
    text = getattr(doc, "text", None) or (doc.get("text") if isinstance(doc, dict) else "") or ""
    return title, text


def fetch_url(url: str, settings: Settings) -> FetchedPage:
    t0 = perf_counter()
    headers = {"User-Agent": "LedgerLLM/0.1 (+https://github.com/; summarizer bot)"}
    try:
        with httpx.Client(
            timeout=settings.fetch_timeout_s, follow_redirects=True, headers=headers
        ) as c:
            r = c.get(url)
    except httpx.HTTPError as exc:
        raise FetchError(f"fetch failed: {exc.__class__.__name__}") from exc
    if r.status_code >= 400:
        raise FetchError(f"upstream returned HTTP {r.status_code}")

    body = r.content[: settings.fetch_max_bytes]
    content_type = r.headers.get("content-type", "")
    encoding = r.encoding or "utf-8"
    raw = body.decode(encoding, errors="replace")

    if "html" in content_type or raw.lstrip()[:1] == "<":
        title, text = _extract(raw)
    else:
        title, text = "", raw
    if not text.strip():
        raise FetchError("no extractable text at URL")

    return FetchedPage(
        url=url,
        final_url=str(r.url),
        title=title.strip(),
        text=text.strip(),
        content_type=content_type or None,
        fetched_ms=int((perf_counter() - t0) * 1000),
    )
