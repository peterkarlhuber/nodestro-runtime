"""The key of a local data directory (Nodestro Local Docker, LD-020): key check, rotation and key loss.

A workspace opened with ``verify_vault_key`` never lays a new key over secrets sealed with another one, and the
maintenance functions seal provider keys, MCP tokens and connection secrets again (rotation) or drop the ones a lost
key sealed, in one transaction each.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sqlite3

import pytest

from nodestro_runtime import local_vault
from nodestro_runtime.mcp.vault import VAULT_KEY_ENV, TokenVault, vault_box_from_env
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import ProviderSettingsIn
from nodestro_runtime.schemas import SettingsIn
from nodestro_runtime.server_settings import is_encrypted
from nodestro_runtime.workspace import DATABASE, Workspace

SECRET = "sk-ant-canary-0123456789abcdefghij"
TOKEN = "mcp-token-canary-abcdefghijklmnop"
PENDING = "oauth-pending-canary-qrstuvwxyz"
CONNECTION = "connection-secret-canary-123456"


def new_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture(autouse=True)
def no_operator_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VAULT_KEY_ENV, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def vaults(key: str) -> tuple[TokenVault, TokenVault]:
    """The MCP and the connection vault, as the workspace builds them (same fields, same namespaces)."""
    box = vault_box_from_env({VAULT_KEY_ENV: key})
    return TokenVault(box, "local"), TokenVault(box, "local", namespace="connection")


def open_workspace(data: Path, key: str | None) -> Workspace:
    return Workspace(data, integrations=[], vault_key=key, verify_vault_key=True)


def with_secrets(data: Path, key: str) -> None:
    """A data directory with a provider key, an MCP token, a pending MCP sign-in and a connection secret."""
    workspace = open_workspace(data, key)
    try:
        workspace.update_settings(SettingsIn(providers={"anthropic": ProviderSettingsIn(api_key=SECRET)}))
        mcp, connection = vaults(key)
        workspace.store.put_raw(
            "mcp_servers",
            "srv-1",
            {
                "id": "srv-1",
                "token_sealed": mcp.seal("srv-1", TOKEN),
                "oauth_pending_sealed": mcp.seal("srv-1:oauth-pending", PENDING),
            },
        )
        workspace.store.put_raw(
            "service_connections",
            "con-1",
            {"id": "con-1", "secret_sealed": connection.seal("con-1", CONNECTION), "secret_storage": "vault"},
        )
    finally:
        workspace.close()


def raw(data: Path) -> bytes:
    return b"".join(path.read_bytes() for path in data.glob(DATABASE + "*"))


def documents(data: Path) -> dict[tuple[str, str], str]:
    connection = sqlite3.connect(data / DATABASE)
    try:
        rows = connection.execute("SELECT collection, id, data FROM nodestro_documents").fetchall()
    finally:
        connection.close()
    return {(collection, key): text for collection, key, text in rows}


def readable(data: Path, key: str) -> dict[str, str | None]:
    """Every secret of ``with_secrets``, opened with ``key`` (``None``: it does not open)."""
    found = documents(data)

    settings = json.loads(found[("settings", "app")])
    server = json.loads(found[("mcp_servers", "srv-1")])
    item = json.loads(found[("service_connections", "con-1")])
    mcp, connection = vaults(key)
    box = vault_box_from_env({VAULT_KEY_ENV: key})
    assert box is not None
    sealed = settings["providers"]["anthropic"].get("api_key")
    try:
        provider = box.decrypt("local", "providers.anthropic.api_key", sealed) if sealed else None
    except NodestroError:
        provider = None
    return {
        "provider": provider,
        "token": mcp.open("srv-1", server["token_sealed"]) if server.get("token_sealed") else None,
        "pending": (
            mcp.open("srv-1:oauth-pending", server["oauth_pending_sealed"])
            if server.get("oauth_pending_sealed")
            else None
        ),
        "connection": connection.open("con-1", item["secret_sealed"]) if item.get("secret_sealed") else None,
    }


ALL = {"provider": SECRET, "token": TOKEN, "pending": PENDING, "connection": CONNECTION}


# ---- the key check ------------------------------------------------------------------------------------------------


def test_without_a_key_the_workspace_does_not_open(tmp_path: Path) -> None:
    with pytest.raises(NodestroError) as refused:
        open_workspace(tmp_path / "data", None)
    assert refused.value.code == "secrets.vault_key_missing"
    # The directory is not left locked: it opens with a key.
    open_workspace(tmp_path / "data", new_key()).close()


def test_the_first_key_is_kept_and_another_one_is_refused_before_anything_changes(tmp_path: Path) -> None:
    data, key = tmp_path / "data", new_key()
    open_workspace(data, key).close()  # an installation without any secret yet
    check = documents(data)[("local_vault", "check")]
    assert "nodestro:enc:v1:" in check

    with pytest.raises(NodestroError) as refused:
        open_workspace(data, new_key())
    assert refused.value.code == "secrets.vault_key_mismatch"
    assert documents(data)[("local_vault", "check")] == check  # not replaced by the other key's

    again = open_workspace(data, key)  # the right key still opens it
    again.close()


def test_a_wrong_key_does_not_migrate_plain_keys(tmp_path: Path) -> None:
    data, key = tmp_path / "data", new_key()
    with_secrets(data, key)
    plain = Workspace(data, integrations=[])  # a plain key written without a key store (an earlier release)
    try:
        values = plain.store.get_raw("settings", "app")
        values["providers"]["openai"] = {"api_key": "sk-openai-plain-canary"}
        plain.store.put_raw("settings", "app", values)
    finally:
        plain.close()
    before = documents(data)
    with pytest.raises(NodestroError, match="does not fit"):
        open_workspace(data, new_key())
    assert documents(data) == before


def test_data_from_before_the_check_value_is_checked_by_its_secrets(tmp_path: Path) -> None:
    data, key = tmp_path / "data", new_key()
    with_secrets(data, key)
    connection = sqlite3.connect(data / DATABASE)
    with connection:
        connection.execute("DELETE FROM nodestro_documents WHERE collection = 'local_vault'")
    connection.close()
    with pytest.raises(NodestroError) as refused:
        open_workspace(data, new_key())
    assert refused.value.code == "secrets.vault_key_mismatch" and refused.value.params["sealed"] == 4
    open_workspace(data, key).close()  # the right key opens it and stores the check value again
    assert ("local_vault", "check") in documents(data)


def test_the_first_start_with_a_key_seals_plain_provider_keys(tmp_path: Path) -> None:
    data = tmp_path / "data"
    old = Workspace(data, integrations=[])  # Nodestro Local Docker before LD-020: no key store
    try:
        old.update_settings(SettingsIn(providers={"anthropic": ProviderSettingsIn(api_key=SECRET)}))
    finally:
        old.close()
    key = new_key()
    for _ in range(2):  # idempotent
        workspace = open_workspace(data, key)
        try:
            stored = workspace.store.get_raw("settings", "app")["providers"]["anthropic"]["api_key"]
            assert is_encrypted(stored)
            assert workspace.provider_settings("anthropic")["api_key"] == SECRET
        finally:
            workspace.close()
    connection = sqlite3.connect(data / DATABASE)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    assert SECRET.encode() not in raw(data)


# ---- rotation -----------------------------------------------------------------------------------------------------


def test_rotation_seals_every_kind_of_secret_with_the_new_key(tmp_path: Path) -> None:
    data, old, new = tmp_path / "data", new_key(), new_key()
    with_secrets(data, old)
    old_values = [text for text in documents(data).values() if "nodestro:enc:v1:" in text]

    assert local_vault.rekey(data, old, new) == {"provider": 1, "mcp": 2, "connection": 1}
    assert readable(data, new) == ALL
    assert set(readable(data, old).values()) == {None}
    # The old ciphertexts are gone from the file, its write-ahead log and its free pages.
    content = raw(data)
    for text in old_values:
        for part in text.split('"'):
            if part.startswith("nodestro:enc:v1:"):
                assert part.encode() not in content
    # The workspace now opens with the new key only, and uses the secrets.
    with pytest.raises(NodestroError, match="does not fit"):
        open_workspace(data, old)
    workspace = open_workspace(data, new)
    try:
        assert workspace.provider_settings("anthropic")["api_key"] == SECRET
    finally:
        workspace.close()


def test_an_interrupted_rotation_can_be_run_again(tmp_path: Path) -> None:
    data, old, new = tmp_path / "data", new_key(), new_key()
    with_secrets(data, old)
    local_vault.rekey(data, old, new)
    assert local_vault.rekey(data, old, new) == {"provider": 0, "mcp": 0, "connection": 0}
    assert readable(data, new) == ALL


def test_rotation_stops_with_nothing_changed_on_a_secret_neither_key_opens(tmp_path: Path) -> None:
    data, old, new = tmp_path / "data", new_key(), new_key()
    with_secrets(data, old)
    connection = sqlite3.connect(data / DATABASE)
    with connection:  # a connection secret sealed with a third key
        _, foreign = vaults(new_key())
        text = f'{{"id":"con-2","secret_sealed":"{foreign.seal("con-2", "x")}"}}'
        connection.execute(
            "INSERT INTO nodestro_documents (collection, id, data) VALUES ('service_connections', 'con-2', ?)", (text,)
        )
    connection.close()
    before = documents(data)
    with pytest.raises(NodestroError) as refused:
        local_vault.rekey(data, old, new)
    assert refused.value.code == "secrets.vault_key_unreadable" and refused.value.params["unreadable"] == 1
    assert documents(data) == before


def test_rotation_with_a_wrong_old_key_is_refused(tmp_path: Path) -> None:
    data, old = tmp_path / "data", new_key()
    with_secrets(data, old)
    before = documents(data)
    with pytest.raises(NodestroError, match="does not fit"):
        local_vault.rekey(data, new_key(), new_key())
    assert documents(data) == before


def test_rotation_needs_a_stopped_data_directory(tmp_path: Path) -> None:
    data, old = tmp_path / "data", new_key()
    workspace = open_workspace(data, old)
    try:
        with pytest.raises(NodestroError) as refused:
            local_vault.rekey(data, old, new_key())
        assert refused.value.code == "workspace.in_use"
    finally:
        workspace.close()


def test_rotation_of_an_empty_data_directory_does_nothing(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    assert local_vault.rekey(tmp_path / "data", new_key(), new_key()) == {"provider": 0, "mcp": 0, "connection": 0}
    assert not (tmp_path / "data" / DATABASE).exists()


# ---- key loss -----------------------------------------------------------------------------------------------------


def test_after_a_key_loss_unreadable_secrets_are_removed_only_on_request(tmp_path: Path) -> None:
    data, lost, replacement = tmp_path / "data", new_key(), new_key()
    with_secrets(data, lost)
    with pytest.raises(NodestroError, match="does not fit"):
        open_workspace(data, replacement)

    assert local_vault.discard_unreadable(data, replacement) == {"provider": 1, "mcp": 2, "connection": 1}
    assert readable(data, replacement) == dict.fromkeys(ALL)
    found = documents(data)
    assert '"secret_storage":"none"' in found[("service_connections", "con-1")]
    assert "nodestro:enc:v1:" not in found[("mcp_servers", "srv-1")]
    workspace = open_workspace(data, replacement)
    try:
        assert "api_key" not in workspace.provider_settings("anthropic")
        workspace.update_settings(SettingsIn(providers={"anthropic": ProviderSettingsIn(api_key=SECRET)}))
        assert workspace.provider_settings("anthropic")["api_key"] == SECRET
    finally:
        workspace.close()


def test_secrets_the_key_opens_are_kept_on_a_key_loss(tmp_path: Path) -> None:
    data, key = tmp_path / "data", new_key()
    with_secrets(data, key)
    assert local_vault.discard_unreadable(data, key) == {"provider": 0, "mcp": 0, "connection": 0}
    assert readable(data, key) == ALL
