"""The key of a local data directory: which secrets it seals, whether a key fits, rotation and key loss (LD-020).

A local data directory (the desktop, Nodestro Local Docker) seals its secrets with one key, the ``vault_key`` of
:class:`~nodestro_runtime.workspace.Workspace` (:class:`~nodestro_runtime.server_settings.SecretBox`, tenant
``local``):

- provider API keys: the settings document ``settings/app``, ``providers.<name>.api_key``;
- MCP tokens and pending OAuth sign-ins: ``mcp_servers/<id>``, ``token_sealed`` and ``oauth_pending_sealed``;
- the secrets of service connections: ``service_connections/<id>``, ``secret_sealed``.

**Key check.** A workspace opened with ``verify_vault_key`` stores a check value sealed with its key on the first
opening (``local_vault/check``) and refuses every later opening with a key that cannot open it
(``secrets.vault_key_mismatch``): a new key is never silently laid over secrets sealed with another one. Data from
before the check value (no check value yet) is refused when it holds sealed secrets and none of them opens with the
key. The check value is a fixed text, sealed like every other secret: it tells nothing about the key.

**Rotation and key loss** work on a stopped data directory (its directory lock is taken) with SQLite directly, without
a schema migration, so they also work on the copy of an earlier release's data that a rollback restores:

- :func:`rekey` seals every secret again with a new key in one transaction. A secret that opens with neither key
  stops it with nothing changed; one that already opens with the new key is left as it is, so an interrupted
  rotation can be run again with the same two keys. Freed pages are overwritten (``secure_delete``) and the
  write-ahead log is emptied, so the old ciphertexts do not stay in the database file.
- :func:`discard_unreadable` is the explicit answer to a lost key: it removes every secret that does not open with
  the given key (they have to be entered again) and stores that key's check value.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .problems import NodestroError
from .server_settings import ENCRYPTED_PREFIX, SecretBox
from .storage import RawDocuments, stopped_documents

#: Where the check value is stored (a collection of its own: backups and restores keep the current one).
CHECK_COLLECTION = "local_vault"
CHECK_ID = "check"
_CHECK_FIELD = "local_vault.check"
_CHECK_TEXT = "nodestro local vault key check v1"
#: The collections that hold sealed secrets of a local data directory.
SETTINGS = "settings"
MCP_SERVERS = "mcp_servers"
CONNECTIONS = "service_connections"
COLLECTIONS = (SETTINGS, MCP_SERVERS, CONNECTIONS)


@dataclass(frozen=True)
class Sealed:
    """One sealed secret: where it is (``collection``, ``id``, the ``path`` in the document), the ``field`` it is bound
    to when sealed and its sealed ``value``."""

    collection: str
    id: str
    path: tuple[str, ...]
    field: str
    value: str
    kind: str


def sealed_in(collection: str, doc_id: str, data: Any) -> list[Sealed]:
    """The sealed secrets of one document (only values in the ``nodestro:enc:v1:`` format)."""
    found: list[tuple[tuple[str, ...], str, Any, str]] = []
    if not isinstance(data, dict):
        return []
    if collection == SETTINGS and doc_id == "app":
        providers = data.get("providers")
        if isinstance(providers, dict):
            for name, entry in providers.items():
                if isinstance(entry, dict):
                    found.append(
                        (("providers", name, "api_key"), f"providers.{name}.api_key", entry.get("api_key"), "provider")
                    )
    elif collection == MCP_SERVERS:
        found.append((("token_sealed",), f"mcp:{doc_id}", data.get("token_sealed"), "mcp"))
        found.append(
            (("oauth_pending_sealed",), f"mcp:{doc_id}:oauth-pending", data.get("oauth_pending_sealed"), "mcp")
        )
    elif collection == CONNECTIONS:
        found.append((("secret_sealed",), f"connection:{doc_id}", data.get("secret_sealed"), "connection"))
    return [
        Sealed(collection, doc_id, path, field, value, kind)
        for path, field, value, kind in found
        if isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX)
    ]


def _opens(box: SecretBox, tenant_id: str, field: str, value: str) -> str | None:
    try:
        return box.decrypt(tenant_id, field, value)
    except NodestroError:
        return None


def _check_opens(box: SecretBox, tenant_id: str, check: Any) -> bool:
    value = check.get("value") if isinstance(check, dict) else None
    return isinstance(value, str) and _opens(box, tenant_id, _CHECK_FIELD, value) == _CHECK_TEXT


def check_value(box: SecretBox, tenant_id: str) -> dict[str, str]:
    """A new check value for ``box``'s key."""
    return {"value": box.encrypt(tenant_id, _CHECK_FIELD, _CHECK_TEXT)}


