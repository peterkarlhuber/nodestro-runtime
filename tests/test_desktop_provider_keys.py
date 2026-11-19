"""Provider API keys on a desktop are stored only encrypted when the keychain key is available (security fix N-03).

The desktop app hands the backend a key from the operating system's keychain (``NODESTRO_VAULT_KEY`` through the
handoff, see :mod:`nodestro_runtime.mcp.vault`). With it, provider keys are sealed like MCP tokens and connection
secrets, and keys that an earlier release stored in plain text are encrypted when the workspace opens. Without a key
the earlier behaviour stays (a plain key in the user-only database file), and an encrypted key is never handed to a
provider.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
import sqlite3

import pytest

from nodestro_runtime.mcp.vault import VAULT_KEY_ENV
from nodestro_runtime.providers import ProviderSettingsIn
from nodestro_runtime.schemas import SettingsIn
from nodestro_runtime.server_settings import is_encrypted
from nodestro_runtime.workspace import DATABASE, Workspace

SECRET = "sk-ant-canary-0123456789abcdefghij"
OTHER = "sk-openai-canary-9876543210zyxwvu"


def new_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture(autouse=True)
def no_operator_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VAULT_KEY_ENV, raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def raw_database(data: Path) -> bytes:
    """Every byte of the database, its write-ahead log included (a key must not be readable in either)."""
    found = b""
    for path in data.glob(DATABASE + "*"):
        found += path.read_bytes()
    return found


def stored_settings(workspace: Workspace) -> dict:
    return workspace.store.get_raw("settings", "app") or {}


def set_keys(workspace: Workspace, **keys: str) -> None:
    workspace.update_settings(
        SettingsIn(providers={name: ProviderSettingsIn(api_key=key) for name, key in keys.items()})
    )


def checkpoint(workspace: Workspace) -> None:
    """Move the write-ahead log into the database file, so the raw bytes show what is stored."""
    connection = sqlite3.connect(workspace.data_dir / DATABASE)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def test_with_the_keychain_key_a_new_key_is_stored_only_encrypted(tmp_path: Path) -> None:
    key = new_key()
    workspace = Workspace(tmp_path / "data", integrations=[], vault_key=key)
    try:
        set_keys(workspace, anthropic=SECRET)
        entry = stored_settings(workspace)["providers"]["anthropic"]
        assert is_encrypted(entry["api_key"]) and SECRET not in entry["api_key"]
        # The runtime uses the real key, and the status still never shows it.
        assert workspace.provider_settings("anthropic")["api_key"] == SECRET
        assert SECRET in workspace.secret_keys()
        status = next(item for item in workspace.settings().providers if item.id == "anthropic")
        assert status.key_stored and SECRET not in status.model_dump_json()
        checkpoint(workspace)
    finally:
        workspace.close()
    assert SECRET.encode() not in raw_database(tmp_path / "data")
    # Opened again with the same key, the key is still usable.
    again = Workspace(tmp_path / "data", integrations=[], vault_key=key)
    try:
        assert again.provider_settings("anthropic")["api_key"] == SECRET
    finally:
        again.close()


def test_plain_keys_of_an_earlier_release_are_encrypted_on_opening(tmp_path: Path) -> None:
    data = tmp_path / "data"
    old = Workspace(data, integrations=[])
    try:
        set_keys(old, anthropic=SECRET, openai=OTHER)
        values = stored_settings(old)
        assert values["providers"]["anthropic"]["api_key"] == SECRET  # the earlier, plain storage
        values["api_key"] = SECRET  # the top-level key of the oldest releases
        old.store.put_raw("settings", "app", values)
    finally:
        old.close()

    key = new_key()
    migrated = Workspace(data, integrations=[], vault_key=key)
    try:
        values = stored_settings(migrated)
        assert "api_key" not in values
        for name, plain in (("anthropic", SECRET), ("openai", OTHER)):
            sealed = values["providers"][name]["api_key"]
            assert is_encrypted(sealed) and plain not in sealed
            assert migrated.provider_settings(name)["api_key"] == plain
        # Other provider settings are kept as they were.
        assert set(values["providers"]) == {"anthropic", "openai"}
        checkpoint(migrated)
    finally:
        migrated.close()
    raw = raw_database(data)
    assert SECRET.encode() not in raw and OTHER.encode() not in raw

    # A second opening changes nothing: the migration is idempotent.
    before = None
    reopened = Workspace(data, integrations=[], vault_key=key)
    try:
        before = stored_settings(reopened)["providers"]["anthropic"]["api_key"]
    finally:
        reopened.close()
    last = Workspace(data, integrations=[], vault_key=key)
    try:
        assert stored_settings(last)["providers"]["anthropic"]["api_key"] == before
    finally:
        last.close()


def test_an_encrypted_key_is_never_used_without_the_keychain_key(tmp_path: Path) -> None:
    data = tmp_path / "data"
    sealed_workspace = Workspace(data, integrations=[], vault_key=new_key())
    try:
        set_keys(sealed_workspace, anthropic=SECRET)
        sealed = stored_settings(sealed_workspace)["providers"]["anthropic"]["api_key"]
    finally:
        sealed_workspace.close()

    # Started without the keychain (for example from the command line): the key counts as missing ...
    plain = Workspace(data, integrations=[])
    try:
        assert "api_key" not in plain.provider_settings("anthropic")
        assert sealed not in plain.secret_keys()
        status = next(item for item in plain.settings().providers if item.id == "anthropic")
        assert not status.key_stored
        # ... and is not dropped: another setting change keeps the encrypted value for the next keychain start.
        plain.update_settings(SettingsIn(default_budget_usd=3.0))
        assert stored_settings(plain)["providers"]["anthropic"]["api_key"] == sealed
    finally:
        plain.close()


def test_a_different_keychain_key_makes_the_key_missing_not_revealed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    first = Workspace(data, integrations=[], vault_key=new_key())
    try:
        set_keys(first, anthropic=SECRET)
    finally:
        first.close()
    second = Workspace(data, integrations=[], vault_key=new_key())
    try:
        assert "api_key" not in second.provider_settings("anthropic")
        set_keys(second, anthropic=OTHER)  # entered again: stored with the new key
        assert second.provider_settings("anthropic")["api_key"] == OTHER
    finally:
        second.close()


def test_the_operators_variable_is_the_key_without_a_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VAULT_KEY_ENV, new_key())
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        set_keys(workspace, anthropic=SECRET)
        assert is_encrypted(stored_settings(workspace)["providers"]["anthropic"]["api_key"])
        # The same key seals MCP tokens and connection secrets.
        assert workspace.mcp.vault.persistent and workspace.connections.vault.persistent
    finally:
        workspace.close()


def test_without_a_key_store_the_earlier_storage_stays(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        set_keys(workspace, anthropic=SECRET)
        assert stored_settings(workspace)["providers"]["anthropic"]["api_key"] == SECRET
        assert workspace.provider_settings("anthropic")["api_key"] == SECRET
        assert not workspace.mcp.vault.persistent and not workspace.connections.vault.persistent
    finally:
        workspace.close()


def test_a_malformed_keychain_key_is_refused(tmp_path: Path) -> None:
    from nodestro_runtime.problems import NodestroError

    with pytest.raises(NodestroError) as caught:
        Workspace(tmp_path / "data", integrations=[], vault_key="short")
    assert caught.value.code == "profile.invalid"
