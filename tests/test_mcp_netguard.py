"""Threat model T7: the MCP client's outbound network protection."""

from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path

import pytest

from nodestro_runtime.mcp.netguard import EgressPolicy, GuardedBackend, address_problem, parse_endpoints
from nodestro_runtime.problems import NodestroError

REFUSED = {
    "127.0.0.1": "loopback",
    "127.255.0.9": "loopback",
    "::1": "loopback",
    "10.0.0.1": "private",
    "172.16.5.4": "private",
    "192.168.1.1": "private",
    "169.254.169.254": "metadata",
    "169.254.170.2": "metadata",
    "fd00:ec2::254": "metadata",
    "100.100.100.200": "metadata",
    "169.254.10.10": "link_local",
    "fe80::1": "link_local",
    "fc00::1": "private",
    "100.64.0.1": "shared",
    "0.0.0.0": "unspecified",
    "::": "unspecified",
    "224.0.0.1": "multicast",
    "ff02::1": "multicast",
    "::ffff:127.0.0.1": "loopback",
    "::ffff:10.1.2.3": "private",
    "64:ff9b::7f00:1": "loopback",
    "64:ff9b::a9fe:a9fe": "metadata",
    "2002:7f00:1::": "loopback",
    "::127.0.0.1": "loopback",
    "192.0.2.1": "private",
    "240.0.0.1": "private",
}


@pytest.mark.parametrize(("value", "reason"), sorted(REFUSED.items()))
def test_internal_addresses_are_refused(value: str, reason: str) -> None:
    assert address_problem(ipaddress.ip_address(value)) == reason


