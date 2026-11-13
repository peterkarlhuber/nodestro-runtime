"""Address rules for outbound connections that need no network client: which IP addresses may never be contacted,
and whether a URL carries a credential.

Shared by the MCP client's network guard (:mod:`nodestro_runtime.mcp.netguard`, threat model T7, which re-exports these
names) and service connections (:mod:`nodestro_runtime.connections`, ADR-0001). Moved here unchanged from the guard so
the core runtime does not import the MCP client.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

#: Explicitly refused, whatever range they are in (cloud metadata services).
METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(value)
    for value in ("169.254.169.254", "169.254.170.2", "169.254.170.23", "fd00:ec2::254", "100.100.100.200")
)
METADATA_HOSTS = frozenset({"metadata", "metadata.google.internal", "metadata.azure.com", "instance-data"})
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_6TO4 = ipaddress.ip_network("2002::/16")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _embedded_v4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries (mapped, NAT64, 6to4, IPv4-compatible), if any."""
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in _NAT64 or address in _NAT64_LOCAL:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if address.sixtofour is not None:
        return address.sixtofour
    packed = int(address)
    if packed >> 32 == 0 and packed > 1:  # IPv4-compatible ::a.b.c.d (deprecated, still routable by some stacks)
        return ipaddress.IPv4Address(packed)
    return None


def address_problem(address: IPAddress, *, allow_loopback: bool = False) -> str | None:
    """Why ``address`` may not be connected to, or ``None`` when it is a global unicast address.

    ``allow_loopback`` permits loopback addresses only (a desktop's local development server with consent)."""
    if address in METADATA_ADDRESSES:
        return "metadata"
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id:
            return "scoped"
        embedded = _embedded_v4(address)
        if embedded is not None:
            return address_problem(embedded, allow_loopback=allow_loopback)
    if address.is_loopback:
        return None if allow_loopback else "loopback"
    if address.is_unspecified:
        return "unspecified"
    if address.is_multicast:
        return "multicast"
    if address.is_link_local:
        return "link_local"
    if isinstance(address, ipaddress.IPv6Address) and address.is_site_local:
        # Deprecated site-local fec0::/10 (RFC 3879): internal by definition, but ``is_global`` calls it global.
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


_SECRET_NAMES = re.compile(
    r"(key|token|secret|passw|pwd|auth|signature|sig$|credential|session|bearer|jwt|access|private|code$)", re.I
)
_SECRET_VALUE = re.compile(r"^[A-Za-z0-9_\-.~+/=]{20,}$")


def _looks_secret(value: str) -> bool:
    """A long token-like value: at least 20 characters of a token alphabet with letters and digits mixed."""
    value = value.strip()
    if not _SECRET_VALUE.match(value):
        return False
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def url_secret_problem(url: str) -> str | None:
    """Why ``url`` seems to carry a credential (``query``, ``path``, ``fragment``), or ``None``.

    Server addresses are stored and shown in the clear, so keys belong in the sealed token, never in the address
    (ADR-0007 D-3; independent review M-1). Query parameters with credential-like names or token-like values, long
    token-like path segments and any fragment are refused."""
    from urllib.parse import parse_qsl

    parts = urlsplit(url.strip())
    if parts.fragment:
        return "fragment"
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if _SECRET_NAMES.search(name) or _looks_secret(value):
            return "query"
    if parts.query and not parse_qsl(parts.query, keep_blank_values=True) and _looks_secret(parts.query):
        return "query"
    for segment in parts.path.split("/"):
        if len(segment) >= 24 and _looks_secret(segment):
            return "path"
    return None


__all__ = ["METADATA_ADDRESSES", "METADATA_HOSTS", "IPAddress", "address_problem", "url_secret_problem"]
