"""The connection hub of one workspace (one tenant in one process): talks to remote MCP servers.

Each operation opens its own short MCP connection through the official SDK (``mcp.Client`` with automatic version
negotiation: ``server/discover`` at 2026-07-28, fallback to the ``initialize`` handshake) over Nodestro's guarded
HTTP client (:mod:`.netguard`), and closes it afterwards. Nothing is kept between operations except the HTTP
client's lifetime-bounded state, so a server restart, a protocol change or a revoked token never leaves a stale
session behind.

The hub runs its own event loop in a background thread; agents (which run in worker threads) call it through the
synchronous methods, the API through the ``a``-prefixed coroutines.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
import concurrent.futures
from dataclasses import dataclass, field
import json
import logging
import threading
import time
from typing import Any, TypeVar

from ..problems import NodestroError
from .models import McpServer, ToolDefinition, clean_definition
from .netguard import EgressPolicy, Resolver, guarded_client

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Hard ceilings, whatever a server's settings say.
MAX_TOOLS = 500
MAX_PAGES = 20
_SSE_EVENT_LIMIT = 4 * 1024 * 1024


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

    def __init__(self, policy: EgressPolicy, *, resolver: Resolver | None = None, verify: Any = True) -> None:
        self.policy = policy
        self.resolver = resolver
        self.verify = verify
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
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())

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
        with self._lock:
            self._closed = True
            loop, self._loop = self._loop, None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)

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

        return await self._session(server, token, operation)

    def discover(self, server: McpServer, token: str | None) -> Discovery:
        return self.run(self.adiscover(server, token), timeout=server.timeout_seconds * 2 + 5)

    async def acall_tool(
        self, server: McpServer, token: str | None, name: str, arguments: dict[str, Any]
    ) -> CallResult:
        async def operation(client: Any) -> CallResult:
            started = time.monotonic()
            tools, _skipped = await self._list(client)
            definition = next((tool for tool in tools if tool.name == name), None)
            if definition is None:
                return CallResult("", True, client.protocol_version, None, 0)
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

        return await self._session(server, token, operation)

    def call_tool(
        self,
        server: McpServer,
        token: str | None,
        name: str,
        arguments: dict[str, Any],
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> CallResult:
        return self.run(
            self.acall_tool(server, token, name, arguments),
            timeout=server.timeout_seconds * 2 + 5,
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
