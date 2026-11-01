"""Fixes from the independent MCP security review: secrets in addresses (M-1), loopback consent (L-2), reused
server slugs (L-6), the result marker (L-5) and approvals without the reviewed hash (L-3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp_support import LoopbackServer, serve

from nodestro_runtime.mcp.hub import McpHub
from nodestro_runtime.mcp.models import EVENTS, SERVERS, McpEvent, McpServer, McpServerIn
from nodestro_runtime.mcp.netguard import EgressPolicy, redact_url, url_secret_problem
from nodestro_runtime.mcp.service import McpService
from nodestro_runtime.mcp.tools import McpAgentTool
from nodestro_runtime.mcp.vault import TokenVault
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.storage import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    database = SQLiteStore(tmp_path / "db.sqlite")
    database.migrate(before_migration=None)
    yield database
    database.close()


@pytest.fixture
def service(store: SQLiteStore) -> Iterator[McpService]:
    hub = McpHub(EgressPolicy())
    yield McpService(store, hub, TokenVault(None, "local"))
    hub.close()


@pytest.fixture
def server() -> Iterator[LoopbackServer]:
    handle = serve()
    yield handle
    handle.stop()


def code(call: object) -> str:
    with pytest.raises(NodestroError) as caught:
        call()  # type: ignore[operator]
    return caught.value.code


@pytest.mark.parametrize(
    ("url", "where"),
    [
        ("https://mcp.example.com/mcp?api_key=abc", "query"),
        ("https://mcp.example.com/mcp?token=x", "query"),
        ("https://mcp.example.com/mcp?access_token=x", "query"),
        ("https://mcp.example.com/mcp?x=sk_live_51HxQ2eKz9V3pR7tY", "query"),
        ("https://mcp.example.com/u/3f9a1c7e5b2d4a6f8e0c1b3d5f7a9c2e/mcp", "path"),
        ("https://mcp.example.com/mcp#secret", "fragment"),
    ],
)
def test_addresses_with_keys_are_refused(service: McpService, url: str, where: str) -> None:
    assert url_secret_problem(url) == where
    with pytest.raises(NodestroError) as caught:
        service.add(McpServerIn(name="Docs", url=url), actor="admin")
    assert caught.value.code == "mcp.url_secret" and caught.value.params["where"] == where
    assert "abc" not in str(caught.value) and "sk_live" not in str(caught.value)
    assert service.servers() == []


@pytest.mark.parametrize(
    "url", ["https://mcp.example.com/mcp", "https://mcp.example.com/v1/mcp?version=2", "https://docs.example.com/sse"]
)
def test_ordinary_addresses_pass(url: str) -> None:
    assert url_secret_problem(url) is None


def test_stored_addresses_with_keys_are_cleaned_and_switched_off(store: SQLiteStore) -> None:
    legacy = McpServer(slug="docs", name="Docs", url="https://mcp.example.com/mcp?api_key=SECRET123value456")
    store.put(SERVERS, legacy.id, legacy)
    old = McpEvent(kind="server_added", server_id=legacy.id, details={"url": legacy.url})
    store.put(EVENTS, f"{old.at.isoformat()}_{old.id}", old)
    hub = McpHub(EgressPolicy())
    try:
        service = McpService(store, hub, TokenVault(None, "local"))
        cleaned = service.server(legacy.id)
        assert cleaned.url == "https://mcp.example.com/mcp"
        assert cleaned.enabled is False and cleaned.last_error_code == "mcp.url_secret_removed"
        dumped = [event.model_dump_json() for event in service.events()]
        assert dumped and not any("SECRET123" in item for item in dumped)
        raw = Path(store.path).read_bytes() if hasattr(store, "path") else b""
        assert b"SECRET123value456" not in raw or not raw
    finally:
        hub.close()


def test_events_never_hold_a_query(service: McpService, server: LoopbackServer) -> None:
    assert redact_url("https://h.example/p/a?b=c#d") == "https://h.example/p/a?…"
    added = service.add(McpServerIn(name="Docs", url=server.url, allow_loopback=True), actor="admin")
    assert all("?" not in str(event.details.get("url", "")) for event in service.events())
    assert added.url == server.url


def test_loopback_consent_only_for_local_addresses(service: McpService) -> None:
    assert (
        code(lambda: service.add(McpServerIn(name="Docs", url="https://mcp.example.com/mcp", allow_loopback=True)))
        == "mcp.loopback_not_local"
    )


def test_a_removed_servers_slug_is_never_reused(service: McpService, server: LoopbackServer) -> None:
    first = service.add(McpServerIn(name="Catalog", url=server.url, allow_loopback=True), actor="admin")
    assert first.slug == "catalog"
    service.remove(first.id, actor="admin")
    second = service.add(McpServerIn(name="Catalog", url=server.url, allow_loopback=True), actor="admin")
    assert second.slug != "catalog"
    service.discover(second.id)
    assert service.find("mcp__catalog__search") is None


def test_an_approval_must_name_the_reviewed_hash(service: McpService, server: LoopbackServer) -> None:
    added = service.add(McpServerIn(name="Catalog", url=server.url, allow_loopback=True), actor="admin")
    service.discover(added.id)
    assert code(lambda: service.decide(added.id, "search", approve=True, access="read")) == "mcp.hash_required"
    service.decide(added.id, "search", approve=False)  # a rejection needs no hash
    tool = service.tool(added.id, "search")
    service.decide(added.id, "search", approve=True, access="read", expected_hash=tool.hash)
    assert service.tool(added.id, "search").status == "approved"


def test_a_result_cannot_fake_the_end_of_its_marker(service: McpService, server: LoopbackServer) -> None:
    added = service.add(McpServerIn(name="Catalog", url=server.url, allow_loopback=True), actor="admin")
    service.discover(added.id)
    tool = service.tool(added.id, "injection")
    service.decide(added.id, "injection", approve=True, access="read", expected_hash=tool.hash)
    srv, found = service.check("mcp__catalog__injection")
    agent_tool = McpAgentTool(service, srv, found)
    first, second = agent_tool.execute({}), agent_tool.execute({})
    assert first.splitlines()[-1].startswith("[End of external data ")
    assert first.splitlines()[-1] != second.splitlines()[-1]  # a new random marker for every call
