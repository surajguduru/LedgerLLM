"""Fetch a URL and extract its main text, without letting the URL reach internal resources.

`POST /v1/summarize` accepts a `url`, so any tenant can make this server send a GET request.
Left alone that is server-side request forgery (SSRF): `http://169.254.169.254/` reads cloud
metadata credentials, `http://127.0.0.1:8000/admin` reaches our own admin API, `http://10.0.0.1/`
reaches the private network. `validate_url` is the guard:

- the scheme must be http or https, the URL must carry no credentials and must name a host;
- the host is resolved with `socket.getaddrinfo`, and the URL is refused if *any* resolved
  address is not globally routable (`ip.is_global` covers private, loopback, link-local, CGNAT
  100.64/10, reserved, unspecified and fd00::/8), is multicast, or is the metadata address.
  IPv4-mapped IPv6 (`::ffff:127.0.0.1`) is unwrapped first. Numeric host spellings such as
  `2130706433`, `0x7f.0.0.1` and `127.1` need no special case: they resolve to loopback.

Refusals raise `FetchBlocked` (400 `fetch_blocked`); a name that does not resolve is an ordinary
`FetchError` (422 `fetch_failed`). Messages say why without naming the resolved address.

OWNER: Sai. Still to do: truncation strategy for long pages (head + tail, map-reduce as a stretch
goal); content-type handling (PDF via pypdf is a stretch goal).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from time import perf_counter

import httpx
import trafilatura

from app.config import Settings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}
# Link-local, so is_global already refuses it; named explicitly because it is the one that matters.
METADATA_ADDRESS = ipaddress.IPv4Address("169.254.169.254")


class FetchError(Exception):
    pass


class FetchBlocked(FetchError):
    """The URL is refused by policy (scheme, credentials or a non-public address), not by failure."""


@dataclass
class FetchedPage:
    url: str | None
    final_url: str | None
    title: str
    text: str
    content_type: str | None
    fetched_ms: int


def _is_forbidden(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip == METADATA_ADDRESS or ip.is_multicast or not ip.is_global


def _resolve(host: str, port: int) -> list[IPAddress]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise FetchError("could not resolve the URL's host") from exc
    # sockaddr[0] is the address; an IPv6 one may carry a "%scope" suffix.
    return [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]


def validate_url(url: str) -> list[IPAddress]:
    """Return the addresses `url` resolves to, or raise FetchBlocked if fetching it is not allowed.

    The URL is parsed with httpx, the library that will fetch it, so the guard and the client
    cannot disagree about which host the URL names.
    """
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise FetchError("invalid URL") from exc
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise FetchBlocked("only http and https URLs can be fetched")
    if parsed.userinfo:
        raise FetchBlocked("URLs with credentials are not allowed")
    if not parsed.host:
        raise FetchBlocked("URL has no host")
    addresses = _resolve(parsed.host, parsed.port or DEFAULT_PORTS[parsed.scheme])
    if not addresses:
        raise FetchError("could not resolve the URL's host")
    if any(_is_forbidden(ip) for ip in addresses):
        raise FetchBlocked("URL resolves to a private, loopback or otherwise non-public address")
    return addresses


def _extract(html: str) -> tuple[str, str]:
    doc = trafilatura.bare_extraction(html, with_metadata=True, include_comments=False)
    if doc is None:
        return "", ""
    title = getattr(doc, "title", None) or (doc.get("title") if isinstance(doc, dict) else "") or ""
    text = getattr(doc, "text", None) or (doc.get("text") if isinstance(doc, dict) else "") or ""
    return title, text


def fetch_url(url: str, settings: Settings) -> FetchedPage:
    t0 = perf_counter()
    validate_url(url)
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
