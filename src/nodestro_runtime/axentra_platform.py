"""Axentra platform services in the server profile (opt-in; see ``docs/AXENTRA.md``).

Axentra is the generic SaaS backend library (identity, tenants, permissions, audit, usage, keyring, storage, ...).
Nodestro embeds it as a Python library in its **server profile only**: the same PostgreSQL database, Axentra's tables
(``axentra_*``) next to Nodestro's (``nodestro_*``), one Axentra product ``nodestro`` in single-product mode. The
desktop and Local Docker profiles never import this module and never need the package.

Everything is off by default. The operator switches it on with environment variables, read once at start-up together
with the other server settings (:class:`~nodestro_runtime.server_settings.ServerSettings`):

==========================================  ================================================================
``NODESTRO_AXENTRA``                        ``off`` (default) or ``on``: open Axentra next to Nodestro
                                            (needs Python 3.11+ and ``nodestro-runtime[postgres]``)
``NODESTRO_AXENTRA_KEYRING`` (or ``_FILE``) Axentra keyring ``kid:base64key[,kid2:base64key]`` (first key
                                            encrypts); required with ``NODESTRO_AXENTRA=on``
``NODESTRO_AXENTRA_CONFIG``                 optional Axentra TOML file for Axentra's own knobs; Nodestro's
                                            values (database, product, mode, keyring) always win
``NODESTRO_AXENTRA_ALLOW_SUPERUSER``        ``1`` lets Axentra run on a superuser / ``BYPASSRLS`` role (its
                                            row-level security is then no defense in depth); default refused
``NODESTRO_SECRETS_FORMAT``                 ``v1`` (default) or ``axs1``: format of newly written tenant secrets
                                            (``axs1`` = Axentra keyring with rotation; needs NODESTRO_AXENTRA=on)
==========================================  ================================================================

:class:`KeyringSecretBox` reads both secret formats (``nodestro:enc:v1:`` and Axentra's ``axs1.<kid>.…``) and writes
the configured one, so switching formats never makes a stored value unreadable. :func:`reencrypt_tenant_secrets`
moves every stored tenant secret to the active Axentra key (resumable, idempotent, never overwrites a value it cannot
read). Nothing here changes Nodestro's external contracts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING, Any, Literal

from .problems import NodestroError

if TYPE_CHECKING:  # pragma: no cover
    from .server_settings import SecretBox
    from .storage_postgres import PostgresDatabase

logger = logging.getLogger(__name__)

#: The Axentra product Nodestro's tenants belong to (single-product mode).
PRODUCT_SLUG = "nodestro"
PRODUCT_NAME = "Nodestro"
#: Axentra modules Nodestro uses; the others (billing, notify, storage, search, data, events) stay off until a
#: migration step needs them, so Axentra does not start background behaviour nobody asked for.
MODULES = ("identity", "tenancy", "access", "audit", "usage")
#: Prefix of Axentra ciphertexts (``axs1.<kid>.<nonce>.<ciphertext>``).
AXS1_PREFIX = "axs1."
SecretsFormat = Literal["v1", "axs1"]
_ON = frozenset({"1", "on", "true", "yes"})
_OFF = frozenset({"", "0", "off", "false", "no"})


def available() -> bool:
    """Whether the ``axentra`` package can be imported (Python 3.11+ and ``nodestro-runtime[postgres]``)."""
    if sys.version_info < (3, 11):
        return False
    try:
        import axentra  # noqa: F401
    except ImportError:
        return False
    return True


def axentra_version() -> str | None:
    """The installed Axentra version (package metadata), ``None`` without the package."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("axentra")
    except PackageNotFoundError:
        return None