@pytest.mark.parametrize("value", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_global_addresses_pass(value: str) -> None:
    assert address_problem(ipaddress.ip_address(value)) is None


def test_loopback_consent_allows_only_loopback() -> None:
    assert address_problem(ipaddress.ip_address("127.0.0.1"), allow_loopback=True) is None
    assert address_problem(ipaddress.ip_address("::1"), allow_loopback=True) is None
    assert address_problem(ipaddress.ip_address("10.0.0.1"), allow_loopback=True) == "private"
    assert address_problem(ipaddress.ip_address("169.254.169.254"), allow_loopback=True) == "metadata"


def code(call: object) -> str:
    with pytest.raises(NodestroError) as caught:
        call()  # type: ignore[operator]
    return caught.value.code


def test_urls_are_checked() -> None:
    desktop = EgressPolicy()
    assert desktop.check_url("https://mcp.example.com/mcp") == ("https", "mcp.example.com", 443)
    assert code(lambda: desktop.check_url("http://mcp.example.com/mcp")) == "mcp.https_required"
    assert code(lambda: desktop.check_url("https://user:pw@mcp.example.com/mcp")) == "mcp.url_credentials"
    assert code(lambda: desktop.check_url("https://metadata.google.internal/")) == "mcp.address_refused"
    assert code(lambda: desktop.check_url("https://169.254.169.254/")) == "mcp.address_refused"
    assert code(lambda: desktop.check_url("https://[::ffff:10.0.0.1]/")) == "mcp.address_refused"
    assert code(lambda: desktop.check_url("https://localhost/mcp")) == "mcp.address_refused"
    assert code(lambda: desktop.check_url("http://localhost:8000/mcp")) == "mcp.https_required"
    assert desktop.check_url("http://localhost:8000/mcp", allow_loopback=True)
    assert desktop.check_url("http://127.0.0.1:8000/mcp", allow_loopback=True)
    assert code(lambda: desktop.check_url("ftp://mcp.example.com")) == "mcp.url_invalid"


def test_server_profile_needs_the_operator_list() -> None:
    server = EgressPolicy.from_env(server=True, environ={})
    assert code(lambda: server.check_url("https://mcp.example.com/mcp")) == "mcp.endpoint_not_allowed"
    # No loopback consent on a server, ever.
    assert code(lambda: server.check_url("http://127.0.0.1:8000/mcp", allow_loopback=True)) == "mcp.https_required"
    listed = EgressPolicy.from_env(server=True, environ={"NODESTRO_MCP_ENDPOINTS": "https://mcp.example.com"})
    assert listed.check_url("https://mcp.example.com/mcp")
    assert code(lambda: listed.check_url("https://mcp.example.com:8443/mcp")) == "mcp.endpoint_not_allowed"
    assert code(lambda: listed.check_url("https://other.example.com/mcp")) == "mcp.endpoint_not_allowed"
    # An entry never allows a refused address.
    bad = EgressPolicy.from_env(server=True, environ={"NODESTRO_MCP_ENDPOINTS": "https://10.0.0.5"})
    assert code(lambda: bad.check_url("https://10.0.0.5/mcp")) == "mcp.address_refused"
    with pytest.raises(NodestroError):
        parse_endpoints("not a url")


def resolver_from(answers: dict[str, list[list[str]]]):  # type: ignore[no-untyped-def]
    """A resolver whose answers change per lookup (rebinding)."""
    calls: dict[str, int] = {}

    async def resolve(host: str, port: int) -> list[str]:
        index = calls.get(host, 0)
        calls[host] = index + 1
        rounds = answers[host]
        return rounds[min(index, len(rounds) - 1)]

    return resolve


def test_every_answer_is_checked_and_rebinding_is_caught() -> None:
    backend = GuardedBackend(
        resolver=resolver_from(
            {
                "public.test": [["93.184.216.34"]],
                "mixed.test": [["93.184.216.34", "10.0.0.1"]],
                "rebind.test": [["93.184.216.34"], ["127.0.0.1"]],
                "v6.test": [["::ffff:169.254.169.254"]],
            }
        )
    )
    assert asyncio.run(backend.resolve("public.test", 443)) == [ipaddress.ip_address("93.184.216.34")]
    assert code(lambda: asyncio.run(backend.resolve("mixed.test", 443))) == "mcp.address_refused"
    assert code(lambda: asyncio.run(backend.resolve("v6.test", 443))) == "mcp.address_refused"
    # The first connection gets the public answer, the next one (pool refill, redirect) the rebound one.
    assert asyncio.run(backend.resolve("rebind.test", 443))
    assert code(lambda: asyncio.run(backend.resolve("rebind.test", 443))) == "mcp.address_refused"


# ---- end to end through the HTTP client ---------------------------------------------------------------------


def _get(client, url: str) -> int:  # type: ignore[no-untyped-def]
    async def go() -> int:
        async with client:
            response = await client.get(url)
            return int(response.status_code)

    return asyncio.run(go())


@pytest.fixture
def plain_server():  # type: ignore[no-untyped-def]
    pytest.importorskip("mcp")
    from mcp_support import serve

    server = serve()
    yield server
    server.stop()


def test_a_name_resolving_to_loopback_never_connects(plain_server) -> None:  # type: ignore[no-untyped-def]
    from nodestro_runtime.mcp.netguard import guarded_client

    async def to_loopback(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    url = f"https://rebind.test:{plain_server.port}/mcp"
    client = guarded_client(EgressPolicy(), url, resolver=to_loopback)
    assert code(lambda: _get(client, url)) == "mcp.address_refused"
    assert plain_server.requests == []


def test_environment_proxies_are_ignored(plain_server, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from nodestro_runtime.mcp.netguard import guarded_client

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    url = f"http://127.0.0.1:{plain_server.port}/mcp"
    client = guarded_client(EgressPolicy(), url, allow_loopback=True)
    _get(client, url)
    assert plain_server.requests, "the request went straight to the server, not to the proxy"


def test_redirects_to_other_origins_are_checked_again() -> None:
    from nodestro_runtime.mcp.netguard import guarded_client

    policy = EgressPolicy(allowlist=(("https", "mcp.example.com", 443),))
    client = guarded_client(policy, "https://mcp.example.com/mcp")

    async def go() -> None:
        async with client:
            await client.get("https://other.example.com/mcp")

    assert code(lambda: asyncio.run(go())) == "mcp.endpoint_not_allowed"


def test_tls_verifies_the_configured_name_not_the_address(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    import ssl

    from mcp_support import self_signed, serve

    from nodestro_runtime.mcp.netguard import guarded_client

    cert, key, ca = self_signed(tmp_path, "good.test")
    server = serve(ssl=(cert, key))
    try:
        context = ssl.create_default_context(cafile=ca)

        async def to_loopback(host: str, port: int) -> list[str]:
            return ["127.0.0.1"]

        good = f"https://good.test:{server.port}/mcp"
        client = guarded_client(EgressPolicy(), good, allow_loopback=True, resolver=to_loopback, verify=context)
        assert _get(client, good) >= 200  # any HTTP answer: the TLS handshake with the name succeeded
        # Same address, different name: the certificate does not match, the connection fails.
        other = f"https://other.test:{server.port}/mcp"
        client = guarded_client(EgressPolicy(), other, allow_loopback=True, resolver=to_loopback, verify=context)
        with pytest.raises(Exception) as caught:
            _get(client, other)
        assert "certificate" in repr(caught.value).lower() or "ssl" in repr(caught.value).lower()
    finally:
        server.stop()


def test_only_the_desktop_can_consent_to_loopback() -> None:
    container = EgressPolicy.from_env(server=False, loopback=False, environ={})
    assert code(lambda: container.check_url("http://127.0.0.1:8000/mcp", allow_loopback=True)) == ("mcp.https_required")
    desktop = EgressPolicy.from_env(server=False, loopback=True, environ={})
    assert desktop.check_url("http://127.0.0.1:8000/mcp", allow_loopback=True)
