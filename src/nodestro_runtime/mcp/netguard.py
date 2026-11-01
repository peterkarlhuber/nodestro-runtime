"""Outbound network protection for MCP connections (threat model T7).

Every connection the MCP client opens goes through :class:`GuardedBackend`: the connection step itself resolves the
host name, checks every resolved address and connects only to an address that passed. The address checked is the
address connected, so there is no gap between resolution and connect (DNS rebinding, answers that change between
calls). TLS still verifies the configured host name: the HTTP layer passes the name as SNI and verification name, the
socket uses the checked address.

:class:`EgressPolicy` adds the request-level rules: ``https`` only (loopback ``http`` only with explicit consent),
refused metadata host names, the operator's egress allow-list (``NODESTRO_MCP_ENDPOINTS``). Environment proxy
variables are ignored; the HTTP client is built with ``trust_env=False``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
import ipaddress
import os
import re
import socket
from typing import Any, cast
from urllib.parse import urlsplit

from ..problems import NodestroError

#: Explicitly refused, whatever range they are in (cloud metadata services).
METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(value)
    for value in ("169.254.169.254", "169.254.170.2", "169.254.170.23", "fd00:ec2::254", "100.100.100.200")
)
METADATA_HOSTS = frozenset({"metadata", "metadata.google.internal", "metadata.azure.com", "instance-data"})
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_6TO4 = ipaddress.ip_network("2002::/16")
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})

#: The operator's egress allow-list: comma-separated origins (``https://host[:port]``).
#: The most bytes one HTTP response of an MCP server (JSON-RPC, SSE stream, OAuth and metadata documents) may have.
#: Counted on the wire while it is read, before anything is parsed; compressed answers are not accepted, so the bytes
#: counted are the bytes parsed (independent review M-2, threat model T11).
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

ENDPOINTS_ENV = "NODESTRO_MCP_ENDPOINTS"

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]


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
    if address.is_private:
        return "private"
    if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network("100.64.0.0/10"):
        return "shared"
    if address.is_reserved:
        return "reserved"
    if not address.is_global:
        return "not_global"
    return None


def _normal_origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        raise ValueError(url)
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def parse_endpoints(text: str | None) -> tuple[tuple[str, str, int], ...] | None:
    """The allow-list from ``NODESTRO_MCP_ENDPOINTS``; ``None`` when the variable is not set."""
    if text is None:
        return None
    origins = []
    for item in text.split(","):
        if item.strip():
            try:
                origins.append(_normal_origin(item))
            except ValueError:
                raise NodestroError(
                    "mcp.endpoints_invalid",
                    f"{ENDPOINTS_ENV} contains an invalid origin: {item.strip()}",
                    value=item.strip(),
                ) from None
    return tuple(origins)


@dataclass(frozen=True)
class EgressPolicy:
    """Which MCP endpoints a workspace may reach.

    ``allowlist``: allowed origins; ``None`` means no operator list (desktop), an empty tuple means no MCP at all.
    ``require_allowlist``: a server profile must have a list (empty = none); a desktop may have one."""

    allowlist: tuple[tuple[str, str, int], ...] | None = None
    require_allowlist: bool = False
    #: Loopback ``http`` endpoints are possible at all (desktop); each server still needs its own consent.
    loopback_possible: bool = True

    @classmethod
    def from_env(
        cls, *, server: bool, loopback: bool | None = None, environ: dict[str, str] | None = None
    ) -> EgressPolicy:
        """``server``: a server tenant (the list is required); ``loopback``: local development servers are possible
        (the desktop only: inside a container, loopback is the container itself)."""
        env = os.environ if environ is None else environ
        allowlist = parse_endpoints(env.get(ENDPOINTS_ENV))
        if server and allowlist is None:
            allowlist = ()
        possible = (not server) if loopback is None else loopback and not server
        return cls(allowlist=allowlist, require_allowlist=server, loopback_possible=possible)

    def check_url(self, url: str, *, allow_loopback: bool = False) -> tuple[str, str, int]:
        """Refuse a URL the client must never contact; returns its normalized origin."""
        try:
            scheme, host, port = _normal_origin(url)
        except ValueError:
            raise NodestroError(
                "mcp.url_invalid", f"Not a valid http(s) address: {redact_url(url)}", url=redact_url(url)
            ) from None
        parts = urlsplit(url.strip())
        if parts.username or parts.password:
            raise NodestroError("mcp.url_credentials", "The address must not contain a user name or password")
        if host in METADATA_HOSTS:
            raise NodestroError("mcp.address_refused", f"The address {host} is refused", host=host, reason="metadata")
        loopback = host in _LOOPBACK_NAMES or _is_loopback_literal(host)
        if scheme != "https" and not (loopback and allow_loopback and self.loopback_possible):
            raise NodestroError(
                "mcp.https_required",
                "MCP servers are reached over https only (http only for a local development server on this computer)",
                url=redact_url(url),
            )
        if loopback and not (allow_loopback and self.loopback_possible):
            raise NodestroError("mcp.address_refused", f"The address {host} is refused", host=host, reason="loopback")
        literal = _literal(host)
        if literal is not None:
            reason = address_problem(literal, allow_loopback=allow_loopback and self.loopback_possible)
            if reason is not None:
                raise NodestroError("mcp.address_refused", f"The address {host} is refused", host=host, reason=reason)
        consented_loopback = loopback and allow_loopback and self.loopback_possible
        if self.allowlist is not None and not consented_loopback and (scheme, host, port) not in self.allowlist:
            raise NodestroError(
                "mcp.endpoint_not_allowed",
                f"{scheme}://{host}:{port} is not on the operator's list of allowed MCP endpoints ({ENDPOINTS_ENV})",
                origin=f"{scheme}://{host}:{port}",
            )
        return scheme, host, port


def _literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def is_loopback_url(url: str) -> bool:
    """Whether ``url`` names this computer (a loopback name or literal)."""
    try:
        _scheme, host, _port = _normal_origin(url)
    except ValueError:
        return False
    return host in _LOOPBACK_NAMES or _is_loopback_literal(host)


def _is_loopback_literal(host: str) -> bool:
    literal = _literal(host)
    return literal is not None and address_problem(literal, allow_loopback=True) is None and literal.is_loopback


async def system_resolver(host: str, port: int) -> list[str]:
    import anyio

    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass
class GuardedBackend:
    """An ``httpcore2`` network backend that connects only to checked addresses (see the module documentation)."""

    allow_loopback: bool = False
    resolver: Resolver = system_resolver
    #: Every address connected to, for tests and the audit trail.
    connected: list[str] = field(default_factory=list)
    #: HTTP status codes of the answers (the SDK does not always pass them on; used for stable error codes).
    statuses: list[int] = field(default_factory=list)

    async def resolve(self, host: str, port: int) -> list[IPAddress]:
        literal = _literal(host)
        if literal is not None:
            candidates = [literal]
        else:
            if host.lower().rstrip(".") in METADATA_HOSTS:
                raise NodestroError(
                    "mcp.address_refused", f"The address {host} is refused", host=host, reason="metadata"
                )
            try:
                answers = await self.resolver(host, port)
            except OSError as exc:
                raise NodestroError("mcp.dns_failed", f"The name {host} cannot be resolved", host=host) from exc
            candidates = []
            for answer in answers:
                parsed = _literal(answer.split("%")[0]) if "%" in answer else _literal(answer)
                if parsed is None:
                    continue
                if "%" in answer:  # a scoped (link-local) answer
                    raise NodestroError(
                        "mcp.address_refused", f"{host} resolves to a scoped address", host=host, reason="scoped"
                    )
                candidates.append(parsed)
        if not candidates:
            raise NodestroError("mcp.dns_failed", f"The name {host} has no address", host=host)
        for candidate in candidates:
            reason = address_problem(candidate, allow_loopback=self.allow_loopback)
            if reason is not None:
                # One refused answer refuses the name: a mix of public and internal answers is an attack pattern.
                raise NodestroError(
                    "mcp.address_refused",
                    f"{host} resolves to a refused address ({reason})",
                    host=host,
                    reason=reason,
                )
        return candidates

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> Any:
        import anyio
        from httpcore2 import ConnectError, ConnectTimeout
        from httpcore2._backends.anyio import AnyIOStream

        addresses = await self.resolve(host, port)
        last: Exception | None = None
        for address in addresses:
            try:
                with anyio.fail_after(timeout):
                    stream = await anyio.connect_tcp(str(address), port, local_host=local_address)
            except TimeoutError as exc:
                last = ConnectTimeout(str(exc))
                continue
            except OSError as exc:
                last = ConnectError(str(exc))
                continue
            for option in socket_options or ():
                stream._raw_socket.setsockopt(*option)
            # The connected peer is the checked address: anyio connected to the literal address, no new lookup.
            self.connected.append(str(address))
            return AnyIOStream(stream)
        assert last is not None
        raise last

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> Any:
        raise NodestroError("mcp.address_refused", "Unix sockets are refused", host=path, reason="unix")

    async def sleep(self, seconds: float) -> None:
        import anyio

        await anyio.sleep(seconds)


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


def redact_url(url: str) -> str:
    """``url`` without query and fragment, for events, logs and error texts."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "(invalid address)"
    if not parts.scheme or not parts.netloc:
        return url.split("?", 1)[0].split("#", 1)[0][:200]
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    path = "/".join(
        "…" if len(segment) >= 24 and _looks_secret(segment) else segment for segment in parts.path.split("/")
    )
    hidden = "?…" if parts.query else ""
    return f"{parts.scheme}://{host}{port}{path}{hidden}"