@dataclass(frozen=True)
class AxentraConfig:
    """The operator's Axentra settings (see the module documentation); the keyring stays out of ``repr``."""

    enabled: bool = False
    keyring: str = field(default="", repr=False)
    #: Where the keyring came from, as an Axentra secret reference (``env:NAME`` or ``file:/path``).
    keyring_ref: str = ""
    config_file: str | None = None
    allow_superuser: bool = False
    secrets_format: SecretsFormat = "v1"

    @classmethod
    def from_env(cls, environ: Mapping[str, str], problems: list[dict[str, str]]) -> AxentraConfig:
        """The settings in ``environ``; every problem is appended to ``problems`` (nothing raises here)."""

        def problem(name: str, value: str, reason: str) -> None:
            problems.append({"field": name, "value": value, "reason": reason})

        switch = environ.get("NODESTRO_AXENTRA", "").strip().lower()
        if switch not in _ON | _OFF:
            problem("NODESTRO_AXENTRA", switch, "NODESTRO_AXENTRA must be on or off")
        enabled = switch in _ON
        fmt = environ.get("NODESTRO_SECRETS_FORMAT", "").strip().lower() or "v1"
        if fmt not in ("v1", "axs1"):
            problem("NODESTRO_SECRETS_FORMAT", fmt, "NODESTRO_SECRETS_FORMAT must be v1 or axs1")
            fmt = "v1"
        superuser = environ.get("NODESTRO_AXENTRA_ALLOW_SUPERUSER", "").strip().lower()
        if superuser not in _ON | _OFF:
            problem("NODESTRO_AXENTRA_ALLOW_SUPERUSER", superuser, "NODESTRO_AXENTRA_ALLOW_SUPERUSER must be 1 or 0")
        config_file = environ.get("NODESTRO_AXENTRA_CONFIG", "").strip() or None
        keyring, ref = "", ""
        direct = environ.get("NODESTRO_AXENTRA_KEYRING", "").strip()
        path = environ.get("NODESTRO_AXENTRA_KEYRING_FILE", "").strip()
        if direct and path:
            problem("NODESTRO_AXENTRA_KEYRING", "", "set either NODESTRO_AXENTRA_KEYRING or its _FILE, not both")
        elif path:
            try:
                keyring = Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                problem("NODESTRO_AXENTRA_KEYRING_FILE", path, f"NODESTRO_AXENTRA_KEYRING_FILE cannot be read: {exc}")
            ref = f"file:{path}"
        elif direct:
            keyring, ref = direct, "env:NODESTRO_AXENTRA_KEYRING"
        if not enabled:
            if fmt == "axs1":
                problem("NODESTRO_SECRETS_FORMAT", fmt, "NODESTRO_SECRETS_FORMAT=axs1 needs NODESTRO_AXENTRA=on")
            return cls(secrets_format=fmt)  # type: ignore[arg-type]
        if not available():
            problem(
                "NODESTRO_AXENTRA",
                switch,
                "NODESTRO_AXENTRA=on needs the axentra package (Python 3.11+, nodestro-runtime[postgres])",
            )
        if not keyring:
            if not (direct and path):
                problem("NODESTRO_AXENTRA_KEYRING", "", "NODESTRO_AXENTRA_KEYRING (or _FILE) is required")
        elif available():
            from axentra.config.secrets import Keyring
            from axentra.core.errors import ConfigurationError

            try:
                Keyring.parse(keyring)
            except ConfigurationError as exc:
                problem("NODESTRO_AXENTRA_KEYRING", "", f"NODESTRO_AXENTRA_KEYRING is invalid: {exc}")
        if config_file and not Path(config_file).is_file():
            problem("NODESTRO_AXENTRA_CONFIG", config_file, "NODESTRO_AXENTRA_CONFIG is not a readable file")
        return cls(
            enabled=True,
            keyring=keyring,
            keyring_ref=ref,
            config_file=config_file,
            allow_superuser=superuser in _ON,
            secrets_format=fmt,  # type: ignore[arg-type]
        )


def _with_search_path(conninfo: str, schema: str | None) -> str:
    """``conninfo`` whose sessions use ``schema`` first, so Axentra's tables live next to Nodestro's."""
    if not schema:
        return conninfo
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parts = conninfo_to_dict(conninfo)
    options = str(parts.get("options") or "")
    parts["options"] = f"{options} -c search_path={schema}".strip()
    return make_conninfo("", **parts)


