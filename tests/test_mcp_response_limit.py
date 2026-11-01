"""Threat model T11, independent review M-2: an MCP server's answers are limited before they are read or parsed."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Any

import pytest

from nodestro_runtime.mcp.netguard import EgressPolicy, guarded_client
from nodestro_runtime.problems import NodestroError

LIMIT = 64 * 1024


class _State:
    sent = 0


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen_encoding = self.headers.get("Accept-Encoding")  # type: ignore[attr-defined]
        if self.path == "/declared":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(100 * 1024 * 1024))
            self.end_headers()
            return  # never sends the body
        if self.path == "/gzip":
            body = b"\x1f\x8b" + b"0" * 32
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # /endless: chunked JSON that never ends
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunk = b"[" + b"1," * 8191 + b"1]"
        try:
            for _ in range(10_000):  # up to about 160 MB
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.server.sent += len(chunk)  # type: ignore[attr-defined]
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def http() -> Iterator[Any]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.sent = 0  # type: ignore[attr-defined]
    server.seen_encoding = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _post(server: Any, path: str) -> Any:
    url = f"http://127.0.0.1:{server.server_port}{path}"
    policy = EgressPolicy()
    if not policy.loopback_possible:
        pytest.skip("loopback consent is not possible in this process")

    async def go() -> Any:
        client = guarded_client(policy, url, allow_loopback=True, max_response_bytes=LIMIT, timeout=20)
        async with client:
            response = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            return await response.aread()

    return asyncio.run(go())


def _code(call: Any) -> str:
    with pytest.raises(NodestroError) as caught:
        call()
    return caught.value.code


def test_a_declared_oversized_answer_is_refused_before_its_body(http: Any) -> None:
    assert _code(lambda: _post(http, "/declared")) == "mcp.response_too_large"


def test_an_endless_answer_stops_at_the_limit(http: Any) -> None:
    assert _code(lambda: _post(http, "/endless")) == "mcp.response_too_large"
    # The client stopped reading: the server could not push the whole 160 MB.
    assert http.sent < 64 * 1024 * 1024


def test_compressed_answers_are_not_accepted_and_not_asked_for(http: Any) -> None:
    assert _code(lambda: _post(http, "/gzip")) == "mcp.protocol_error"
    assert http.seen_encoding == "identity"


def test_an_oversized_tool_result_fails_the_call_without_reading_it_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the MCP SDK: a tool answer larger than the limit fails the call with a stable code."""
    pytest.importorskip("mcp")
    from mcp_support import serve

    from nodestro_runtime.mcp import netguard
    from nodestro_runtime.mcp.hub import McpHub
    from nodestro_runtime.mcp.models import McpServer, definition_hash

    server = serve()
    hub = McpHub(EgressPolicy())
    try:
        mcp_server = McpServer(slug="big", name="Big", url=server.url, allow_loopback=True)
        found = hub.discover(mcp_server, None)
        big = next(tool for tool in found.tools if tool.name == "big")
        expected = definition_hash(big)
        ok = hub.call_tool(mcp_server, None, "big", {"size": 1000}, expected_hash=expected)
        assert not ok.is_error
        monkeypatch.setattr(netguard, "MAX_RESPONSE_BYTES", LIMIT)
        hub.close()
        hub = McpHub(EgressPolicy())  # new connections pick up the limit
        with pytest.raises(NodestroError) as caught:
            hub.call_tool(mcp_server, None, "big", {"size": 4 * LIMIT}, expected_hash=expected)
        assert caught.value.code == "mcp.response_too_large", caught.value
    finally:
        hub.close()
        server.stop()