def missing() -> NodestroError:
    return NodestroError(
        "secrets.vault_key_missing",
        "No key is available for this data directory's secrets; they are never stored unencrypted. Nodestro Local "
        "Docker reads its key file through the launcher (./nodestro start, .\\nodestro.ps1 start)",
    )


def mismatch(sealed: int) -> NodestroError:
    return NodestroError(
        "secrets.vault_key_mismatch",
        "The key does not fit this data directory: its encrypted secrets were sealed with another key. Put the "
        "earlier key file back, or accept the loss of those secrets explicitly (Nodestro Local Docker: "
        "./nodestro accept-new-vault-key --yes)",
        sealed=sealed,
    )


def verify(
    documents: Iterable[tuple[str, str, Any]], check: Any, box: SecretBox, tenant_id: str
) -> dict[str, str] | None:
    """Whether ``box`` fits the data: ``None`` when its check value opens, a new check value to store when there is
    none yet (and the data holds no sealed secret, or one that opens). Raises ``secrets.vault_key_mismatch``."""
    if check is not None:
        if not _check_opens(box, tenant_id, check):
            raise mismatch(sum(len(sealed_in(*item)) for item in documents))
        return None
    sealed = [item for document in documents for item in sealed_in(*document)]
    if sealed and not any(_opens(box, tenant_id, item.field, item.value) is not None for item in sealed):
        raise mismatch(len(sealed))
    return check_value(box, tenant_id)


class _Document(BaseModel):
    model_config = ConfigDict(extra="allow")


def verify_store(store: Any, box: SecretBox, tenant_id: str) -> None:
    """:func:`verify` on an open document store; stores the check value when there is none yet."""
    documents: list[tuple[str, str, Any]] = []
    app = store.get_raw(SETTINGS, "app")
    if app is not None:
        documents.append((SETTINGS, "app", app))
    for collection in (MCP_SERVERS, CONNECTIONS):
        documents += [(collection, key, item.model_dump()) for key, item in store.items(collection, _Document)]
    new = verify(documents, store.get_raw(CHECK_COLLECTION, CHECK_ID), box, tenant_id)
    if new is not None:
        store.put_raw(CHECK_COLLECTION, CHECK_ID, new)


# ---- maintenance on a stopped data directory ----------------------------------------------------------------------


@contextmanager
def _offline(data_dir: Path) -> Iterator[RawDocuments | None]:
    """The documents of a stopped data directory in one write transaction (``None``: there is no database yet)."""
    from .workspace import DATABASE, DataDirectoryLock

    if not data_dir.is_dir():  # a volume that never held a data directory (an update's copy of a fresh install)
        yield None
        return
    lock = DataDirectoryLock(data_dir)
    try:
        with stopped_documents(data_dir / DATABASE, (*COLLECTIONS, CHECK_COLLECTION)) as documents:
            yield documents
    finally:
        lock.release()


def _documents(raw: RawDocuments) -> dict[tuple[str, str], Any]:
    documents: dict[tuple[str, str], Any] = {}
    for key, data in raw.documents.items():
        try:
            documents[key] = json.loads(data)
        except ValueError:
            continue  # a damaged document holds no secret this module could read
    return documents


