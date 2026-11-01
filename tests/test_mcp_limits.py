"""Operational limits of the MCP hub (WP-7): rate limit, overload, resting servers, server count, shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
import threading
import time
from typing import Any

import pytest

from nodestro_runtime.mcp.hub import McpHub, McpLimits
from nodestro_runtime.problems import NodestroError


def test_limits_come_from_the_environment() -> None:
    assert McpLimits.from_env({}) == McpLimits()
    limits = McpLimits.from_env({"NODESTRO_MCP_MAX_PARALLEL": "3", "NODESTRO_MCP_BREAKER_SECONDS": "2.5"})
    assert limits.max_parallel == 3 and limits.breaker_seconds == 2.5
    for bad in ("zero", "0", "-1"):
        with pytest.raises(NodestroError) as caught:
            McpLimits.from_env({"NODESTRO_MCP_CALLS_PER_MINUTE": bad})
        assert caught.value.code == "mcp.limits_invalid"


pytest.importorskip("mcp")

from mcp_support import LoopbackServer, serve  # noqa: E402

from nodestro_runtime.mcp.models import McpServerIn  # noqa: E402
from nodestro_runtime.mcp.netguard import EgressPolicy  # noqa: E402
from nodestro_runtime.mcp.service import McpService  # noqa: E402
from nodestro_runtime.mcp.vault import TokenVault  # noqa: E402
from nodestro_runtime.storage import SQLiteStore  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def server() -> Iterator[LoopbackServer]:
    handle = serve()
    yield handle
    handle.stop()


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Any]:
    opened: list[tuple[McpHub, SQLiteStore]] = []

    def factory(clock: Clock | None = None, **limits: Any) -> McpService:
        store = SQLiteStore(tmp_path / f"db{len(opened)}.sqlite")
        store.migrate(before_migration=None)
        hub = McpHub(EgressPolicy(), limits=McpLimits(**limits), clock=clock or time.monotonic)
        opened.append((hub, store))
        return McpService(store, hub, TokenVault(None, "local"))

    yield factory
    for hub, store in opened:
        hub.close()
        store.close()


def approve(service: McpService, url: str, *names: str) -> str:
    server_id = service.add(McpServerIn(name="Catalog", url=url, allow_loopback=True), actor="admin").id
    asyncio.run(service.adiscover(server_id, actor="admin"))
    for name in names:
        service.decide(
            server_id,
            name,
            approve=True,
            expected_hash=service.tool(server_id, name).hash,
            access="read",
            actor="admin",
        )
    return server_id


def code(call: Any) -> str:
    with pytest.raises(NodestroError) as caught:
        call()
    return caught.value.code


def test_each_server_takes_a_limited_number_of_calls_per_minute(make: Any, server: LoopbackServer) -> None:
    clock = Clock()
    service = make(clock, calls_per_minute=2)
    approve(service, server.url, "search")
    for _ in range(2):
        service.call("mcp__catalog__search", {"query": "q"})
    before = len(server.requests)
    assert code(lambda: service.call("mcp__catalog__search", {"query": "q"})) == "mcp.rate_limited"
    assert len(server.requests) == before  # refused before any connection
    clock.now += 61
    service.call("mcp__catalog__search", {"query": "q"})


def test_a_failing_server_rests_and_a_successful_test_ends_the_rest(make: Any) -> None:
    clock = Clock()
    service = make(clock, breaker_failures=2, breaker_seconds=30)
    handle = serve()
    port = handle.port
    server_id = approve(service, handle.url, "search")
    handle.stop()
    for _ in range(2):
        assert code(lambda: service.call("mcp__catalog__search", {"query": "q"})) == "mcp.unreachable"
    assert code(lambda: service.call("mcp__catalog__search", {"query": "q"})) == "mcp.cooling_down"
    stats = service.hub.stats()["servers"][server_id]
    assert stats["consecutive_failures"] == 2 and stats["cooling_down_seconds"] == 30
    assert stats["last_error_code"] == "mcp.unreachable"
    # After the rest the next call is tried again (and fails again: the server is still down).
    clock.now += 31
    assert code(lambda: service.call("mcp__catalog__search", {"query": "q"})) == "mcp.unreachable"
    # A connection test that succeeds ends the rest at once.
    revived = serve(port=port)
    try:
        assert code(lambda: service.call("mcp__catalog__search", {"query": "q"})) == "mcp.cooling_down"
        asyncio.run(service.adiscover(server_id, actor="admin"))
        assert "results for q" in service.call("mcp__catalog__search", {"query": "q"})[2].text
        assert service.hub.stats()["servers"][server_id]["consecutive_failures"] == 0
    finally:
        revived.stop()


def test_refusals_by_policy_do_not_count_as_failures(make: Any, server: LoopbackServer) -> None:
    service = make(breaker_failures=1)
    server_id = approve(service, server.url, "search", "fails")
    for _ in range(3):
        _srv, _tool, result = service.call("mcp__catalog__fails", {})
        assert result.is_error  # a tool error is the tool's answer, not a sick server
    assert service.hub.stats()["servers"][server_id]["consecutive_failures"] == 0
    service.call("mcp__catalog__search", {"query": "q"})


def test_calls_wait_for_a_free_slot_and_give_up_when_overloaded(make: Any, server: LoopbackServer) -> None:
    service = make(max_parallel=1, queue_seconds=0.3)
    approve(service, server.url, "slow", "search")
    started = threading.Event()
    results: list[Any] = []

    def busy() -> None:
        started.set()
        results.append(service.call("mcp__catalog__slow", {"seconds": 1.5}))

    worker = threading.Thread(target=busy)
    worker.start()
    started.wait()
    time.sleep(0.3)
    assert service.hub.stats()["active_calls"] == 1
    assert code(lambda: service.call("mcp__catalog__search", {"query": "q"})) == "mcp.overloaded"
    worker.join()
    assert results and results[0][2].text == "done"
    assert service.hub.stats()["active_calls"] == 0
    service.call("mcp__catalog__search", {"query": "q"})


def test_the_number_of_servers_is_limited(make: Any) -> None:
    service = make(max_servers=2)
    for index in range(2):
        service.add(McpServerIn(name=f"S{index}", url=f"https://s{index}.example.com/mcp"))
    assert code(lambda: service.add(McpServerIn(name="S3", url="https://s3.example.com/mcp"))) == "mcp.too_many_servers"


def test_closing_the_hub_stops_its_thread_and_refuses_later_calls(make: Any, server: LoopbackServer) -> None:
    service = make()
    approve(service, server.url, "search")
    thread = service.hub._thread
    assert thread is not None and thread.is_alive()
    service.hub.close()
    assert not thread.is_alive()
    assert code(lambda: service.call("mcp__catalog__search", {"query": "q"})) == "mcp.closed"


def test_tools_that_change_data_are_usable_while_the_shared_gate_is_on(make: Any, server: LoopbackServer) -> None:
    service = make()
    server_id = approve(service, server.url)
    service.decide(
        server_id,
        "delete_page",
        approve=True,
        expected_hash=service.tool(server_id, "delete_page").hash,
        access="write",
        actor="admin",
    )
    assert code(lambda: service.check("mcp__catalog__delete_page")) == "mcp.write_not_supported"
    service.write_gate = True
    # The switch alone is not enough: only a caller that asks before every call (gated) may run it (review H-1).
    assert code(lambda: service.call("mcp__catalog__delete_page", {"page_id": "7"})) == "mcp.write_not_supported"
    assert service.call("mcp__catalog__delete_page", {"page_id": "7"}, gated=True)[2].text == "deleted 7"