def build_settings(config: AxentraConfig, *, database_url: str, public_origin: str, schema: str | None = None) -> Any:
    """Axentra's :class:`~axentra.config.settings.Settings` for Nodestro's server (``config.enabled`` only).

    The operator's ``NODESTRO_AXENTRA_CONFIG`` file is read first; Nodestro then fixes what must agree with it:
    the database (the server's own, same schema), single-product mode with product ``nodestro``, the modules and the
    environment (production for an https origin). The keyring is passed to Axentra directly, never as a setting."""
    from axentra.config.settings import load_settings

    production = public_origin.startswith("https://")
    url = _with_search_path(database_url, schema)
    overrides: dict[str, Any] = {
        "environment": "production" if production else "development",
        "mode": "single",
        "modules": list(MODULES),
        "product": {"slug": PRODUCT_SLUG, "name": PRODUCT_NAME},
        "database": {"url": url, "allow_superuser": config.allow_superuser},
        # The keyring is not a setting here: Nodestro validated it and hands it to Axentra (AxentraServices).
        "security": {"public_url": public_origin, "master_keys_ref": None, "master_keys": None},
        "observability": {"otel_service_name": "nodestro"},
    }
    # Only the given file and Nodestro's values: AXENTRA__* variables of the process are ignored on purpose, so the
    # server's behaviour depends on documented NODESTRO_* settings and one optional file.
    return load_settings(config.config_file, environ={}, **_merge_file(config.config_file, overrides))


def _merge_file(path: str | None, overrides: dict[str, Any]) -> dict[str, Any]:
    """``overrides`` deep-merged over the sections the file sets (load_settings merges top-level keys only)."""
    if not path:
        return overrides
    data = _read_toml(path)
    merged = dict(overrides)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            merged[key] = {**data[key], **value}
    return merged


def _read_toml(path: str) -> dict[str, Any]:
    if sys.version_info < (3, 11):  # pragma: no cover - Axentra needs 3.11+
        raise NodestroError("profile.invalid", "NODESTRO_AXENTRA_CONFIG needs Python 3.11+")
    else:
        import tomllib

        try:
            return tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise NodestroError("profile.invalid", f"NODESTRO_AXENTRA_CONFIG cannot be read: {exc}") from None


class AxentraServices:
    """Axentra opened for one Nodestro server process (API, worker or an operator command).

    ``platform`` is the :class:`axentra.platform.Axentra` facade (services as attributes). Construction opens
    nothing; :meth:`start` opens the pool, checks the database role and creates/confirms the product ``nodestro``.
    """

    def __init__(self, config: AxentraConfig, *, database_url: str, public_origin: str, schema: str | None = None):
        if not config.enabled:
            raise NodestroError("axentra.disabled", "Axentra is not switched on (NODESTRO_AXENTRA)")
        from axentra.config.secrets import Keyring
        from axentra.platform import Axentra

        self.config = config
        self.settings = build_settings(config, database_url=database_url, public_origin=public_origin, schema=schema)
        self.platform = Axentra(self.settings, keyring=Keyring.parse(config.keyring))
        self._product_id: str | None = None

    # ---- lifecycle ---------------------------------------------------------------------------------

    def start(self) -> None:
        from axentra.core.errors import AxentraError

        try:
            self.platform.start()
        except AxentraError as exc:
            raise NodestroError("axentra.unavailable", f"Axentra cannot start: {exc}") from None
        product = self.platform.single_product()
        if product is None:  # pragma: no cover - start() creates it in single mode
            raise NodestroError("axentra.unavailable", "Axentra has no product")
        self._product_id = product.id

    def close(self) -> None:
        self.platform.close()

    @property
    def product_id(self) -> str:
        if self._product_id is None:
            raise NodestroError("axentra.unavailable", "Axentra is not started")
        return self._product_id

    # ---- schema --------------------------------------------------------------------------------------

    def pending_migrations(self) -> list[str]:
        """Axentra migrations not applied yet (``namespace:version_name``), empty when current (read-only)."""
        check = self.platform.migration_runner().check()
        return [migration.label for migration in check.pending]

    def schema_problems(self) -> list[str]:
        """Why Axentra's schema does not match this code: pending, unknown (newer) or changed migrations."""
        from axentra.core.errors import AxentraError

        try:
            self.platform.migration_runner().require_current()
        except AxentraError as exc:
            return [str(exc)]
        return []

    def migrate(self, before: Callable[[list[str]], None] | None = None) -> list[str]:
        """Apply Axentra's migrations; ``before(pending)`` (the backup) runs first, under Axentra's migration lock
        and only when something is pending. A database written by a newer Axentra is refused."""
        from axentra.core.errors import AxentraError

        hook = None if before is None else (lambda pending: before([migration.label for migration in pending]))
        try:
            return list(self.platform.migrate(before_apply=hook))
        except AxentraError as exc:
            raise NodestroError("storage.migration_failed", f"Axentra's migration failed: {exc}") from None

    def require_current_schema(self) -> None:
        problems = self.schema_problems()
        if problems:
            raise NodestroError(
                "storage.schema_mismatch",
                f"Axentra's schema is not current; run the migration first ({problems[0]})",
                problems=problems,
            )

    # ---- secrets -------------------------------------------------------------------------------------

    def secret_box(self, legacy: SecretBox | None) -> KeyringSecretBox:
        return KeyringSecretBox(legacy, self.platform.secrets, write=self.config.secrets_format)


