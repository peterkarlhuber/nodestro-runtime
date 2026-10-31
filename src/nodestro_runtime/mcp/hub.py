"""The connection hub of one workspace (one tenant in one process): talks to remote MCP servers.

Each operation opens its own short MCP connection through the official SDK (``mcp.Client`` with automatic version
negotiation: ``server/discover`` at 2026-07-28, fallback to the ``initialize`` handshake) over Nodestro's guarded
HTTP client (:mod:`.netguard`), and closes it afterwards. Nothing is kept between operations except the HTTP
client's lifetime-bounded state, so a server restart, a protocol change or a revoked token never leaves a stale
session behind.

The hub runs its own event loop in a background thread; agents (which run in worker threads) call it through the
synchronous methods, the API through the ``a``-prefixed coroutines.

Limits (WP-7, :class:`McpLimits`): calls per tenant and process run at most ``max_parallel`` at a time and wait at
most ``queue_seconds`` for a free slot (``mcp.overloaded``); each server takes at most ``calls_per_minute`` calls
(``mcp.rate_limited``); after ``breaker_failures`` failed calls in a row a server rests for ``breaker_seconds``
(``mcp.cooling_down``) so a dead service cannot tie up the workers. A connection test always runs and ends a rest
when it succeeds.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping
import concurrent.futures
from dataclasses import dataclass, field
import json
import logging
import os
import threading
import time
from typing import Any, ClassVar, TypeVar

from ..problems import NodestroError
from .models import McpServer, ToolDefinition, clean_definition
from .netguard import EgressPolicy, Resolver, guarded_client

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Hard ceilings, whatever a server's settings say.
MAX_TOOLS = 500
MAX_PAGES = 20
_SSE_EVENT_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True)
class McpLimits:
    """Operational limits of one hub (see the module documentation); set by the operator through the environment."""

    max_servers: int = 25
    max_parallel: int = 8
    queue_seconds: float = 10.0
    calls_per_minute: int = 120
    breaker_failures: int = 5
    breaker_seconds: float = 60.0

    ENV: ClassVar[dict[str, str]] = {
        "max_servers": "NODESTRO_MCP_MAX_SERVERS",
        "max_parallel": "NODESTRO_MCP_MAX_PARALLEL",
        "queue_seconds": "NODESTRO_MCP_QUEUE_SECONDS",
        "calls_per_minute": "NODESTRO_MCP_CALLS_PER_MINUTE",
        "breaker_failures": "NODESTRO_MCP_BREAKER_FAILURES",
        "breaker_seconds": "NODESTRO_MCP_BREAKER_SECONDS",
    }

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> McpLimits:
        environ = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for name, variable in cls.ENV.items():
            raw = environ.get(variable, "").strip()
            if not raw:
                continue
            default = getattr(cls, name)
            try:
                value = type(default)(raw)
            except ValueError:
                raise NodestroError(
                    "mcp.limits_invalid", f"{variable} must be a number, not {raw!r}", variable=variable
                ) from None
            if value <= 0:
                raise NodestroError("mcp.limits_invalid", f"{variable} must be greater than 0", variable=variable)
            values[name] = value
        return cls(**values)


@dataclass
class ServerHealth:
    """What the hub knows about one server since the process started (no secrets, no arguments)."""

    calls: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    cooling_until: float | None = None
    last_duration_ms: int | None = None
    last_error_code: str | None = None
    recent: deque[float] = field(default_factory=deque)

    def view(self, now: float) -> dict[str, Any]:
        resting = self.cooling_until is not None and self.cooling_until > now
        return {
            "calls": self.calls,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "cooling_down_seconds": round(self.cooling_until - now, 1) if resting and self.cooling_until else 0,
            "last_duration_ms": self.last_duration_ms,
            "last_error_code": self.last_error_code,
        }


#: Failures that say something about the server's health (not about one call's arguments or a refusal by policy).
_HEALTH_FAILURES = frozenset({"mcp.unreachable", "mcp.timeout", "mcp.http_error", "mcp.protocol_error"})


def sdk_available() -> bool:
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


def require_sdk() -> None:
    if not sdk_available():
        raise NodestroError(
            "mcp.unavailable",
            "MCP is not available in this installation (it needs Python 3.11 or later and the 'mcp' package)",
        )


@dataclass
class Discovery:
    protocol_version: str | None
    server_name: str | None
    server_version: str | None
    tools: list[ToolDefinition] = field(default_factory=list)
    #: Tools the server listed but Nodestro could not accept (invalid or too large), with the reason.
    skipped: list[dict[str, str]] = field(default_factory=list)


@dataclass
class CallResult:
    text: str
    is_error: bool
    protocol_version: str | None
    #: The definition the server listed right before the call (compared with the approved one by the caller).
    definition: ToolDefinition | None
    duration_ms: int
    truncated: bool = False
    #: False when the call was not sent: the tool was missing or its listed definition did not match the expected
    #: hash (the comparison happens on the same connection, before the request).
    sent: bool = True


def _find(exc: BaseException, kind: type[BaseException]) -> BaseException | None:
    if isinstance(exc, kind):
        return exc
    for child in getattr(exc, "exceptions", ()) or ():
        found = _find(child, kind)
        if found is not None:
            return found
    for linked in (exc.__cause__, exc.__context__):
        if linked is not None and linked is not exc:
            found = _find(linked, kind)
            if found is not None:
                return found
    return None


def translate(exc: BaseException, server: McpServer, statuses: list[int] | None = None) -> NodestroError:
    """A stable Nodestro error for whatever the SDK, HTTP or the network raised.

    ``statuses`` are the HTTP status codes seen on the connection; the SDK reports a refused request as a generic
    protocol error, so a 401/403 among them means the credentials were refused."""
    found = _find(exc, NodestroError)
    if isinstance(found, NodestroError):
        return found
    import httpx2

    refused = [code for code in statuses or () if code in (401, 403)]
    if refused:
        return NodestroError(
            "mcp.unauthorized", f"{server.name} refused the credentials (HTTP {refused[-1]})", server=server.name
        )

    status = _find(exc, httpx2.HTTPStatusError)
    if isinstance(status, httpx2.HTTPStatusError):
        code = status.response.status_code
        if code in (401, 403):
            return NodestroError(
                "mcp.unauthorized", f"{server.name} refused the credentials (HTTP {code})", server=server.name
            )
        return NodestroError("mcp.http_error", f"{server.name} answered HTTP {code}", server=server.name, status=code)
    if _find(exc, TimeoutError) is not None or _find(exc, httpx2.TimeoutException) is not None:
        return NodestroError(
            "mcp.timeout",
            f"{server.name} did not answer within {server.timeout_seconds:g} seconds",
            server=server.name,
            seconds=server.timeout_seconds,
        )
    if _find(exc, httpx2.TransportError) is not None or _find(exc, OSError) is not None:
        return NodestroError("mcp.unreachable", f"{server.name} cannot be reached", server=server.name)
    from mcp.shared.exceptions import MCPError

    mcp_error = _find(exc, MCPError)
    if isinstance(mcp_error, MCPError):
        message = str(getattr(mcp_error, "message", "") or mcp_error)[:300]
        return NodestroError("mcp.protocol_error", f"{server.name}: {message}", server=server.name, detail=message)
    logger.info("MCP operation on %s failed: %r", server.id, exc)
    return NodestroError(
        "mcp.protocol_error",
        f"{server.name}: unexpected answer ({type(exc).__name__})",
        server=server.name,
        detail=type(exc).__name__,
    )


def _content_text(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append(str(getattr(block, "text", "")))
        elif kind == "image":
            parts.append(f"[image {getattr(block, 'mime_type', None) or getattr(block, 'mimeType', '')}]")
        elif kind == "audio":
            parts.append("[audio]")
        elif kind == "resource_link":
            parts.append(f"[resource link {getattr(block, 'uri', '')}]")
        elif kind == "resource":
            resource = getattr(block, "resource", None)
            text = getattr(resource, "text", None)
            parts.append(str(text) if text is not None else f"[resource {getattr(resource, 'uri', '')}]")
        else:
            parts.append(f"[{kind or 'content'}]")
    return "\n".join(parts)


class McpHub:
    """Remote MCP operations for one workspace (see the module documentation)."""

    def __init__(
        self,
        policy: EgressPolicy,
        *,
        resolver: Resolver | None = None,
        verify: Any = True,
        limits: McpLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self.resolver = resolver
        self.verify = verify
        self.limits = limits or McpLimits()
        self.clock = clock
        self.health: dict[str, ServerHealth] = {}
        self.active_calls = 0
        self._tenant_slots: asyncio.Semaphore | None = None
        #: Tokens kept only in this process (no secure key store, D-3): server id -> token.
        self.session_tokens: dict[str, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._semaphores: dict[str, tuple[int, asyncio.Semaphore]] = {}
        self._closed = False

    # ---- event loop ----------------------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._closed:
                raise NodestroError("mcp.closed", "The MCP hub is closed")
            if self._loop is None:
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def run() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                thread = threading.Thread(target=run, name="nodestro-mcp", daemon=True)
                thread.start()
                ready.wait()
                self._loop, self._thread = loop, thread
            return self._loop

    def _submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        try:
            loop = self._ensure_loop()
        except BaseException:
            coro.close()  # never started: close it so it is not reported as "never awaited"
            raise
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def run(
        self,
        coro: Coroutine[Any, Any, T],
        *,
        timeout: float,
        cancelled: Callable[[], bool] | None = None,
    ) -> T:
        """Run ``coro`` on the hub's loop from a worker thread; cancelled after ``timeout`` or when ``cancelled()``."""
        future = self._submit(coro)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                return future.result(timeout=max(0.0, min(0.2, remaining)))
            except concurrent.futures.TimeoutError:
                if cancelled is not None and cancelled():
                    future.cancel()
                    raise NodestroError("mcp.cancelled", "The MCP call was cancelled with its run") from None
                if remaining <= 0:
                    future.cancel()
                    raise NodestroError(
                        "mcp.timeout", f"No answer within {timeout:g} seconds", seconds=timeout
                    ) from None

    async def acall(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` on the hub's loop from another event loop (the API)."""
        return await asyncio.wrap_future(self._submit(coro))

    def close(self) -> None:
        """Stop the hub: running operations are cancelled, the loop thread ends; later calls raise ``mcp.closed``."""
        with self._lock:
            self._closed = True
            loop, self._loop = self._loop, None
            thread, self._thread = self._thread, None
        if loop is None:
            return

        def stop() -> None:
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.call_soon(loop.stop)

        loop.call_soon_threadsafe(stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    # ---- limits and health (WP-7) ----------------------------------------------------------

    def _health(self, server: McpServer) -> ServerHealth:
        return self.health.setdefault(server.id, ServerHealth())

    def _admit(self, server: McpServer) -> None:
        """Refuse a call to a resting or busy server before any connection is opened."""
        health = self._health(server)
        now = self.clock()
        if health.cooling_until is not None and health.cooling_until > now:
            raise NodestroError(
                "mcp.cooling_down",
                f"{server.name} failed {health.consecutive_failures} times in a row; calls pause for "
                f"{health.cooling_until - now:.0f} seconds",
                server=server.name,
                seconds=round(health.cooling_until - now),
            )
        while health.recent and health.recent[0] <= now - 60:
            health.recent.popleft()
        if len(health.recent) >= self.limits.calls_per_minute:
            raise NodestroError(
                "mcp.rate_limited",
                f"{server.name} already received {self.limits.calls_per_minute} calls in the last minute",
                server=server.name,
                limit=self.limits.calls_per_minute,
            )
        health.recent.append(now)

    def _record(self, server: McpServer, error: NodestroError | None, duration_ms: int | None = None) -> None:
        health = self._health(server)
        health.calls += 1
        health.last_duration_ms = duration_ms
        if error is None or error.code not in _HEALTH_FAILURES:
            health.consecutive_failures = 0
            health.cooling_until = None
            health.last_error_code = error.code if error is not None else None
            return
        health.failures += 1
        health.consecutive_failures += 1
        health.last_error_code = error.code
        if health.consecutive_failures >= self.limits.breaker_failures:
            health.cooling_until = self.clock() + self.limits.breaker_seconds
            logger.warning(
                "MCP server %s failed %d times in a row; pausing calls for %gs",
                server.id,
                health.consecutive_failures,
                self.limits.breaker_seconds,
            )

    def stats(self) -> dict[str, Any]:
        """Operational state for administrators: active calls, limits and per-server health (no secrets)."""
        now = self.clock()
        return {
            "active_calls": self.active_calls,
            "limits": {name: getattr(self.limits, name) for name in McpLimits.ENV},
            "servers": {server_id: health.view(now) for server_id, health in self.health.items()},
        }

    async def _slot(self, server: McpServer) -> asyncio.Semaphore:
        if self._tenant_slots is None:
            self._tenant_slots = asyncio.Semaphore(self.limits.max_parallel)
        slots = self._tenant_slots
        try:
            await asyncio.wait_for(slots.acquire(), timeout=self.limits.queue_seconds)
        except asyncio.TimeoutError:
            raise NodestroError(
                "mcp.overloaded",
                f"All {self.limits.max_parallel} MCP call slots stayed busy for {self.limits.queue_seconds:g} seconds",
                server=server.name,
                limit=self.limits.max_parallel,
            ) from None
        return slots

    # ---- connections ---------------------------------------------------------------------

    def _semaphore(self, server: McpServer) -> asyncio.Semaphore:
        known = self._semaphores.get(server.id)
        if known is None or known[0] != server.max_parallel_calls:
            known = (server.max_parallel_calls, asyncio.Semaphore(server.max_parallel_calls))
            self._semaphores[server.id] = known
        return known[1]

    def _client(self, server: McpServer, token: str | None) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        return guarded_client(
            self.policy,
            server.url,
            allow_loopback=server.allow_loopback,
            timeout=server.timeout_seconds,
            headers=headers,
            resolver=self.resolver,
            verify=self.verify,
        )

    async def _session(self, server: McpServer, token: str | None, operation: Callable[[Any], Awaitable[T]]) -> T:
        require_sdk()
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp_types import Implementation

        from .. import __version__

        http: Any = None
        try:
            async with self._semaphore(server):
                http = self._client(server, token)
                async with http:
                    transport = streamable_http_client(
                        server.url, http_client=http, terminate_on_close=True, max_sse_event_size=_SSE_EVENT_LIMIT
                    )
                    with _deadline(server.timeout_seconds * 2):
                        async with Client(
                            transport,
                            mode="auto",
                            read_timeout_seconds=server.timeout_seconds,
                            cache=None,
                            client_info=Implementation(name="nodestro", version=__version__),
                        ) as client:
                            return await operation(client)
        except NodestroError:
            raise
        except Exception as exc:
            statuses = getattr(getattr(http, "nodestro_backend", None), "statuses", None)
            raise translate(exc, server, statuses) from None

    @staticmethod
    async def _list(client: Any) -> tuple[list[ToolDefinition], list[dict[str, str]]]:
        tools: list[ToolDefinition] = []
        skipped: list[dict[str, str]] = []
        cursor: str | None = None
        for _page in range(MAX_PAGES):
            listing = await client.list_tools(cursor=cursor)
            for entry in listing.tools:
                raw = entry.model_dump(mode="json", by_alias=True, exclude_none=True)
                try:
                    tools.append(clean_definition(raw))
                except (ValueError, KeyError, TypeError) as exc:
                    skipped.append({"name": str(raw.get("name", ""))[:128], "reason": str(exc)[:200]})
                if len(tools) >= MAX_TOOLS:
                    return tools, skipped
            cursor = listing.next_cursor
            if not cursor:
                break
        return tools, skipped

    async def adiscover(self, server: McpServer, token: str | None) -> Discovery:
        async def operation(client: Any) -> Discovery:
            tools, skipped = await self._list(client)
            info = client.server_info
            return Discovery(
                protocol_version=client.protocol_version,
                server_name=getattr(info, "name", None),
                server_version=getattr(info, "version", None),
                tools=tools,
                skipped=skipped,
            )

        try:
            found = await self._session(server, token, operation)
        except NodestroError as exc:
            self._health(server).last_error_code = exc.code
            raise
        health = self._health(server)
        health.consecutive_failures, health.cooling_until, health.last_error_code = 0, None, None
        return found

    def discover(self, server: McpServer, token: str | None) -> Discovery:
        return self.run(self.adiscover(server, token), timeout=server.timeout_seconds * 2 + 5)

    async def acall_tool(
        self,
        server: McpServer,
        token: str | None,
        name: str,
        arguments: dict[str, Any],
        *,
        expected_hash: str | None = None,
    ) -> CallResult:
        """List, compare and call on one connection. With ``expected_hash`` the request is sent only when the tool's
        listed definition has exactly that hash (an approval never covers a definition changed since)."""
        from .models import definition_hash

        async def operation(client: Any) -> CallResult:
            started = time.monotonic()
            tools, _skipped = await self._list(client)
            definition = next((tool for tool in tools if tool.name == name), None)
            if definition is None:
                return CallResult("", True, client.protocol_version, None, 0, sent=False)
            if expected_hash is not None and definition_hash(definition) != expected_hash:
                return CallResult("", True, client.protocol_version, definition, 0, sent=False)
            result = await client.call_tool(name, arguments, read_timeout_seconds=server.timeout_seconds)
            text = _content_text(list(result.content))
            if not text and result.structured_content is not None:
                text = json.dumps(result.structured_content, ensure_ascii=False)
            truncated = len(text) > server.max_output_chars
            if truncated:
                text = text[: server.max_output_chars]
            return CallResult(
                text=text,
                is_error=bool(result.is_error),
                protocol_version=client.protocol_version,
                definition=definition,
                duration_ms=int((time.monotonic() - started) * 1000),
                truncated=truncated,
            )

        self._admit(server)
        slots = await self._slot(server)
        self.active_calls += 1
        started = time.monotonic()
        try:
            found = await self._session(server, token, operation)
        except NodestroError as exc:
            self._record(server, exc)
            raise
        finally:
            self.active_calls -= 1
            slots.release()
        self._record(server, None, int((time.monotonic() - started) * 1000))
        return found

    def call_tool(
        self,
        server: McpServer,
        token: str | None,
        name: str,
        arguments: dict[str, Any],
        *,
        expected_hash: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> CallResult:
        return self.run(
            self.acall_tool(server, token, name, arguments, expected_hash=expected_hash),
            timeout=server.timeout_seconds * 2 + self.limits.queue_seconds + 5,
            cancelled=cancelled,
        )


class _deadline:
    """An overall limit for one MCP connection (discovery probe, handshake and the operation together)."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._scope: Any = None

    def __enter__(self) -> _deadline:
        import anyio

        self._scope = anyio.fail_after(self.seconds)
        self._scope.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._scope.__exit__(*exc)
