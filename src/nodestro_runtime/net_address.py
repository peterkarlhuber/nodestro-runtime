"""Address rules for outbound connections that need no network client: which IP addresses may never be contacted,
and whether a URL carries a credential.

Shared by the MCP client's network guard (:mod:`nodestro_runtime.mcp.netguard`, threat model T7, which re-exports these
names) and service connections (:mod:`nodestro_runtime.connections`, ADR-0001). Since NAL-A2 the rules live in Orkexa's
shared egress guard (:mod:`orkexa.net.guard`, Orkexa ADR 0017; standard library only), so the web tools, MCP and
service connections apply one set of rules. The names here are re-exported unchanged;
``tests/test_net_address_parity.py`` compares them with the earlier rules (parity proof).
"""

from __future__ import annotations

from orkexa.net.guard import METADATA_ADDRESSES as METADATA_ADDRESSES
from orkexa.net.guard import METADATA_HOSTS as METADATA_HOSTS
from orkexa.net.guard import IPAddress as IPAddress
from orkexa.net.guard import _embedded_v4 as _embedded_v4
from orkexa.net.guard import _looks_secret as _looks_secret
from orkexa.net.guard import address_problem as address_problem
from orkexa.net.guard import url_secret_problem as url_secret_problem

__all__ = ["METADATA_ADDRESSES", "METADATA_HOSTS", "IPAddress", "address_problem", "url_secret_problem"]