def keyring_secret_box(config: AxentraConfig, legacy: SecretBox | None) -> KeyringSecretBox:
    """The tenant secret box for ``config`` without opening the database (only the keyring is needed)."""
    from axentra.config.secrets import Keyring
    from axentra.config.secrets import SecretBox as AxentraSecretBox

    return KeyringSecretBox(legacy, AxentraSecretBox(Keyring.parse(config.keyring)), write=config.secrets_format)


def secret_context(tenant_id: str, name: str) -> str:
    """The associated data of a tenant secret in Axentra's format: binds tenant and field, like v1 did."""
    return f"nodestro|tenant:{tenant_id}|{name}"


class KeyringSecretBox:
    """Tenant secrets with Axentra's rotatable keyring, still reading Nodestro's v1 values.

    Same interface as :class:`~nodestro_runtime.server_settings.SecretBox` (``encrypt``/``decrypt`` per tenant and
    field). ``write="axs1"`` encrypts new values with the active Axentra key; ``write="v1"`` keeps writing v1 (the
    keyring then only reads ``axs1`` values, e.g. after a rollback of the format switch). Decryption picks the format
    from the value. ``legacy=None`` retires v1: such values then read as ``secrets.unreadable`` (never deleted).
    """

    def __init__(self, legacy: SecretBox | None, box: Any, *, write: SecretsFormat = "axs1") -> None:
        if write == "v1" and legacy is None:
            raise ValueError("writing v1 secrets needs the legacy key")
        self.legacy = legacy
        self.box = box
        self.write = write

    def __repr__(self) -> str:
        return f"KeyringSecretBox(write={self.write!r}, legacy={'yes' if self.legacy else 'retired'})"

    @property
    def active_kid(self) -> str:
        return str(self.box.keyring.active_kid)

    def encrypt(self, tenant_id: str, name: str, plaintext: str) -> str:
        if self.write == "v1":
            assert self.legacy is not None
            return self.legacy.encrypt(tenant_id, name, plaintext)
        return str(self.box.encrypt(plaintext, secret_context(tenant_id, name)))

    def decrypt(self, tenant_id: str, name: str, token: str) -> str:
        if isinstance(token, str) and token.startswith(AXS1_PREFIX):
            try:
                return str(self.box.decrypt(token, secret_context(tenant_id, name)))
            except Exception:  # unknown key id, tampered value, other tenant or field
                raise NodestroError(
                    "secrets.unreadable",
                    "A stored secret cannot be decrypted with this server's keyring; enter it again",
                ) from None
        if self.legacy is None:
            raise NodestroError(
                "secrets.unreadable", "A stored secret uses the retired v1 format; this server no longer reads it"
            )
        return self.legacy.decrypt(tenant_id, name, token)

    def needs_rewrite(self, token: str) -> bool:
        """Whether ``token`` is not in the format and key this box writes."""
        if self.write == "v1":
            return False
        parts = token.split(".")
        return not (token.startswith(AXS1_PREFIX) and len(parts) == 4 and parts[1] == self.active_kid)


