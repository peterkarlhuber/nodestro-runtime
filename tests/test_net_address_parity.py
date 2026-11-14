"""Parity of the shared egress guard (orkexa.net.guard, NAL-A2) with the address rules MCP used before.

The MCP guard (threat model T7) and service connections now import Orkexa's rules. This test keeps the earlier
implementation as the reference and requires the same verdict for every vector; the only allowed difference is a
stricter one (an address the reference allowed and the shared rules refuse). ``tests/test_mcp_netguard.py`` runs
unchanged against the new backend.
"""

from __future__ import annotations

import ipaddress
import itertools

import pytest

from nodestro_runtime.mcp.netguard import GuardedBackend, address_problem
from nodestro_runtime.problems import NodestroError

_METADATA = frozenset(
    ipaddress.ip_address(value)
    for value in ("169.254.169.254", "169.254.170.2", "169.254.170.23", "fd00:ec2::254", "100.100.100.200")
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


def _embedded(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in _NAT64 or address in _NAT64_LOCAL:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if address.sixtofour is not None:
        return address.sixtofour
    packed = int(address)
    if packed >> 32 == 0 and packed > 1:
        return ipaddress.IPv4Address(packed)
    return None


def reference(address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_loopback: bool = False) -> str | None:
    """The runtime's rules before NAL-A2 (``net_address.address_problem`` at 9e0a73d), unchanged."""
    if address in _METADATA:
        return "metadata"
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id:
            return "scoped"
        embedded = _embedded(address)
        if embedded is not None:
            return reference(embedded, allow_loopback=allow_loopback)
    if address.is_loopback:
        return None if allow_loopback else "loopback"
    if address.is_unspecified:
        return "unspecified"
    if address.is_multicast:
        return "multicast"
    if address.is_link_local:
        return "link_local"
    if isinstance(address, ipaddress.IPv6Address) and address.is_site_local:
        return "site_local"
    if address.is_private:
        return "private"
    if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network("100.64.0.0/10"):
        return "shared"
    if address.is_reserved:
        return "reserved"
    if not address.is_global:
        return "not_global"
    return None


VECTORS = [
    # loopback, private, shared, link-local, metadata, unspecified, multicast, reserved, documentation, benchmark
    "127.0.0.1",
    "127.255.255.254",
    "10.0.0.1",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "100.64.0.1",
    "100.127.255.255",
    "169.254.169.254",
    "169.254.170.2",
    "169.254.1.1",
    "100.100.100.200",
    "0.0.0.0",
    "0.1.2.3",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    "192.0.2.1",
    "198.51.100.1",
    "203.0.113.1",
    "198.18.0.1",
    "192.0.0.1",
    "192.88.99.1",
    # public
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",
    "2606:4700:4700::1111",
    "2001:4860:4860::8888",
    # IPv6 internal and embedded IPv4
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "fd00::1",
    "fd00:ec2::254",
    "fec0::1",
    "ff02::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:8.8.8.8",
    "64:ff9b::7f00:1",
    "64:ff9b::808:808",
    "64:ff9b:1::a00:1",
    "2002:7f00:1::",
    "2002:808:808::",
    "::127.0.0.1",
    "::8.8.8.8",
    "2001:db8::1",
    "2001::1",
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    "100::1",
    "fe80::1%1",
]


@pytest.mark.parametrize(("value", "loopback"), list(itertools.product(VECTORS, (False, True))))
def test_the_shared_rules_give_the_earlier_verdict(value: str, loopback: bool) -> None:
    address = ipaddress.ip_address(value)
    before = reference(address, allow_loopback=loopback)
    now = address_problem(address, allow_loopback=loopback)
    if before is None:
        # Only a stricter verdict is allowed (none of the vectors has one today).
        assert now is None, f"{value}: newly refused as {now}"
    else:
        assert now == before, f"{value}: {before} -> {now}"


async def test_the_mcp_backend_keeps_its_error_codes_and_fork() -> None:
    backend = GuardedBackend(resolver=lambda host, port: _answers("10.0.0.5"))
    assert backend.httpcore_module == "httpcore2"
    with pytest.raises(NodestroError) as caught:
        await backend.resolve("internal.example", 443)
    assert (caught.value.code, caught.value.params["reason"]) == ("mcp.address_refused", "private")
    with pytest.raises(NodestroError) as caught:
        await GuardedBackend().resolve("metadata.google.internal", 443)
    assert caught.value.code == "mcp.address_refused"
    with pytest.raises(NodestroError) as caught:
        await GuardedBackend(resolver=lambda host, port: _answers()).resolve("nothing.example", 443)
    assert caught.value.code == "mcp.dns_failed"
    with pytest.raises(NodestroError) as caught:
        await GuardedBackend().connect_unix_socket("/var/run/docker.sock")
    assert caught.value.code == "mcp.address_refused"


async def _answers(*values: str) -> list[str]:
    return list(values)
