"""Versioned exchange formats and the shared import check (N-14): round trips, secrets, versions, tampered files."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import io
import json
import os
from pathlib import Path
import stat
from typing import Any
import zipfile

import pytest

from nodestro_runtime import exchange
from nodestro_runtime.connections import ConnectionIn, ConnectionService
from nodestro_runtime.knowledge import KnowledgeBase
from nodestro_runtime.mcp import exchange as mcp_exchange
from nodestro_runtime.mcp.hub import McpHub
from nodestro_runtime.mcp.models import McpServerIn, StdioCommand
from nodestro_runtime.mcp.netguard import EgressPolicy
from nodestro_runtime.mcp.service import McpService
from nodestro_runtime.mcp.vault import TokenVault
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.server_settings import SecretBox
from nodestro_runtime.storage import SQLiteStore

CANARY = "CANARY-SECRET-n14-3f9a7c1e5b2d"


def code(call: Callable[[], Any]) -> str:
    with pytest.raises(NodestroError) as caught:
        call()
    return caught.value.code


def raw(document: Any) -> bytes:
    return json.dumps(document).encode()


def without_time(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "created_at"}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    opened = SQLiteStore(tmp_path / "store.db")
    opened.migrate(before_migration=None)
    yield opened
    opened.close()


def mcp_service(store: SQLiteStore) -> McpService:
    return McpService(store, McpHub(EgressPolicy()), TokenVault(SecretBox(os.urandom(32)), "local"))


def connection_service(store: SQLiteStore) -> ConnectionService:
    return ConnectionService(store, TokenVault(SecretBox(os.urandom(32)), "local", namespace="connection"))


# ---- format and version ---------------------------------------------------------------------------------------------


def test_the_format_names_its_version_and_newer_versions_are_refused() -> None:
    assert exchange.format_version("nodestro-mcp/1", "nodestro-mcp") == 1
    assert code(lambda: exchange.format_version("nodestro-mcp/2", "nodestro-mcp")) == "exchange.version_newer"
    assert code(lambda: exchange.format_version("nodestro-connection/1", "nodestro-mcp")) == "exchange.format_mismatch"
    for value in (None, 1, "", "nodestro-mcp", "nodestro-mcp/0", "nodestro-mcp/1.1", "other/1", "nodestro-mcp/x"):
        assert code(lambda value=value: exchange.format_version(value, "nodestro-mcp")) == "exchange.format_invalid"
    with pytest.raises(NodestroError) as newer:
        exchange.read_json(raw({"format": "nodestro-collection/7"}), "nodestro-collection")
    assert newer.value.params == {"version": 7, "supported": 1}


def test_json_files_are_checked_for_size_syntax_and_shape_first() -> None:
    big = raw({"format": "nodestro-mcp/1", "servers": [], "pad": "x" * exchange.MAX_JSON_BYTES})
    assert code(lambda: exchange.read_json(big, "nodestro-mcp")) == "exchange.too_large"
    assert code(lambda: exchange.read_json(b"{not json", "nodestro-mcp")) == "exchange.invalid_json"
    # Deep nesting: refused as JSON where the parser has a recursion limit, else by the content check (Python 3.14).
    deep = b'{"format": "nodestro-mcp/1", "servers": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    assert code(lambda: exchange.parse(exchange.Envelope, exchange.read_json(deep, "nodestro-mcp"))) in (
        "exchange.invalid_json",
        "exchange.content_invalid",
    )
    assert code(lambda: exchange.read_json(b"[]", "nodestro-mcp")) == "exchange.format_invalid"
    with pytest.raises(NodestroError) as invalid:
        exchange.parse(
            mcp_exchange.McpPackage, {"format": "nodestro-mcp/1", "servers": [{"name": "x", "token": CANARY}]}
        )
    assert invalid.value.code == "exchange.content_invalid"
    assert CANARY not in json.dumps(invalid.value.params)  # the places, never the values


# ---- the secret check -----------------------------------------------------------------------------------------------


def test_the_secret_check_finds_names_addresses_shapes_and_known_secrets() -> None:
    assert exchange.secret_findings({"servers": [{"name": "Docs", "url": "https://docs.example.com/mcp"}]}) == []
    assert exchange.secret_findings({"a": {"api_key": "x"}}) == ["a.api_key"]
    assert exchange.secret_findings({"a": {"token": None, "password": ""}}) == []
    assert exchange.secret_findings({"url": "https://user:pw@example.com/"}) == ["url"]
    assert exchange.secret_findings({"url": "https://example.com/?access_token=abc"}) == ["url"]
    assert exchange.secret_findings({"items": ["sk-ant-api03-abcdefghijklmnopqrstuvwxyz"]}) == ["items[0]"]
    assert exchange.secret_findings({"description": f"see {CANARY} here"}, [CANARY]) == ["(known secret)"]
    with pytest.raises(NodestroError) as refused:
        exchange.ensure_no_secrets({"description": CANARY}, [CANARY])
    assert refused.value.code == "exchange.secret_detected" and CANARY not in json.dumps(refused.value.params)


# ---- nodestro-mcp/1 --------------------------------------------------------------------------------------------------


def test_mcp_services_round_trip_without_their_tokens(store: SQLiteStore, tmp_path: Path) -> None:
    source = mcp_service(store)
    source.add(McpServerIn(name="Docs", url="http://127.0.0.1:9/mcp", allow_loopback=True, auth="bearer", token=CANARY))
    source.add(McpServerIn(name="Search", url="https://search.example.com/mcp", timeout_seconds=30))
    exported = mcp_exchange.export_mcp(source)
    assert exported["format"] == "nodestro-mcp/1" and CANARY not in json.dumps(exported)
    assert {server["name"] for server in exported["servers"]} == {"Docs", "Search"}

    target_store = SQLiteStore(tmp_path / "target.db")
    target_store.migrate(before_migration=None)
    try:
        target = mcp_service(target_store)
        preview = mcp_exchange.import_mcp(target, raw(exported), dry_run=True)
        assert [item.status for item in preview.items] == ["ready", "ready"] and target.servers() == []
        assert [item.needs_secret for item in preview.items] == [True, False]
        report = mcp_exchange.import_mcp(target, raw(exported), dry_run=False, actor="admin")
        assert [item.status for item in report.items] == ["created", "created"]
        docs = target.server(report.items[0].id or "")
        # Imported: switched off, no token, nothing approved; the token is listed to be entered again.
        assert docs.enabled is False and target.token(docs) is None and docs.token_storage == "none"
        assert target.tools(docs.id) == []
        assert report.secrets_to_reenter == [f"mcp:{docs.id}"]
        assert without_time(mcp_exchange.export_mcp(target)) == without_time(exported)
    finally:
        target_store.close()


def test_an_mcp_import_keeps_the_rules_of_adding_a_server(store: SQLiteStore) -> None:
    service = mcp_service(store)
    stdio = {"requirements": ["mcp-demo==1.0"], "module": "mcp_demo"}
    package = {"format": "nodestro-mcp/1", "servers": [{"name": "Local", "transport": "stdio", "stdio": stdio}]}
    # A stdio server runs only in a container: without the Docker sandbox it is refused, nothing is stored.
    preview = mcp_exchange.import_mcp(service, raw(package), dry_run=True)
    assert preview.items[0].status == "refused" and preview.items[0].code == "sandbox.isolation_required"
    assert code(lambda: mcp_exchange.import_mcp(service, raw(package), dry_run=False)) == "exchange.items_refused"
    assert service.servers() == []
    unsafe = [
        {"name": "Key", "url": "https://docs.example.com/mcp?api_key=abcdef"},
        {"name": "Loop", "url": "http://127.0.0.1:9/mcp"},
        {"name": "Meta", "url": "https://169.254.169.254/mcp"},
    ]
    report = mcp_exchange.import_mcp(service, raw({"format": "nodestro-mcp/1", "servers": unsafe}), dry_run=True)
    assert [item.code for item in report.items] == ["mcp.url_secret", "mcp.https_required", "mcp.address_refused"]
    tampered = {"format": "nodestro-mcp/1", "servers": [{"name": "x", "url": "https://a.example.com", "enabled": True}]}
    assert code(lambda: mcp_exchange.import_mcp(service, raw(tampered), dry_run=True)) == "exchange.content_invalid"
    sneaky = {**stdio, "env": {"TOKEN": "x"}}
    stdio_extra = {"format": "nodestro-mcp/1", "servers": [{"name": "x", "transport": "stdio", "stdio": sneaky}]}
    assert code(lambda: mcp_exchange.import_mcp(service, raw(stdio_extra), dry_run=True)) == "exchange.content_invalid"
    newer = {"format": "nodestro-mcp/2", "servers": []}
    assert code(lambda: mcp_exchange.import_mcp(service, raw(newer), dry_run=True)) == "exchange.version_newer"
    many = {
        "format": "nodestro-mcp/1",
        "servers": [{"name": f"s{n}", "url": f"https://s{n}.example.com"} for n in range(26)],
    }
    assert mcp_exchange.import_mcp(service, raw(many), dry_run=True).items[-1].code == "mcp.too_many_servers"


def test_an_mcp_export_with_a_secret_in_its_address_is_refused(store: SQLiteStore) -> None:
    service = mcp_service(store)
    server = service.add(McpServerIn(name="Docs", url="https://docs.example.com/mcp", auth="bearer", token=CANARY))
    # An address written before the address rules (or by hand into the store) that carries the token itself.
    store.put("mcp_servers", server.id, server.model_copy(update={"url": f"https://docs.example.com/mcp/{CANARY}"}))
    with pytest.raises(NodestroError) as refused:
        mcp_exchange.export_mcp(service)
    assert refused.value.code == "exchange.secret_detected" and CANARY not in json.dumps(refused.value.params)
    stdio = StdioCommand(requirements=["mcp-demo==1.0"], module="mcp_demo")
    assert stdio.address() == "stdio:mcp_demo"


# ---- nodestro-connection/1 -------------------------------------------------------------------------------------------


def test_connections_round_trip_without_their_secrets(store: SQLiteStore, tmp_path: Path) -> None:
    source = connection_service(store)
    mail = source.create(
        ConnectionIn(
            type="smtp",
            name="Mail",
            description="Outgoing",
            settings={"host": "smtp.example.com", "port": 587, "from_address": "bot@example.com", "username": "bot"},
        )
    )
    source.set_secret(mail.id, CANARY)
    source.create(ConnectionIn(type="webhook", name="Hook", settings={"url": "https://hooks.example.com/in"}))
    exported = exchange.export_connections(source)
    assert exported["format"] == "nodestro-connection/1" and CANARY not in json.dumps(exported)
    assert all("egress" not in item and "secret_sealed" not in item for item in exported["connections"])

    target_store = SQLiteStore(tmp_path / "target.db")
    try:
        target = connection_service(target_store)
        report = exchange.import_connections(target, raw(exported), dry_run=False, actor="admin")
        imported = {item.name: target.get(item.id or "") for item in report.items}
        assert imported["Mail"].egress == ["smtp.example.com:587"] and not target.secret_set(imported["Mail"])
        assert report.secrets_to_reenter == [f"connection:{imported['Mail'].id}"]  # a webhook needs no secret
        assert without_time(exchange.export_connections(target)) == without_time(exported)
        # Importing the same file again: the names are taken, so the preview refuses every entry.
        again = exchange.import_connections(target, raw(exported), dry_run=True)
        assert {item.code for item in again.items} == {"connection.name_taken"}
    finally:
        target_store.close()


def test_a_connection_import_keeps_the_rules_of_creating_one(store: SQLiteStore) -> None:
    service = connection_service(store)
    entries = [
        {"type": "webhook", "name": "Hook", "settings": {"url": "https://user:pw@hooks.example.com/x"}},
        {"type": "smtp", "name": "Mail", "settings": {"host": "smtp.example.com", "from_address": "a@b.c", "x": 1}},
        {"type": "database", "name": "DB"},
        {"type": "search", "name": "Twice", "provider": "test"},
        {"type": "search", "name": "twice", "provider": "test"},
    ]
    report = exchange.import_connections(
        service, raw({"format": "nodestro-connection/1", "connections": entries}), dry_run=True
    )
    codes = [item.code for item in report.items]
    assert codes[0] == "connection.address_invalid" and codes[1] == "connection.settings_invalid"
    assert (
        codes[2] is not None and codes[2].startswith("connection.type") and codes[3:] == [None, "connection.name_taken"]
    )
    assert CANARY not in report.model_dump_json()
    with_secret = {"format": "nodestro-connection/1", "connections": [{"type": "search", "name": "S", "secret": "x"}]}
    assert (
        code(lambda: exchange.import_connections(service, raw(with_secret), dry_run=True)) == "exchange.content_invalid"
    )
    assert service.connections() == []


# ---- nodestro-collection/1 -------------------------------------------------------------------------------------------


@pytest.fixture
def knowledge(tmp_path: Path) -> Iterator[KnowledgeBase]:
    opened = KnowledgeBase(tmp_path / "knowledge.db")
    yield opened
    opened.close()


def exported_collection(knowledge: KnowledgeBase) -> bytes:
    knowledge.add(b"Employees have 30 days of vacation.", name="hr.md", description="HR", collection="Policies")
    knowledge.add(b"# Travel\n\nBook trains first.", name="travel.docx", collection="Policies", converted_from="docx")
    knowledge.add(b"Not in the collection.", name="other.txt")
    target = io.BytesIO()
    manifest = exchange.export_collection(knowledge, "Policies", target)
    assert manifest.format == "nodestro-collection/1" and len(manifest.documents) == 2
    return target.getvalue()


def manifest_of(data: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return json.loads(archive.read("manifest.json"))  # type: ignore[no-any-return]


def test_a_collection_round_trips_and_is_indexed_again(knowledge: KnowledgeBase, tmp_path: Path) -> None:
    data = exported_collection(knowledge)
    target = KnowledgeBase(tmp_path / "target.db")
    try:
        preview = exchange.import_collection(target, io.BytesIO(data), dry_run=True)
        assert preview.collection == "Policies" and not preview.collection_exists and target.files() == []
        assert preview.embedding is not None and preview.embedding.matches is False
        report = exchange.import_collection(target, io.BytesIO(data), dry_run=False)
        assert [item.status for item in report.items] == ["created", "created"]
        assert {item.name for item in target.files()} == {"hr.md", "travel.docx"}
        travel = next(item for item in target.files() if item.name == "travel.docx")
        assert travel.source_format == "docx" and travel.searchable and travel.collection == "Policies"
        assert target.search("vacation", k=1)[0].file_id is not None
        again = io.BytesIO()
        exchange.export_collection(target, "Policies", again)
        first, second = manifest_of(data), manifest_of(again.getvalue())
        assert first["documents"] == second["documents"] and first["name"] == second["name"]
        # The collection has files now: another import into it is refused; under another name it is fine.
        assert code(lambda: exchange.import_collection(target, io.BytesIO(data), dry_run=False)) == (
            "exchange.collection_exists"
        )
        # Under another name: the documents are here already, so they are named and left as they are.
        copy = exchange.import_collection(target, io.BytesIO(data), dry_run=False, collection="Copy")
        assert copy.collection == "Copy" and [item.status for item in copy.items] == ["exists", "exists"]
        assert {item.collection for item in target.files()} == {"Policies"} and len(target.files()) == 2
    finally:
        target.close()


def rebuilt(data: bytes, change: Callable[[dict[str, bytes], dict[str, Any]], None]) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files.pop("manifest.json"))
    change(files, manifest)
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, content in files.items():
            archive.writestr(name, content)
    return target.getvalue()


def test_tampered_and_oversized_collections_are_refused(
    knowledge: KnowledgeBase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = exported_collection(knowledge)
    target = KnowledgeBase(tmp_path / "target.db")

    def refused(package: bytes) -> str:
        return code(lambda: exchange.import_collection(target, io.BytesIO(package), dry_run=True))

    def changed_document(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
        files["documents/0001"] = b"Employees have 99 days of vacation."

    def extra_file(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
        files["documents/0003"] = b"unlisted"

    def traversal(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
        files["../evil.py"] = b"print('x')"

    def unexpected(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
        files["run.sh"] = b"#!/bin/sh"

    def newer(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
        manifest["format"] = "nodestro-collection/2"

    def unknown_field(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
        manifest["vectors"] = [[0.1, 0.2]]

    def missing(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
        files.pop("documents/0002")

    def bomb(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
        files["documents/0001"] = b"\0" * (3 * exchange.MIB)
        manifest["documents"][0]["size"] = 3 * exchange.MIB

    try:
        # Same size, other bytes: the preview names the document; a changed size is refused at once.
        preview = exchange.import_collection(target, io.BytesIO(rebuilt(data, changed_document)), dry_run=True)
        assert [item.code for item in preview.items] == ["exchange.checksum", None]

        def longer(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
            files["documents/0001"] += b" And more."

        assert refused(rebuilt(data, longer)) == "exchange.checksum"
        assert refused(rebuilt(data, extra_file)) == "exchange.incomplete"
        assert refused(rebuilt(data, missing)) == "exchange.incomplete"
        assert refused(rebuilt(data, traversal)) == "exchange.unsafe_path"
        assert refused(rebuilt(data, unexpected)) == "exchange.unexpected_file"
        assert refused(rebuilt(data, newer)) == "exchange.version_newer"
        assert refused(rebuilt(data, unknown_field)) == "exchange.content_invalid"
        assert refused(rebuilt(data, bomb)) == "exchange.zip_bomb"
        assert refused(b"not a zip file") == "exchange.zip_invalid"
        link = io.BytesIO()
        with zipfile.ZipFile(link, "w") as archive:
            info = zipfile.ZipInfo("documents/0001")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
        assert refused(link.getvalue()) == "exchange.unsafe_path"
        crowded = io.BytesIO()
        with zipfile.ZipFile(crowded, "w") as archive:
            for number in range(exchange.MAX_DOCUMENTS + 2):
                archive.writestr(f"documents/{number:04d}", b"")
        assert refused(crowded.getvalue()) == "exchange.too_many_entries"
        monkeypatch.setattr(exchange, "MAX_COLLECTION_BYTES", len(data) - 1)
        assert refused(data) == "exchange.too_large"
        assert target.files() == []
    finally:
        target.close()


def test_a_collection_with_a_damaged_document_stores_nothing(knowledge: KnowledgeBase, tmp_path: Path) -> None:
    data = exported_collection(knowledge)

    def damaged(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
        files["documents/0002"] = files["documents/0002"][:-1] + b"!"

    target = KnowledgeBase(tmp_path / "target.db")
    try:
        preview = exchange.import_collection(target, io.BytesIO(rebuilt(data, damaged)), dry_run=True)
        assert [item.code for item in preview.items] == [None, "exchange.checksum"]
        package = io.BytesIO(rebuilt(data, damaged))
        assert code(lambda: exchange.import_collection(target, package, dry_run=False)) == "exchange.items_refused"
        assert target.files() == []
        assert code(lambda: exchange.export_collection(target, "Nothing", io.BytesIO())) == (
            "exchange.collection_not_found"
        )
    finally:
        target.close()