def _set(document: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = document
    for part in path[:-1]:
        target = target[part]
    if value is None and len(path) == 3:  # a provider key: the entry has no key any more
        target.pop(path[-1], None)
    else:
        target[path[-1]] = value


def _write(raw: RawDocuments, collection: str, key: str, data: Any) -> None:
    raw.put(collection, key, json.dumps(data, separators=(",", ":")))


def _box(key: str) -> SecretBox:
    """The box of a key file's key (base64, at least 32 bytes), as ``nodestro_runtime.mcp.vault`` reads it."""
    text = key.strip()
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=False)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) < 32:
        raise NodestroError("secrets.vault_key_invalid", "The key is not 32 random bytes, base64-encoded")
    return SecretBox(raw)


def rekey(data_dir: str | Path, old_key: str, new_key: str, *, tenant_id: str = "local") -> dict[str, int]:
    """Seal every secret of a stopped data directory with ``new_key`` instead of ``old_key`` (see the module
    documentation); returns how many secrets of each kind were sealed again."""
    old, new = _box(old_key), _box(new_key)
    counts = {"provider": 0, "mcp": 0, "connection": 0}
    with _offline(Path(data_dir)) as raw:
        if raw is None:
            return counts
        documents = _documents(raw)
        check = documents.pop((CHECK_COLLECTION, CHECK_ID), None)
        if check is not None and not (_check_opens(old, tenant_id, check) or _check_opens(new, tenant_id, check)):
            raise mismatch(sum(len(sealed_in(c, k, d)) for (c, k), d in documents.items()))
        unreadable = 0
        changed: set[tuple[str, str]] = set()
        for (collection, key), data in documents.items():
            for item in sealed_in(collection, key, data):
                if _opens(new, tenant_id, item.field, item.value) is not None:
                    continue  # sealed with the new key already (an interrupted rotation)
                plain = _opens(old, tenant_id, item.field, item.value)
                if plain is None:
                    unreadable += 1
                    continue
                _set(data, item.path, new.encrypt(tenant_id, item.field, plain))
                changed.add((collection, key))
                counts[item.kind] += 1
        if unreadable:
            raise NodestroError(
                "secrets.vault_key_unreadable",
                f"{unreadable} stored secret(s) open with neither key; nothing was changed. Remove them first "
                "(Nodestro Local Docker: ./nodestro accept-new-vault-key --yes with the current key) or use the "
                "right earlier key",
                unreadable=unreadable,
            )
        for collection, key in sorted(changed):
            _write(raw, collection, key, documents[(collection, key)])
        _write(raw, CHECK_COLLECTION, CHECK_ID, check_value(new, tenant_id))
    return counts


def discard_unreadable(data_dir: str | Path, key: str, *, tenant_id: str = "local") -> dict[str, int]:
    """Remove every secret of a stopped data directory that does not open with ``key`` and make ``key`` the data
    directory's key (the explicit answer to a lost key); returns how many secrets of each kind were removed."""
    box = _box(key)
    counts = {"provider": 0, "mcp": 0, "connection": 0}
    with _offline(Path(data_dir)) as raw:
        if raw is None:
            return counts
        documents = _documents(raw)
        documents.pop((CHECK_COLLECTION, CHECK_ID), None)
        for (collection, doc_key), data in documents.items():
            dropped = [
                item
                for item in sealed_in(collection, doc_key, data)
                if _opens(box, tenant_id, item.field, item.value) is None
            ]
            for item in dropped:
                _set(data, item.path, None)
                if collection == CONNECTIONS:
                    data["secret_storage"] = "none"
                counts[item.kind] += 1
            if dropped:
                _write(raw, collection, doc_key, data)
        _write(raw, CHECK_COLLECTION, CHECK_ID, check_value(box, tenant_id))
    return counts


__all__ = [
    "CHECK_COLLECTION",
    "CHECK_ID",
    "Sealed",
    "check_value",
    "discard_unreadable",
    "mismatch",
    "missing",
    "rekey",
    "sealed_in",
    "verify",
    "verify_store",
]
