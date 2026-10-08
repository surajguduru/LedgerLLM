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

Redirects are where a guard that only checks the first URL fails: a public page answers
`302 Location: http://169.254.169.254/`. `fetch_url` therefore never lets httpx follow redirects;
it follows at most `MAX_REDIRECTS` hops itself, resolving each `Location` against the current URL
and validating the result before requesting it.

Bodies are streamed and reading stops at `settings.fetch_max_bytes`, so a huge (or endless)
response costs at most that much memory and bandwidth instead of being downloaded and sliced.

Known limitation: DNS rebinding (time of check vs time of use). `validate_url` resolves the
name, then httpx resolves it again when it connects; a hostile DNS server with a zero TTL can
answer with a public address the first time and 127.0.0.1 the second. The fixes all move the
check to connect time: an egress proxy that enforces the policy on the address it dials, a
network policy that denies RFC 1918 and metadata ranges, or connecting to the validated address
with an explicit Host header and TLS SNI. The one we would build next is the last: it needs no
infrastructure on the free tier and closes the window completely. It is not built yet because it
needs a custom httpx transport that rewrites the target while still verifying the certificate
against the original host name, which is easy to get subtly wrong and deserves its own change
(docs/DESIGN.md, D18).

Pages longer than the plan allows are shortened (head + tail) or map-reduced in
app/feature/longdoc.py (D20), not here: the fetch returns the whole extracted text.

OWNER: Sai. Still to do: content-type handling (PDF via pypdf is a stretch goal).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from time import perf_counter
from urllib.parse import urljoin

import httpx
import trafilatura

from app.config import Settings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}
# Link-local, so is_global already refuses it; named explicitly because it is the one that matters.
METADATA_ADDRESS = ipaddress.IPv4Address("169.254.169.254")
MAX_REDIRECTS = 3
HEADERS = {"User-Agent": "LedgerLLM/0.1 (+https://github.com/; summarizer bot)"}


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


# IPv6 prefixes that carry an IPv4 address in their last 32 bits. `ipaddress` reports several of these as
# global (64:ff9b::7f00:1 is NAT64 for 127.0.0.1; ::127.0.0.1 is IPv4-compatible loopback), so the
# embedded address must be checked too — on a host with a NAT64 gateway they reach the IPv4 target.
_NAT64_PREFIXES = (
    ipaddress.IPv6Network("64:ff9b::/96"),  # RFC 6052 well-known prefix
    ipaddress.IPv6Network("64:ff9b:1::/48"),  # RFC 8215 local-use prefix
)
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")  # deprecated ::a.b.c.d form


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    out: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        out.append(ip.ipv4_mapped)
    if any(ip in net for net in _NAT64_PREFIXES) or (
        ip in _IPV4_COMPATIBLE and int(ip) > 1  # not :: or ::1, which is_global already refuses
    ):
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip.sixtofour is not None:
        out.append(ip.sixtofour)
    if ip.teredo is not None:
        out.extend(ip.teredo)  # (server, client)
    return out


def _is_forbidden(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if any(_is_forbidden(v4) for v4 in _embedded_ipv4(ip)):
            return True
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


def _validate_hop(url: str, hop: int) -> None:
    try:
        validate_url(url)
    except FetchBlocked as exc:
        if hop == 0:
            raise
        raise FetchBlocked(f"redirect target refused: {exc}") from exc


def _read_capped(r: httpx.Response, max_bytes: int) -> bytes:
    """Read the (decoded) body until `max_bytes`, then stop; the rest is never downloaded."""
    chunks: list[bytes] = []
    size = 0
    for chunk in r.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= max_bytes:
            break
    return b"".join(chunks)[:max_bytes]


def _get(client: httpx.Client, url: str, settings: Settings) -> tuple[httpx.Response, bytes]:
    """GET `url`, following up to MAX_REDIRECTS redirects and validating every target first.

    Returns the final response and at most `settings.fetch_max_bytes` of its body.
    """
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        _validate_hop(current, hop)
        try:
            with client.stream(
                "GET",
                current,
                headers=HEADERS,
                follow_redirects=False,
                timeout=settings.fetch_timeout_s,
            ) as r:
                if r.status_code >= 400:
                    raise FetchError(f"upstream returned HTTP {r.status_code}")
                if not 300 <= r.status_code < 400:
                    return r, _read_capped(r, settings.fetch_max_bytes)
                location = r.headers.get("location")
        except httpx.HTTPError as exc:
            raise FetchError(f"fetch failed: {exc.__class__.__name__}") from exc
        if not location:
            raise FetchError(f"upstream returned HTTP {r.status_code} without a Location header")
        current = urljoin(current, location)
    raise FetchError(f"too many redirects (more than {MAX_REDIRECTS})")


def fetch_url(url: str, settings: Settings, *, client: httpx.Client | None = None) -> FetchedPage:
    """Fetch `url` and extract its text. `client` lets tests inject an httpx.MockTransport."""
    t0 = perf_counter()
    if client is None:
        with httpx.Client(timeout=settings.fetch_timeout_s, follow_redirects=False) as own:
            r, body = _get(own, url, settings)
    else:
        r, body = _get(client, url, settings)

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