def response_too_large(limit: int) -> NodestroError:
    return NodestroError(
        "mcp.response_too_large",
        f"The MCP server's answer is larger than {limit // (1024 * 1024)} MB and was not read",
        limit=limit,
    )


def _capped_stream(stream: Any, limit: int) -> Any:
    """``stream`` (a response body) that stops with ``mcp.response_too_large`` once more than ``limit`` bytes
    arrived, before the reader holds them all."""
    import httpx2

    class _Capped(httpx2.AsyncByteStream):
        async def __aiter__(self) -> Any:
            total = 0
            async for chunk in stream:
                total += len(chunk)
                if total > limit:
                    raise response_too_large(limit)
                yield chunk

        async def aclose(self) -> None:
            await stream.aclose()

    return _Capped()


def guarded_client(
    policy: EgressPolicy,
    url: str,
    *,
    allow_loopback: bool = False,
    timeout: float = 60.0,
    headers: dict[str, str] | None = None,
    resolver: Resolver | None = None,
    verify: Any = True,
    max_response_bytes: int | None = None,
) -> Any:
    """An ``httpx2.AsyncClient`` for one MCP endpoint: request-level checks plus :class:`GuardedBackend`.

    Every response body is limited to ``max_response_bytes`` on the wire (a larger ``Content-Length`` is refused
    before the body is read) and must not be compressed, so the limit also bounds what is decoded and parsed."""
    import httpcore2
    import httpx2

    limit: int = MAX_RESPONSE_BYTES if max_response_bytes is None else max_response_bytes
    policy.check_url(url, allow_loopback=allow_loopback)
    backend = GuardedBackend(allow_loopback=allow_loopback and policy.loopback_possible)
    if resolver is not None:
        backend.resolver = resolver

    class _Transport(httpx2.AsyncHTTPTransport):
        def __init__(self) -> None:
            super().__init__(trust_env=False, verify=verify, retries=0)
            ssl_context = self._pool._ssl_context
            self._pool = httpcore2.AsyncConnectionPool(
                ssl_context=ssl_context,
                max_connections=8,
                max_keepalive_connections=4,
                network_backend=cast(Any, backend),
            )

        async def handle_async_request(self, request: Any) -> Any:
            # Redirects the SDK follows and every other request are checked against the policy again.
            policy.check_url(str(request.url), allow_loopback=allow_loopback)
            response = await super().handle_async_request(request)
            backend.statuses.append(int(response.status_code))
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            declared = response.headers.get("content-length")
            too_long = declared is not None and declared.strip().isdigit() and int(declared) > limit
            if encoding not in ("", "identity") or too_long:
                await response.aclose()
                if too_long:
                    raise response_too_large(limit)
                raise NodestroError(
                    "mcp.protocol_error",
                    f"The MCP server sent a compressed answer ({encoding}), which is not accepted",
                    detail="content-encoding",
                )
            response.stream = _capped_stream(response.stream, limit)
            return response

    client = httpx2.AsyncClient(
        transport=_Transport(),
        trust_env=False,
        follow_redirects=False,
        timeout=httpx2.Timeout(timeout, connect=min(timeout, 15.0)),
        # No compression: the size limit counts the bytes that are parsed (independent review M-2).
        headers={**(headers or {}), "Accept-Encoding": "identity"},
    )
    client.nodestro_backend = backend  # type: ignore[attr-defined]
    return client