# ---- re-encryption of stored tenant secrets ----------------------------------------------------------


@dataclass(frozen=True)
class SecretField:
    """Where a tenant secret lives: a document collection and a function yielding ``(path, name)`` per document."""

    collection: str
    fields: Callable[[str, dict[str, Any]], Iterator[tuple[tuple[str, ...], str]]]


def _provider_keys(key: str, data: dict[str, Any]) -> Iterator[tuple[tuple[str, ...], str]]:
    if key != "app":
        return
    for provider, entry in (data.get("providers") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("api_key"), str):
            yield ("providers", provider, "api_key"), f"providers.{provider}.api_key"


def _mcp_tokens(key: str, data: dict[str, Any]) -> Iterator[tuple[tuple[str, ...], str]]:
    server_id = str(data.get("id") or key)
    if isinstance(data.get("token_sealed"), str):
        yield ("token_sealed",), f"mcp:{server_id}"
    if isinstance(data.get("oauth_pending_sealed"), str):
        yield ("oauth_pending_sealed",), f"mcp:{server_id}:oauth-pending"


def _billing_keys(key: str, data: dict[str, Any]) -> Iterator[tuple[tuple[str, ...], str]]:
    if isinstance(data.get("admin_key"), str):
        yield ("admin_key",), f"pricing.billing.{key}.admin_key"


#: Every place a server stores an encrypted tenant secret (see docs/AXENTRA.md, "Secrets").
SECRET_FIELDS: tuple[SecretField, ...] = (
    SecretField("settings", _provider_keys),
    SecretField("mcp_servers", _mcp_tokens),
    SecretField("pricing_billing", _billing_keys),
)


@dataclass
class ReencryptReport:
    """What :func:`reencrypt_tenant_secrets` found and did; ``failed`` lists ``tenant/collection/key:name`` only."""

    dry_run: bool
    active_kid: str
    total: int = 0
    by_format: dict[str, int] = field(default_factory=dict)
    pending: int = 0
    reencrypted: int = 0
    failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "active_kid": self.active_kid,
            "total": self.total,
            "by_format": dict(sorted(self.by_format.items())),
            "pending": self.pending,
            "reencrypted": self.reencrypted,
            "failed": list(self.failed),
        }


def _format_of(token: str) -> str:
    from .server_settings import ENCRYPTED_PREFIX

    if token.startswith(ENCRYPTED_PREFIX):
        return "v1"
    parts = token.split(".")
    if token.startswith(AXS1_PREFIX) and len(parts) == 4:
        return f"axs1:{parts[1]}"
    return "plain"


def _get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = data
    for part in path:
        node = node.get(part) if isinstance(node, dict) else None
    return node


def _set(data: dict[str, Any], path: tuple[str, ...], value: str) -> None:
    node = data
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value


