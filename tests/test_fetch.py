"""URL fetching and the SSRF guard. OWNER: Sai.

No test here touches the real network or real DNS: an autouse fixture replaces
`socket.getaddrinfo` with a table of names, and anything not in the table is resolved
numerically only (`AI_NUMERICHOST`), which still exercises the OS parser for spellings
such as `0x7f.0.0.1` without sending a query.
"""

from __future__ import annotations

import socket

import pytest

from app.feature.fetch import FetchBlocked, FetchError, validate_url
from tests.conftest import summarize

_REAL_GETADDRINFO = socket.getaddrinfo

PUBLIC_V4 = "93.184.215.14"
PUBLIC_V6 = "2606:4700::1111"


@pytest.fixture(autouse=True)
def dns(monkeypatch) -> dict[str, list[str]]:
    """Name -> addresses. Tests add entries; unknown names fail like NXDOMAIN."""
    table: dict[str, list[str]] = {
        "localhost": ["127.0.0.1", "::1"],
        "example.com": [PUBLIC_V4],
    }

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host in table:
            out = []
            for addr in table[host]:
                if ":" in addr:
                    out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, port, 0, 0)))
                else:
                    out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port)))
            return out
        return _REAL_GETADDRINFO(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return table


# --- validate_url ----------------------------------------------------------------------------


def test_public_address_is_allowed():
    assert [str(ip) for ip in validate_url("https://example.com/article")] == [PUBLIC_V4]


def test_public_ipv6_literal_is_allowed():
    assert [str(ip) for ip in validate_url(f"http://[{PUBLIC_V6}]/")] == [PUBLIC_V6]


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",  # RFC 1918
        "172.16.0.1",  # RFC 1918
        "192.168.1.1",  # RFC 1918
        "127.0.0.1",  # loopback
        "[::1]",  # IPv6 loopback
        "169.254.169.254",  # cloud metadata (link-local)
        "[fe80::1]",  # IPv6 link-local
        "100.64.0.1",  # CGNAT shared address space
        "0.0.0.0",  # unspecified
        "[::]",  # IPv6 unspecified
        "240.0.0.1",  # reserved
        "224.0.0.1",  # multicast
        "[ff02::1]",  # IPv6 multicast
        "[fd00::1]",  # unique local
        "[::ffff:127.0.0.1]",  # IPv4-mapped loopback
        "[::ffff:169.254.169.254]",  # IPv4-mapped metadata
    ],
)
def test_non_public_addresses_are_blocked(host):
    with pytest.raises(FetchBlocked):
        validate_url(f"http://{host}/")


@pytest.mark.parametrize("host", ["2130706433", "0x7f.0.0.1", "127.1", "017700000001"])
def test_numeric_loopback_spellings_are_blocked(host):
    with pytest.raises(FetchBlocked):
        validate_url(f"http://{host}/")


def test_name_resolving_to_any_private_address_is_blocked(dns):
    dns["mixed.example"] = [PUBLIC_V4, "10.0.0.5"]
    with pytest.raises(FetchBlocked):
        validate_url("http://mixed.example/")


def test_name_resolving_to_metadata_is_blocked(dns):
    dns["metadata.attacker.example"] = ["169.254.169.254"]
    with pytest.raises(FetchBlocked):
        validate_url("http://metadata.attacker.example/latest/meta-data/")


@pytest.mark.parametrize(
    "url", ["ftp://example.com/file.txt", "file:///etc/passwd", "gopher://example.com/"]
)
def test_non_http_schemes_are_blocked(url):
    with pytest.raises(FetchBlocked):
        validate_url(url)


@pytest.mark.parametrize("url", ["http://user@example.com/", "http://user:pw@example.com/"])
def test_credentials_in_url_are_blocked(url):
    with pytest.raises(FetchBlocked):
        validate_url(url)


def test_empty_host_is_blocked():
    with pytest.raises(FetchBlocked):
        validate_url("http:///path")


def test_unresolvable_host_is_a_fetch_error_not_a_block():
    with pytest.raises(FetchError) as info:
        validate_url("http://does-not-exist.invalid/")
    assert not isinstance(info.value, FetchBlocked)


def test_block_message_does_not_echo_the_resolved_address(dns):
    dns["internal.example"] = ["10.1.2.3"]
    with pytest.raises(FetchBlocked) as info:
        validate_url("http://internal.example/")
    assert "10.1.2.3" not in str(info.value)


# --- pipeline --------------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="TODO(Sai): SSRF guard — private/loopback/metadata IPs must be refused with fetch_blocked",
    strict=False,
)
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/healthz",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://localhost/",
    ],
)
def test_ssrf_targets_are_blocked(client, api_key, url):
    r = summarize(client, api_key, text=None, url=url)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "fetch_blocked"


def test_non_http_scheme_rejected(client, api_key):
    # pydantic's HttpUrl already rejects non-http(s) schemes at validation time
    r = summarize(client, api_key, text=None, url="ftp://example.com/file.txt")
    assert r.status_code == 422


def test_text_longer_than_plan_limit_is_truncated_and_flagged(client):
    from tests.conftest import make_tenant

    key = make_tenant(client, plan="free")["api_key"]  # 20k chars
    r = summarize(client, key, text="word " * 6000)
    assert r.status_code == 200
    assert r.json()["source"]["truncated"] is True
    assert r.json()["source"]["chars"] == 20000
