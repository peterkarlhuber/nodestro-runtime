"""MCP tools in release bundles (WP-9): frozen safe references, no addresses or tokens, revalidation per call."""

from __future__ import annotations

import base64
from collections.abc import Iterator
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp_support import LoopbackServer, build_server, serve

from nodestro_runtime.mcp.hub import McpHub
from nodestro_runtime.mcp.models import McpServerIn
from nodestro_runtime.mcp.netguard import EgressPolicy
from nodestro_runtime.mcp.pinned import PinnedMcp
from nodestro_runtime.mcp.service import McpService
from nodestro_runtime.mcp.vault import TokenVault, vault_box_from_env
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.release import ReleaseBundle
from nodestro_runtime.schemas import Workflow
from nodestro_runtime.storage import SQLiteStore
from nodestro_runtime.tool_library import ToolLibrary

TOKEN = "release-s3cret-token"
NAME = "mcp__docs__search"


@pytest.fixture
def server() -> Iterator[LoopbackServer]:
    handle = serve(token=TOKEN)
    yield handle
    handle.stop()


@pytest.fixture
def service(tmp_path: Path) -> Iterator[McpService]:
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.migrate(before_migration=None)
    box = vault_box_from_env({"NODESTRO_VAULT_KEY": base64.b64encode(os.urandom(32)).decode()})
    hub = McpHub(EgressPolicy())
    yield McpService(store, hub, TokenVault(box, "local"))
    hub.close()
    store.close()


def connect(service: McpService, server: LoopbackServer) -> str:
    data = McpServerIn(name="Docs", url=server.url, allow_loopback=True, auth="bearer", token=TOKEN)
    server_id = service.add(data, actor="admin").id
    service.discover(server_id, actor="admin")
    service.decide(server_id, "search", approve=True, access="read", actor="admin")
    return server_id


def code(call: Any) -> str:
    with pytest.raises(NodestroError) as caught:
        call()
    return caught.value.code


def bundle_with(refs: dict[str, Any]) -> ReleaseBundle:
    return ReleaseBundle(workflow_id="wf", workflow=Workflow(name="W"), mcp_tools=refs)


def test_a_pinned_tool_names_the_server_by_id_only(service: McpService, server: LoopbackServer) -> None:
    server_id = connect(service, server)
    ref = service.pin(NAME)
    assert ref.server_id == server_id and ref.server_slug == "docs" and ref.name == "search"
    assert ref.hash == service.tool(server_id, "search").approved_hash and ref.access == "read"
    dumped = bundle_with({NAME: ref}).model_dump_json()
    assert TOKEN not in dumped
    assert server.url not in dumped and "127.0.0.1" not in dumped
    # Only usable tools can be frozen.
    assert code(lambda: service.pin("mcp__docs__delete_page")) == "mcp.tool_not_approved"


def test_bundles_without_mcp_tools_keep_their_digest() -> None:
    import hashlib
    import json

    bundle = bundle_with({})
    data = bundle.model_dump(mode="json", exclude={"created_at", "tool_trust", "mcp_tools"})
    expected = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    assert bundle.digest() == expected


def test_a_release_uses_only_its_frozen_tools_at_their_definition(
    service: McpService, server: LoopbackServer, tmp_path: Path
) -> None:
    server_id = connect(service, server)
    pinned = PinnedMcp(service, {NAME: service.pin(NAME)})
    library = ToolLibrary(tmp_path / "tools", extra_tools=[])
    library.mcp = pinned  # type: ignore[assignment]
    assert NAME in library
    # A tool approved after publishing is not part of the release.
    service.decide(server_id, "big", approve=True, access="read", actor="admin")
    assert "mcp__docs__big" not in library
    assert code(lambda: pinned.check("mcp__docs__big")) == "mcp.tool_not_in_release"
    tool = library.tool(NAME)
    assert "results for q" in str(tool.execute({"query": "q"}))
    assert pinned.provenance(NAME)["release_hash"] == pinned.refs[NAME].hash
    assert [item.name for item in pinned.usable()] == ["search"]
    # A changed classification (or re-approval at another hash) stops the release until it is published again.
    service.decide(server_id, "search", approve=True, access="destructive", actor="admin")
    assert code(lambda: pinned.check(NAME)) in ("mcp.release_tool_changed", "mcp.write_not_supported")
    service.decide(server_id, "search", approve=True, access="read", actor="admin")
    pinned.check(NAME)
    tool_record = service.tool(server_id, "search")
    tool_record.approved_hash = "0" * 64
    tool_record.hash = "0" * 64
    service.store.put("mcp_tools", tool_record.key, tool_record)
    assert code(lambda: pinned.check(NAME)) == "mcp.release_tool_changed"
    with pytest.raises(NodestroError):
        pinned.call(NAME, {"query": "q"})


def test_a_removed_server_stops_the_release(service: McpService, server: LoopbackServer) -> None:
    server_id = connect(service, server)
    pinned = PinnedMcp(service, {NAME: service.pin(NAME)})
    service.remove(server_id)
    assert code(lambda: pinned.check(NAME)) == "mcp.tool_not_found"
    # Another server under the same slug is not the frozen one.
    other = serve(build_server("other"), token=TOKEN)
    try:
        connect(service, other)
        assert code(lambda: pinned.check(NAME)) == "mcp.release_tool_changed"
    finally:
        other.stop()


def test_the_tool_marker_names_the_frozen_hash(service: McpService, server: LoopbackServer) -> None:
    connect(service, server)
    ref = service.pin(NAME)
    assert bundle_with({NAME: ref}).tool_marker(NAME) == f"{NAME}:mcp:{ref.hash[:12]}"