def reencrypt_tenant_secrets(
    database: PostgresDatabase,
    box: KeyringSecretBox,
    *,
    dry_run: bool = False,
    tenant: str | None = None,
) -> ReencryptReport:
    """Move every stored tenant secret to the active Axentra key (``nodestro-server secrets reencrypt``).

    Guarantees: each document is re-encrypted in its own transaction with the row locked (``FOR UPDATE``), so a
    crash loses nothing and a rerun continues; only values not under the active key are touched (idempotent);
    a value that cannot be decrypted is **left exactly as it is** and reported (by location, never by content);
    plain-text values (which a server never keeps) are reported, not encrypted. ``dry_run`` only counts.
    """
    from .storage_postgres import _encode

    report = ReencryptReport(dry_run=dry_run, active_kid=box.active_kid)
    collections = [item.collection for item in SECRET_FIELDS]
    by_collection = {item.collection: item for item in SECRET_FIELDS}
    params: tuple[Any, ...] = (collections,)
    where = "collection = ANY(%s)"
    if tenant is not None:
        where += " AND tenant_id = %s"
        params += (tenant,)
    rows = database._query(
        f"SELECT tenant_id, collection, id FROM nodestro_documents WHERE {where} ORDER BY tenant_id, collection, id",
        params,
    )
    for tenant_id, collection, key in rows:
        spec = by_collection[collection]
        with database._transaction() as connection:
            found = connection.execute(
                "SELECT coalesce(raw, data::text) FROM nodestro_documents"
                " WHERE tenant_id = %s AND collection = %s AND id = %s FOR UPDATE",
                (tenant_id, collection, key),
            ).fetchone()
            if found is None:
                continue
            data = json.loads(found[0])
            if not isinstance(data, dict):
                continue
            dirty = False
            for path, name in spec.fields(key, data):
                token = _get(data, path)
                if not isinstance(token, str) or not token:
                    continue
                kind = _format_of(token)
                report.total += 1
                report.by_format[kind] = report.by_format.get(kind, 0) + 1
                where_label = f"{tenant_id}/{collection}/{key}:{name}"
                if kind == "plain":
                    report.failed.append(where_label + " (not encrypted)")
                    continue
                if not box.needs_rewrite(token):
                    continue
                report.pending += 1
                try:
                    plaintext = box.decrypt(tenant_id, name, token)
                except NodestroError:
                    report.failed.append(where_label)
                    logger.warning("Re-encryption: %s cannot be decrypted; left unchanged", where_label)
                    continue
                if dry_run:
                    continue
                _set(data, path, box.encrypt(tenant_id, name, plaintext))
                report.reencrypted += 1
                dirty = True
            if dirty:
                text, raw = _encode(json.dumps(data, ensure_ascii=False))
                connection.execute(
                    "UPDATE nodestro_documents SET data = %s::jsonb, raw = %s, updated_at = clock_timestamp()"
                    " WHERE tenant_id = %s AND collection = %s AND id = %s",
                    (text, raw, tenant_id, collection, key),
                )
    return report


# ---- links between Nodestro and Axentra records ----------------------------------------------------------------

_SLUG_BODY = re.compile(r"[^a-z0-9-]")
#: Advisory lock class of :meth:`AxentraLink.ensure_tenant` (two-key form; ``Axen`` in ASCII).
LINK_LOCK_CLASS = 0x4178656E


def tenant_slug(tenant_id: str) -> str:
    """An Axentra tenant slug (``[a-z0-9][a-z0-9-]{1,62}``) for a Nodestro tenant id (``[a-z0-9][a-z0-9_-]{0,62}``).

    Only a label: :data:`nodestro_axentra_ids` is authoritative. ``_`` becomes ``-`` and a one-character id gets
    ``-0``; a collision gets ``-2``, ``-3``, ... (see :meth:`AxentraLink.ensure_tenant`)."""
    slug = _SLUG_BODY.sub("-", tenant_id.lower().replace("_", "-"))[:63]
    return slug if len(slug) >= 2 else f"{slug}-0"


class AxentraLink:
    """Maps Nodestro records to Axentra's (``nodestro_axentra_ids``) and creates the Axentra side when missing.

    Every write runs inside a Nodestro transaction on Nodestro's own connection, with Axentra's services joined to
    it (:meth:`axentra.data.database.Database.joined`): the Nodestro row, the Axentra rows and the link commit or
    roll back together. Nothing is ever dual-written across two connections.
    """

    def __init__(self, services: AxentraServices, database: PostgresDatabase) -> None:
        self.services = services
        self.database = database
        self._tenants: dict[str, str] = {}

    @contextmanager
    def joined(self) -> Iterator[Any]:
        """A Nodestro transaction (reentrant) whose connection Axentra's services use too."""
        with self.database._transaction() as connection, self.services.platform.db.joined(connection):
            yield connection

    def lookup(self, kind: str, nodestro_id: str) -> str | None:
        rows = self.database._query(
            "SELECT axentra_id FROM nodestro_axentra_ids WHERE kind = %s AND nodestro_id = %s", (kind, nodestro_id)
        )
        return str(rows[0][0]) if rows else None

    def reverse(self, kind: str, axentra_id: str) -> str | None:
        rows = self.database._query(
            "SELECT nodestro_id FROM nodestro_axentra_ids WHERE kind = %s AND axentra_id = %s", (kind, axentra_id)
        )
        return str(rows[0][0]) if rows else None

    def link(self, connection: Any, kind: str, nodestro_id: str, axentra_id: str) -> None:
        connection.execute(
            "INSERT INTO nodestro_axentra_ids (kind, nodestro_id, axentra_id) VALUES (%s, %s, %s)",
            (kind, nodestro_id, axentra_id),
        )

    def tenant(self, nodestro_id: str) -> str | None:
        """The Axentra tenant of a Nodestro tenant (cached per process; links never change)."""
        found = self._tenants.get(nodestro_id)
        if found is None:
            found = self.lookup("tenant", nodestro_id)
            if found is not None:
                self._tenants[nodestro_id] = found
        return found

    def ensure_tenant(self, nodestro_id: str, name: str) -> str:
        """The Axentra tenant of ``nodestro_id``, created (product ``nodestro``) and linked when missing.

        Idempotent and safe across processes: an advisory lock per tenant id serializes creators, and the link,
        the Axentra tenant and its audit event commit in the caller's (or a new) Nodestro transaction."""
        from axentra.core.context import Principal

        known = self.tenant(nodestro_id)
        if known is not None:
            return known
        product_id = self.services.product_id
        outermost = not self.database.in_transaction  # inside a caller's transaction the link may still roll back
        with self.joined() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (LINK_LOCK_CLASS, f"tenant:{nodestro_id}")
            )
            row = connection.execute(
                "SELECT axentra_id FROM nodestro_axentra_ids WHERE kind = 'tenant' AND nodestro_id = %s",
                (nodestro_id,),
            ).fetchone()
            if row is not None:
                axentra_id = str(row[0])
            else:
                base = tenant_slug(nodestro_id)
                taken = {
                    str(item[0])
                    for item in connection.execute(
                        "SELECT slug FROM axentra_tenants WHERE product_id = %s AND (slug = %s OR slug LIKE %s)",
                        (product_id, base, base[:60] + "-%"),
                    ).fetchall()
                }
                slug, counter = base, 2
                while slug in taken:
                    slug, counter = f"{base[:60]}-{counter}", counter + 1
                created = self.services.platform.tenancy.create_tenant(
                    Principal.system("nodestro"),
                    product_id,
                    slug=slug,
                    name=name or nodestro_id,
                    settings={"nodestro_tenant": nodestro_id},
                )
                axentra_id = created.id
                self.link(connection, "tenant", nodestro_id, axentra_id)
        if outermost:
            self._tenants[nodestro_id] = axentra_id
        return axentra_id

    def backfill_tenants(self) -> dict[str, Any]:
        """Link every Nodestro tenant (``nodestro_tenants``) to an Axentra tenant; idempotent and resumable."""
        rows = self.database._query("SELECT id, name FROM nodestro_tenants ORDER BY id")
        created, linked = 0, 0
        for tenant_id, name in rows:
            if self.tenant(str(tenant_id)) is not None:
                linked += 1
                continue
            self.ensure_tenant(str(tenant_id), str(name))
            created += 1
        return {"tenants": len(rows), "already_linked": linked, "created": created}


def from_environment(environ: Mapping[str, str] | None = None) -> AxentraConfig:
    """:class:`AxentraConfig` from ``environ`` (default ``os.environ``); ``profile.invalid`` lists every problem."""
    problems: list[dict[str, str]] = []
    config = AxentraConfig.from_env(os.environ if environ is None else environ, problems)
    if problems:
        raise NodestroError(
            "profile.invalid",
            "The server cannot start: " + "; ".join(item["reason"] for item in problems),
            problems=problems,
        )
    return config


__all__ = [
    "AXS1_PREFIX",
    "MODULES",
    "PRODUCT_SLUG",
    "SECRET_FIELDS",
    "AxentraConfig",
    "AxentraLink",
    "AxentraServices",
    "KeyringSecretBox",
    "ReencryptReport",
    "available",
    "axentra_version",
    "build_settings",
    "from_environment",
    "keyring_secret_box",
    "reencrypt_tenant_secrets",
    "secret_context",
    "tenant_slug",
]
