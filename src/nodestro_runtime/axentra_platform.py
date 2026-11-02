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
``NODESTRO_AXENTRA_AUDIT``                  ``1``: mirror the approval audit trail and MCP events into Axentra's
                                            audit log (same transaction; needs NODESTRO_AXENTRA=on)
``NODESTRO_AXENTRA_USAGE``                  ``1``: mirror the AI usage ledger into Axentra meters (``nodestro.ai.*``;
                                            same transaction, idempotent; needs NODESTRO_AXENTRA=on)
``NODESTRO_AXENTRA_RUN_USAGE``              ``1``: also mirror the settled usage of finished workflow runs (one event
                                            per run and meter, deterministic key; needs NODESTRO_AXENTRA_USAGE=1)
``NODESTRO_AXENTRA_NOTIFY``                 ``1``: in-app inbox notifications through Axentra's notify module
                                            (failed runs, pending tool approvals; never e-mail; needs ``on``)
``NODESTRO_IDENTITY_BACKEND``               ``nodestro`` (default), ``mirror`` or ``axentra``: where users,
                                            tenants, memberships and sessions live (``mirror``/``axentra`` need
                                            NODESTRO_AXENTRA=on; see :mod:`nodestro_runtime.axentra_identity`)
``NODESTRO_FILES_BACKEND``                  ``volume`` (default) or ``axentra``: where the bytes of input files
                                            live (``axentra`` needs NODESTRO_AXENTRA=on; see
                                            :mod:`nodestro_runtime.axentra_storage`)
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
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING, Any, Literal

from .axentra_metrics import count_operation
from .problems import NodestroError

if TYPE_CHECKING:  # pragma: no cover
    from .server_settings import SecretBox
    from .storage_postgres import PostgresDatabase

logger = logging.getLogger(__name__)

#: The Axentra product Nodestro's tenants belong to (single-product mode).
PRODUCT_SLUG = "nodestro"
PRODUCT_NAME = "Nodestro"
#: Axentra modules Nodestro uses; the others (billing, notify, search, data, events) stay off until a migration step
#: needs them, so Axentra does not start background behaviour nobody asked for. ``storage`` is added only with
#: ``NODESTRO_FILES_BACKEND=axentra`` (:attr:`AxentraConfig.modules`).
MODULES = ("identity", "tenancy", "access", "audit", "usage")
#: Prefix of Axentra ciphertexts (``axs1.<kid>.<nonce>.<ciphertext>``).
AXS1_PREFIX = "axs1."
SecretsFormat = Literal["v1", "axs1"]
IdentityBackend = Literal["nodestro", "mirror", "axentra"]
IDENTITY_BACKENDS: tuple[IdentityBackend, ...] = ("nodestro", "mirror", "axentra")
FilesBackend = Literal["volume", "axentra"]
FILES_BACKENDS: tuple[FilesBackend, ...] = ("volume", "axentra")
#: Days a migrated volume file is kept as the fallback before ``files remove-source`` may delete it.
DEFAULT_SOURCE_RETENTION_DAYS = 30
_ON = frozenset({"1", "on", "true", "yes"})
_OFF = frozenset({"", "0", "off", "false", "no"})


def modules(config: AxentraConfig) -> tuple[str, ...]:
    """The Axentra modules this server switches on: :attr:`AxentraConfig.modules` (``storage`` with the Axentra
    file backend), plus ``notify`` with ``NODESTRO_AXENTRA_NOTIFY``
    (Nodestro uses only its in-app inbox channel; Axentra's provider stays ``local`` unless its own config file says
    otherwise, and Nodestro creates no e-mail templates)."""
    base = config.modules
    return (*base, "notify") if config.notify else base


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
    #: Mirror audit events (tool approvals, MCP) into Axentra's audit log.
    audit: bool = False
    #: Mirror the AI usage ledger into Axentra's usage meters.
    usage: bool = False
    #: Also mirror the settled usage of finished workflow runs (needs ``usage``).
    run_usage: bool = False
    #: In-app inbox notifications through Axentra's notify module (``NODESTRO_AXENTRA_NOTIFY``).
    notify: bool = False
    #: Where identity records live (``NODESTRO_IDENTITY_BACKEND``, see :mod:`nodestro_runtime.axentra_identity`).
    identity_backend: IdentityBackend = "nodestro"
    #: Where input file bytes live (``NODESTRO_FILES_BACKEND``, see :mod:`nodestro_runtime.axentra_storage`).
    files_backend: FilesBackend = "volume"
    #: ``NODESTRO_FILES_SOURCE_REMOVAL=1``: ``files remove-source`` may delete migrated volume files (default off).
    files_source_removal: bool = False
    #: ``NODESTRO_FILES_SOURCE_RETENTION_DAYS``: the fallback period before a migrated volume file may be removed.
    files_source_retention_days: int = DEFAULT_SOURCE_RETENTION_DAYS

    @property
    def modules(self) -> tuple[str, ...]:
        """The Axentra modules this configuration uses (``storage`` only for ``NODESTRO_FILES_BACKEND=axentra``)."""
        return MODULES + (("storage",) if self.files_backend == "axentra" else ())

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
        backend = environ.get("NODESTRO_IDENTITY_BACKEND", "").strip().lower() or "nodestro"
        if backend not in IDENTITY_BACKENDS:
            problem(
                "NODESTRO_IDENTITY_BACKEND", backend, "NODESTRO_IDENTITY_BACKEND must be nodestro, mirror or axentra"
            )
            backend = "nodestro"
        files = environ.get("NODESTRO_FILES_BACKEND", "").strip().lower() or "volume"
        if files not in FILES_BACKENDS:
            problem("NODESTRO_FILES_BACKEND", files, "NODESTRO_FILES_BACKEND must be volume or axentra")
            files = "volume"
        removal = environ.get("NODESTRO_FILES_SOURCE_REMOVAL", "").strip().lower()
        if removal not in _ON | _OFF:
            problem("NODESTRO_FILES_SOURCE_REMOVAL", removal, "NODESTRO_FILES_SOURCE_REMOVAL must be 1 or 0")
        retention_text = environ.get("NODESTRO_FILES_SOURCE_RETENTION_DAYS", "").strip()
        retention = DEFAULT_SOURCE_RETENTION_DAYS
        if retention_text:
            try:
                retention = int(retention_text)
                if retention < 0:
                    raise ValueError
            except ValueError:
                problem(
                    "NODESTRO_FILES_SOURCE_RETENTION_DAYS",
                    retention_text,
                    "NODESTRO_FILES_SOURCE_RETENTION_DAYS must be a whole number of days (0 or more)",
                )
                retention = DEFAULT_SOURCE_RETENTION_DAYS
        superuser = environ.get("NODESTRO_AXENTRA_ALLOW_SUPERUSER", "").strip().lower()
        if superuser not in _ON | _OFF:
            problem("NODESTRO_AXENTRA_ALLOW_SUPERUSER", superuser, "NODESTRO_AXENTRA_ALLOW_SUPERUSER must be 1 or 0")
        mirrors: dict[str, bool] = {}
        for name in ("audit", "usage", "run_usage", "notify"):
            variable = f"NODESTRO_AXENTRA_{name.upper()}"
            value = environ.get(variable, "").strip().lower()
            if value not in _ON | _OFF:
                problem(variable, value, f"{variable} must be 1 or 0")
            elif value in _ON and not enabled:
                problem(variable, value, f"{variable}=1 needs NODESTRO_AXENTRA=on")
            mirrors[name] = value in _ON
        if mirrors["run_usage"] and not mirrors["usage"]:
            problem("NODESTRO_AXENTRA_RUN_USAGE", "1", "NODESTRO_AXENTRA_RUN_USAGE=1 needs NODESTRO_AXENTRA_USAGE=1")
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
            if backend != "nodestro":
                problem(
                    "NODESTRO_IDENTITY_BACKEND",
                    backend,
                    f"NODESTRO_IDENTITY_BACKEND={backend} needs NODESTRO_AXENTRA=on",
                )
            if files != "volume":
                problem("NODESTRO_FILES_BACKEND", files, f"NODESTRO_FILES_BACKEND={files} needs NODESTRO_AXENTRA=on")
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
            audit=mirrors["audit"],
            usage=mirrors["usage"],
            run_usage=mirrors["run_usage"],
            notify=mirrors["notify"],
            identity_backend=backend,  # type: ignore[arg-type]
            files_backend=files,  # type: ignore[arg-type]
            files_source_removal=removal in _ON,
            files_source_retention_days=retention,
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
        "modules": list(modules(config)),
        "product": {"slug": PRODUCT_SLUG, "name": PRODUCT_NAME},
        "database": {"url": url, "allow_superuser": config.allow_superuser},
        # The keyring is not a setting here: Nodestro validated it and hands it to Axentra (AxentraServices).
        "security": {"public_url": public_origin, "master_keys_ref": None, "master_keys": None},
        "observability": {"otel_service_name": "nodestro"},
        # Nodestro sends only in-app inbox notifications: no delivery provider that leaves the process (an e-mail
        # channel would be a separate, explicit switch; see docs/AXENTRA.md, "Notifications").
        "notify": {"provider": "local"},
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
                plaintext = str(self.box.decrypt(token, secret_context(tenant_id, name)))
            except Exception:  # unknown key id, tampered value, other tenant or field
                count_operation("secret_decrypt", "unreadable", format="axs1")
                raise NodestroError(
                    "secrets.unreadable",
                    "A stored secret cannot be decrypted with this server's keyring; enter it again",
                ) from None
            count_operation("secret_decrypt", "ok", format="axs1")
            return plaintext
        if self.legacy is None:
            count_operation("secret_decrypt", "unreadable", format="v1")
            raise NodestroError(
                "secrets.unreadable", "A stored secret uses the retired v1 format; this server no longer reads it"
            )
        try:
            plaintext = self.legacy.decrypt(tenant_id, name, token)
        except NodestroError:
            count_operation("secret_decrypt", "unreadable", format="v1")
            raise
        count_operation("secret_decrypt", "ok", format="v1")
        return plaintext

    def needs_rewrite(self, token: str) -> bool:
        """Whether ``token`` is not in the format and key this box writes.

        Writing ``v1`` (the rollback of ``NODESTRO_SECRETS_FORMAT=axs1``): every ``axs1`` value needs a rewrite, so
        ``secrets reencrypt`` moves them back to v1 before the keyring may go."""
        if self.write == "v1":
            return isinstance(token, str) and token.startswith(AXS1_PREFIX)
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
    """Move every stored tenant secret to the format and key ``box`` writes (``nodestro-server secrets reencrypt``):
    the active Axentra key with ``NODESTRO_SECRETS_FORMAT=axs1``, or back to v1 with ``v1`` (the rollback).

    Guarantees: each document is re-encrypted in its own transaction with the row locked (``FOR UPDATE``), so a
    crash loses nothing and a rerun continues; only values not under the active key are touched (idempotent);
    a value that cannot be decrypted is **left exactly as it is** and reported (by location, never by content);
    plain-text values (which a server never keeps) are reported, not encrypted. ``dry_run`` only counts.
    """
    from .storage_postgres import _encode

    report = ReencryptReport(dry_run=dry_run, active_kid=box.active_kid if box.write == "axs1" else "v1")
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


def stored_secret_formats(database: PostgresDatabase) -> dict[str, int]:
    """How many stored tenant secrets use which format: ``v1``, ``axs1:<kid>`` and ``plain`` (read-only).

    Cheap enough for every start: only the few documents of :data:`SECRET_FIELDS` that contain an encrypted value
    are read (one per tenant for the settings, one per MCP server or billing key), nothing is decrypted."""
    from .server_settings import ENCRYPTED_PREFIX

    counts: dict[str, int] = {}
    by_collection = {item.collection: item for item in SECRET_FIELDS}
    rows = database._query(
        "SELECT collection, id, coalesce(raw, data::text) FROM nodestro_documents WHERE collection = ANY(%s)"
        " AND (strpos(data::text, %s) > 0 OR strpos(data::text, %s) > 0)",
        ([item.collection for item in SECRET_FIELDS], '"' + AXS1_PREFIX, '"' + ENCRYPTED_PREFIX),
    )
    for collection, key, text in rows:
        data = json.loads(text)
        if not isinstance(data, dict):
            continue
        for path, _name in by_collection[collection].fields(str(key), data):
            token = _get(data, path)
            if isinstance(token, str) and token:
                kind = _format_of(token)
                counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def secrets_rollback_problems(config: AxentraConfig, formats: Mapping[str, int]) -> list[dict[str, str]]:
    """Why this configuration cannot read every stored tenant secret (empty: it can), as ``profile.invalid`` problems.

    The start refuses (instead of reading values as ``secrets.unreadable`` and inviting people to enter them again):
    ``NODESTRO_AXENTRA=off`` while ``axs1`` values exist (the keyring is only read with ``on``), and a keyring that
    lacks a key id still in use (a key removed before ``secrets reencrypt`` reported ``pending: 0``). v1 values are
    always readable: ``NODESTRO_SECRET_KEY`` is required in every configuration."""
    in_use = {kind.split(":", 1)[1]: count for kind, count in formats.items() if kind.startswith("axs1:")}
    if not in_use:
        return []
    total = sum(in_use.values())
    if not config.enabled:
        return [
            {
                "field": "NODESTRO_AXENTRA",
                "value": "off",
                "reason": f"{total} stored tenant secrets use Axentra's keyring (axs1) and cannot be read with "
                "NODESTRO_AXENTRA=off; keep NODESTRO_AXENTRA=on with the keyring, or set NODESTRO_SECRETS_FORMAT=v1 "
                "and run 'nodestro-server secrets reencrypt' first (it moves them back to v1)",
            }
        ]
    from axentra.config.secrets import Keyring

    try:
        kids = set(Keyring.parse(config.keyring).kids)
    except Exception:  # already reported by AxentraConfig.from_env
        return []
    missing = sorted(set(in_use) - kids)
    if not missing:
        return []
    return [
        {
            "field": "NODESTRO_AXENTRA_KEYRING",
            "value": ",".join(missing),
            "reason": "NODESTRO_AXENTRA_KEYRING lacks key ids still used by stored tenant secrets: "
            + ", ".join(f"{kid} ({in_use[kid]} values)" for kid in missing)
            + "; add the old key again (behind the active one) and remove it only after 'secrets reencrypt' "
            "reports pending: 0",
        }
    ]


def require_readable_secrets(database: PostgresDatabase, config: AxentraConfig) -> None:
    """Refuse to serve (``profile.invalid``) when the configuration cannot read every stored tenant secret
    (:func:`secrets_rollback_problems`); ``check``, ``api`` and ``worker`` call it after the schema check."""
    problems = secrets_rollback_problems(config, stored_secret_formats(database))
    if problems:
        raise NodestroError(
            "profile.invalid",
            "The server cannot start: " + "; ".join(item["reason"] for item in problems),
            problems=problems,
        )


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
            # Only committed links are cached: inside a transaction the link may still roll back.
            if found is not None and not self.database.in_transaction:
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
                from axentra.data.database import DbScope

                base = tenant_slug(nodestro_id)
                # axentra_tenants has forced row-level security: read it in Axentra's platform scope (a bare
                # connection without a scope sees no rows unless its role bypasses RLS).
                with self.services.platform.db.transaction(DbScope.platform()) as tx:
                    taken = {
                        str(item["slug"])
                        for item in tx.all(
                            "SELECT slug FROM axentra_tenants WHERE product_id = %s AND (slug = %s OR slug LIKE %s)",
                            (product_id, base, base[:60] + "-%"),
                        )
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


# ---- audit and usage mirrors (M3) ---------------------------------------------------------------

#: The Axentra meters the usage mirror writes (key, unit, what a quantity of the meter is).
USAGE_METERS: tuple[tuple[str, str, str], ...] = (
    ("nodestro.ai.requests", "requests", "AI requests (usage ledger; workflow runs when run usage is mirrored)"),
    ("nodestro.ai.input_tokens", "tokens", "Input tokens of AI requests"),
    ("nodestro.ai.output_tokens", "tokens", "Output tokens of AI requests"),
    ("nodestro.ai.cost_usd", "USD", "Fully priced cost of AI requests (an incomplete cost is never mirrored)"),
)
#: Run states whose usage is final: a continued run is a new run (``resumed_from``), so a finished run never changes.
SETTLED_RUN_STATES = ("succeeded", "failed", "cancelled")
#: How far back the worker's periodic settlement looks after a start (older runs: ``axentra reconcile``).
RUN_SETTLE_LOOKBACK_DAYS = 7
#: How far back Axentra accepts usage (``axentra.usage.service.MAX_BACKDATE`` is 400 days), with a margin.
USAGE_RECONCILE_DAYS = 395
_MICRO = 10**6


def usage_quantities(usage: Any) -> dict[str, Any]:
    """The meter quantities of one usage entry's :class:`~nodestro_runtime.schemas.Usage` (zeros left out).

    The cost is mirrored only when it is complete (``estimated_cost_usd`` known and not marked incomplete), rounded
    to micro-dollars: an unknown or partial cost is never billed as if it were the whole cost."""
    from decimal import ROUND_HALF_UP, Decimal

    found: dict[str, Any] = {}
    for meter, value in (
        ("nodestro.ai.requests", usage.requests),
        ("nodestro.ai.input_tokens", usage.input_tokens),
        ("nodestro.ai.output_tokens", usage.output_tokens),
    ):
        if value:
            found[meter] = int(value)
    cost = usage.estimated_cost_usd
    if cost is not None and usage.cost_complete is not False and cost > 0:
        found["nodestro.ai.cost_usd"] = Decimal(str(cost)).quantize(Decimal(1) / _MICRO, rounding=ROUND_HALF_UP)
    return found


def usage_key(entry_id: str, meter: str) -> str:
    """The idempotency key of one mirrored quantity: replaying it never counts twice."""
    return f"nodestro:usage:{entry_id}:{meter}"


def run_usage_key(run_id: str, meter: str) -> str:
    """The idempotency key of a finished run's settled usage of one meter: one event per run and meter, ever.

    A run's usage is final once it is finished (:data:`SETTLED_RUN_STATES`; continuing a run creates a new run whose
    reused steps count no cost), so a retry, a crash after the write, a second worker or a second reconcile replays
    the same key with the same quantity (Axentra returns the stored event and adds nothing). A finished run whose
    usage changed anyway is refused by Axentra (same key, other request) and reported, never counted again."""
    return f"nodestro:run:{run_id}:{meter}"


@dataclass(frozen=True)
class SettledRun:
    """What the run usage mirror reads of a finished run (the stored run document stays the source of truth)."""

    id: str
    status: str
    trigger: str
    workflow_id: str
    finished_at: Any
    usage: Any

    def properties(self) -> dict[str, Any]:
        """Deterministic properties of the run's usage events (part of the replayed request: never time-dependent)."""
        return {
            "source": "api_run" if self.trigger == "api" else "workflow_run",
            "status": self.status,
            "workflow_id": self.workflow_id,
        }


def settled_runs(database: PostgresDatabase, tenant_id: str, *, since: Any = None) -> list[SettledRun]:
    """The finished, top-level, not imported runs of a tenant (``since``: changed at or after it), oldest first.

    Reads only the run's status, trigger, workflow, end and usage (``jsonb`` fields, not the whole document), through
    the ``(tenant, collection, updated_at)`` index when ``since`` is given."""
    from datetime import datetime

    from .schemas import Usage

    where = (
        "tenant_id = %s AND collection = 'runs' AND data ->> 'status' = ANY(%s)"
        " AND data ->> 'parent_run_id' IS NULL AND data ->> 'imported_at' IS NULL"
    )
    params: tuple[Any, ...] = (tenant_id, list(SETTLED_RUN_STATES))
    if since is not None:
        where += " AND updated_at >= %s"
        params += (since,)
    rows = database._query(
        "SELECT id, data ->> 'status', coalesce(data ->> 'trigger', 'manual'), coalesce(data ->> 'workflow_id', ''),"
        " data ->> 'finished_at', updated_at, data -> 'usage' FROM nodestro_documents"
        f" WHERE {where} ORDER BY updated_at, id",
        params,
    )
    found = []
    for run_id, status, trigger, workflow_id, finished, updated, usage in rows:
        at = datetime.fromisoformat(finished.replace("Z", "+00:00")) if finished else updated
        found.append(
            SettledRun(
                id=str(run_id),
                status=str(status),
                trigger=str(trigger),
                workflow_id=str(workflow_id),
                finished_at=at,
                usage=Usage.model_validate(usage or {}),
            )
        )
    return found


@dataclass
class MirrorStats:
    """What the mirror did since the process started (``failed``: kept in Nodestro, written by reconcile)."""

    audit: int = 0
    usage: int = 0
    runs: int = 0
    skipped: int = 0
    failed: int = 0
    #: Finished runs whose stored usage differs from what was mirrored (never counted twice; see run_usage_key).
    changed: int = 0


class AxentraEvents:
    """The :class:`~nodestro_runtime.platform_events.PlatformEvents` sink of a server with Axentra switched on.

    Called inside the Nodestro transaction that stores the record: Axentra's services are joined to that transaction
    (:meth:`AxentraLink.joined`), and each mirror runs in its own savepoint. A failing mirror rolls back only its
    savepoint and is logged (``stats.failed``); the Nodestro record and the rest of the caller's transaction are kept,
    and :meth:`reconcile` writes what is missing later. Tenants without a ``nodestro_tenants`` row (the price
    reference pseudo tenant, ...) are not mirrored."""

    def __init__(self, link: AxentraLink, *, audit: bool, usage: bool, run_usage: bool = False) -> None:
        self.link = link
        self.audit_enabled = audit
        self.usage_enabled = usage
        self.run_usage_enabled = usage and run_usage
        self.stats = MirrorStats()
        self._meters_ready = False
        #: Where the periodic run settlement continues (``updated_at`` of the runs; ``None``: the lookback).
        self._settled_until: Any = None

    # -- set-up ------------------------------------------------------------------------------------

    def ensure_meters(self) -> list[str]:
        """Create the ``nodestro.ai.*`` meters that are missing (idempotent, safe across processes)."""
        from axentra.core.context import Principal
        from axentra.core.errors import Conflict

        platform = self.link.services.platform
        product_id = self.link.services.product_id
        system = Principal.system("nodestro")
        existing = {meter.key for meter in platform.usage.list_meters(system, product_id)}
        created = []
        for key, unit, description in USAGE_METERS:
            if key in existing:
                continue
            try:
                platform.usage.create_meter(
                    system, product_id, key=key, name=key, unit=unit, description=description, reset_period="month"
                )
                created.append(key)
            except Conflict:  # another process created it first
                pass
        self._meters_ready = True
        return created

    # -- the sink ------------------------------------------------------------------------------------

    def audit(
        self,
        tenant_id: str,
        *,
        event_id: str,
        at: Any,
        action: str,
        actor: str | None,
        target_type: str,
        target_id: str,
        outcome: str,
        details: dict[str, Any],
    ) -> None:
        if not self.audit_enabled:
            return
        self._guarded(
            "audit",
            event_id,
            lambda tx, ax_tenant: self._audit(
                tx,
                ax_tenant,
                action,
                actor,
                target_type,
                target_id,
                outcome,
                {**details, "nodestro_at": at.isoformat()},
            ),
            tenant_id,
        )

    def usage(self, tenant_id: str, entry: Any) -> None:
        if not self.usage_enabled:
            return
        self._guarded("usage", entry.id, lambda tx, ax_tenant: self._usage(tx, ax_tenant, entry), tenant_id)

    # -- internals -----------------------------------------------------------------------------------

    def _tenant(self, tenant_id: str) -> str | None:
        found = self.link.tenant(tenant_id)
        if found is not None:
            return found
        rows = self.link.database._query("SELECT name FROM nodestro_tenants WHERE id = %s", (tenant_id,))
        if not rows:
            return None
        return self.link.ensure_tenant(tenant_id, str(rows[0][0]))

    def _guarded(self, what: str, record_id: str, write: Callable[[Any, str], None], tenant_id: str) -> str:
        """Run ``write`` in a savepoint; the outcome: ``ok``, ``skipped`` (no tenant), ``changed`` (Axentra refused a
        replayed key with another quantity) or ``failed`` (logged; reconcile writes it later)."""
        from axentra.core.errors import Conflict
        from axentra.data.database import DbScope, join

        try:
            # A savepoint: a failure (also while creating the Axentra tenant) rolls back the mirror only.
            with self.link.joined() as connection, connection.transaction():
                ax_tenant = self._tenant(tenant_id) if tenant_id else None
                if ax_tenant is None:
                    self.stats.skipped += 1
                    count_operation("mirror_write", "skipped", kind=what)
                    return "skipped"
                with join(connection, DbScope.tenant(self.link.services.product_id, ax_tenant)) as tx:
                    write(tx, ax_tenant)
        except Conflict:
            self.stats.changed += 1
            count_operation("mirror_write", "changed", kind=what)
            logger.warning(
                "The %s record %s of tenant %s changed after it was mirrored; it is not counted again",
                what,
                record_id,
                tenant_id,
            )
            return "changed"
        except Exception:
            self.stats.failed += 1
            count_operation("mirror_write", "failed", kind=what)
            logger.exception("Could not mirror %s record %s of tenant %s into Axentra", what, record_id, tenant_id)
            return "failed"
        count_operation("mirror_write", "ok", kind=what)
        if what == "audit":
            self.stats.audit += 1
        elif what == "run_usage":
            self.stats.runs += 1
        else:
            self.stats.usage += 1
        return "ok"

    def _actor(self, actor: str | None) -> tuple[str, str, str]:
        if actor:
            user = self.link.lookup("user", actor)
            if user is not None:
                return ("user", user, actor)
        return ("system", "nodestro", actor or "system")

    def _audit(
        self,
        tx: Any,
        ax_tenant: str,
        action: str,
        actor: str | None,
        target_type: str,
        target_id: str,
        outcome: str,
        details: dict[str, Any],
    ) -> None:
        audit_log: Any = self.link.services.platform.audit  # typed loosely: Axentra is optional (3.10)
        audit_log.record(
            tx,
            None,
            action,
            target_type=target_type,
            target_id=target_id,
            product_id=self.link.services.product_id,
            tenant_id=ax_tenant,
            outcome=outcome,
            details=details,
            actor_override=self._actor(actor),
        )

    def _usage(self, tx: Any, ax_tenant: str, entry: Any) -> None:
        from axentra.core.context import Principal

        if not self._meters_ready:
            raise RuntimeError("the nodestro.ai.* meters are not set up (ensure_meters)")
        platform = self.link.services.platform
        properties = {"source": entry.source, "model": entry.model, "estimated": bool(entry.estimated)}
        for meter, quantity in usage_quantities(entry.usage).items():
            platform.usage.record_in(
                tx,
                Principal.system("nodestro"),
                self.link.services.product_id,
                ax_tenant,
                meter,
                quantity,
                usage_key(entry.id, meter),
                occurred_at=entry.at,
                properties=properties,
            )

    # -- settled usage of finished workflow runs ---------------------------------------------------------

    def _run_usage(self, tx: Any, ax_tenant: str, run: SettledRun) -> None:
        from axentra.core.context import Principal

        if not self._meters_ready:
            raise RuntimeError("the nodestro.ai.* meters are not set up (ensure_meters)")
        platform = self.link.services.platform
        for meter, quantity in usage_quantities(run.usage).items():
            platform.usage.record_in(
                tx,
                Principal.system("nodestro"),
                self.link.services.product_id,
                ax_tenant,
                meter,
                quantity,
                run_usage_key(run.id, meter),
                occurred_at=run.finished_at,
                properties=run.properties(),
            )

    def settle_run(self, tenant_id: str, run: SettledRun) -> str:
        """Mirror the settled usage of one finished run (idempotent; see :func:`run_usage_key`); the outcome."""
        store = self.link.database.store(tenant_id)
        with store.transaction():
            return self._guarded(
                "run_usage", run.id, lambda tx, ax_tenant: self._run_usage(tx, ax_tenant, run), tenant_id
            )

    def settle_runs(
        self, *, tenant: str | None = None, since: Any = None, dry_run: bool = False, full: bool = False
    ) -> dict[str, Any]:
        """Mirror the settled usage of every finished top-level run that is not mirrored yet.

        The worker calls it periodically (``since`` = where the last pass stopped; the first pass looks
        :data:`RUN_SETTLE_LOOKBACK_DAYS` back); ``axentra reconcile`` calls it with ``full=True`` (every run Axentra
        still accepts, :data:`USAGE_RECONCILE_DAYS`). Same filter as the usage dashboard: top-level runs only (a
        sub-workflow's cost is counted in its parent run) and never imported runs. Nothing is written twice:
        already mirrored runs are skipped, and a replay is a no-op (:func:`run_usage_key`)."""
        from datetime import datetime, timedelta, timezone

        report = {"runs": 0, "missing": 0, "written": 0, "changed": 0, "too_old": 0, "failed": 0, "skipped": 0}
        if not self.run_usage_enabled:
            return report
        if not dry_run and not self._meters_ready:
            self.ensure_meters()
        started = datetime.now(timezone.utc)
        oldest = started - timedelta(days=USAGE_RECONCILE_DAYS)
        if full:
            since = None
        elif since is None:
            since = self._settled_until or started - timedelta(days=RUN_SETTLE_LOOKBACK_DAYS)
        params: tuple[Any, ...] = ()
        query = "SELECT id FROM nodestro_tenants"
        if tenant is not None:
            query += " WHERE id = %s"
            params = (tenant,)
        for (tenant_id,) in self.link.database._query(query + " ORDER BY id", params):
            tenant_id = str(tenant_id)
            recorded = self._mirrored_runs(tenant_id)
            for run in settled_runs(self.link.database, tenant_id, since=since):
                report["runs"] += 1
                wanted = usage_quantities(run.usage)
                if not wanted:
                    continue
                mirrored = recorded.get(run.id, {})
                if set(wanted) <= set(mirrored):
                    if any(Decimal(str(wanted[meter])) != mirrored[meter] for meter in wanted):
                        # Settled with other quantities: reported, never counted again (see run_usage_key).
                        report["changed"] += 1
                        self.stats.changed += 1
                        count_operation("mirror_write", "changed", kind="run_usage")
                    continue
                if run.finished_at < oldest:
                    report["too_old"] += 1
                    continue
                report["missing"] += 1
                if dry_run:
                    continue
                outcome = self.settle_run(tenant_id, run)
                key = {"ok": "written", "changed": "changed", "skipped": "skipped"}.get(outcome, "failed")
                report[key] += 1
        if not full and tenant is None and not dry_run and report["failed"] == 0:
            # The next pass starts a little before this one did: a run committed while this pass read is not missed.
            self._settled_until = started - timedelta(minutes=5)
        return report

    def _mirrored_runs(self, tenant_id: str) -> dict[str, dict[str, Decimal]]:
        ax_tenant = self.link.tenant(tenant_id)
        if ax_tenant is None:
            return {}
        from axentra.data.database import DbScope, join

        with (
            self.link.joined() as connection,
            join(connection, DbScope.tenant(self.link.services.product_id, ax_tenant)) as tx,
        ):
            rows = tx.all(
                "SELECT idempotency_key, quantity FROM axentra_usage_events"
                " WHERE product_id = %s AND tenant_id = %s AND idempotency_key LIKE 'nodestro:run:%%'",
                (self.link.services.product_id, ax_tenant),
            )
        found: dict[str, dict[str, Decimal]] = {}
        for row in rows:
            _, _, run_id, meter = str(row["idempotency_key"]).split(":", 3)
            found.setdefault(run_id, {})[meter] = Decimal(row["quantity"])
        return found

    # -- reconciliation ------------------------------------------------------------------------------

    def reconcile(self, *, dry_run: bool = False, tenant: str | None = None) -> dict[str, Any]:
        """Write the mirrors that are missing (a failed mirror, records from before the switch was on).

        Idempotent and resumable: usage replays its idempotency keys (nothing is counted twice; entries older than
        :data:`USAGE_RECONCILE_DAYS` are reported, not written, because Axentra refuses them); an audit event is
        written only when no Axentra event carries its ``nodestro_event`` id. Nodestro's records are only read."""
        from datetime import datetime, timedelta, timezone

        from .tool_approvals import AUDIT, ToolApprovalEvent, audit_details
        from .usage_log import UsageLog

        report: dict[str, Any] = {
            "dry_run": dry_run,
            "tenants": 0,
            "audit_missing": 0,
            "usage_missing": 0,
            "usage_too_old": 0,
            "runs_missing": 0,
            "runs_changed": 0,
            "written": 0,
            "failed": 0,
        }
        if self.usage_enabled and not dry_run and not self._meters_ready:
            self.ensure_meters()
        oldest = datetime.now(timezone.utc) - timedelta(days=USAGE_RECONCILE_DAYS)
        params: tuple[Any, ...] = ()
        query = "SELECT id FROM nodestro_tenants"
        if tenant is not None:
            query += " WHERE id = %s"
            params = (tenant,)
        for (tenant_id,) in self.link.database._query(query + " ORDER BY id", params):
            tenant_id = str(tenant_id)
            report["tenants"] += 1
            store = self.link.database.store(tenant_id)
            failed_before = self.stats.failed
            if self.audit_enabled:
                known = self._mirrored_events(tenant_id)
                for _, event in store.items(AUDIT, ToolApprovalEvent):
                    if event.id in known:
                        continue
                    report["audit_missing"] += 1
                    if not dry_run:
                        with store.transaction():
                            self.audit(
                                tenant_id,
                                event_id=event.id,
                                at=event.at,
                                action=f"nodestro.tool_approval.{event.kind}",
                                actor=event.actor,
                                target_type="run" if event.run_id else "tool_pre_grant",
                                target_id=event.run_id or event.pre_grant_id or "",
                                outcome="denied" if event.kind == "decision_refused" else "success",
                                details=audit_details(event),
                            )
                # MCP events are read as stored documents: the core never imports the MCP client (see
                # tests/test_mcp_architecture.py); addresses keep only scheme and host here.
                for data in self._documents(tenant_id, "mcp_events"):
                    event_id = str(data.get("id", ""))
                    if not event_id or event_id in known:
                        continue
                    report["audit_missing"] += 1
                    if not dry_run:
                        with store.transaction():
                            self.audit(
                                tenant_id,
                                event_id=event_id,
                                at=datetime.fromisoformat(str(data["at"])),
                                action=f"nodestro.mcp.{data.get('kind', 'event')}",
                                actor=data.get("actor"),
                                target_type="mcp_server" if data.get("server_id") else "",
                                target_id=str(data.get("server_id") or ""),
                                outcome="success",
                                details=_stored_mcp_details(data),
                            )
            if self.usage_enabled:
                recorded = self._mirrored_usage(tenant_id)
                for entry in UsageLog(store).entries():
                    wanted = set(usage_quantities(entry.usage))
                    if wanted <= recorded.get(entry.id, set()):
                        continue
                    if entry.at < oldest:
                        report["usage_too_old"] += 1
                        continue
                    report["usage_missing"] += 1
                    if not dry_run:
                        with store.transaction():
                            self.usage(tenant_id, entry)
            report["failed"] += self.stats.failed - failed_before
        written = report["audit_missing"] + report["usage_missing"]
        report["written"] = 0 if dry_run else written - report["failed"]
        if self.run_usage_enabled:
            runs = self.settle_runs(tenant=tenant, dry_run=dry_run, full=True)
            report["runs_missing"] = runs["missing"]
            report["runs_changed"] = runs["changed"]
            report["usage_too_old"] += runs["too_old"]
            report["failed"] += runs["failed"]
            report["written"] += runs["written"]
        return report

    def _documents(self, tenant_id: str, collection: str) -> list[dict[str, Any]]:
        rows = self.link.database._query(
            "SELECT data FROM nodestro_documents WHERE tenant_id = %s AND collection = %s AND data IS NOT NULL",
            (tenant_id, collection),
        )
        return [row[0] for row in rows if isinstance(row[0], dict)]

    def _mirrored_events(self, tenant_id: str) -> set[str]:
        ax_tenant = self.link.tenant(tenant_id)
        if ax_tenant is None:
            return set()
        from axentra.data.database import DbScope, join

        with (
            self.link.joined() as connection,
            join(connection, DbScope.tenant(self.link.services.product_id, ax_tenant)) as tx,
        ):
            rows = tx.all(
                "SELECT details->>'nodestro_event' AS event FROM axentra_audit_events"
                " WHERE product_id = %s AND tenant_id = %s AND action LIKE 'nodestro.%%'"
                " AND details ? 'nodestro_event'",
                (self.link.services.product_id, ax_tenant),
            )
        return {str(row["event"]) for row in rows}

    def _mirrored_usage(self, tenant_id: str) -> dict[str, set[str]]:
        ax_tenant = self.link.tenant(tenant_id)
        if ax_tenant is None:
            return {}
        from axentra.data.database import DbScope, join

        with (
            self.link.joined() as connection,
            join(connection, DbScope.tenant(self.link.services.product_id, ax_tenant)) as tx,
        ):
            rows = tx.all(
                "SELECT idempotency_key FROM axentra_usage_events"
                " WHERE product_id = %s AND tenant_id = %s AND idempotency_key LIKE 'nodestro:usage:%%'",
                (self.link.services.product_id, ax_tenant),
            )
        found: dict[str, set[str]] = {}
        for row in rows:
            _, _, entry_id, meter = str(row["idempotency_key"]).split(":", 3)
            found.setdefault(entry_id, set()).add(meter)
        return found


def _address_only(url: str) -> str:
    """``scheme://host[:port]`` of ``url``: an address stored before redaction never reaches the mirror whole."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "(invalid address)"
    return f"{parts.scheme}://{host}{port}" if parts.scheme and host else "(address)"


def _stored_mcp_details(data: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {"nodestro_event": data.get("id"), "nodestro_actor": data.get("actor")}
    if data.get("tool") is not None:
        details["tool"] = data["tool"]
    for key, value in (data.get("details") or {}).items():
        details[key] = _address_only(value) if key == "url" and isinstance(value, str) else value
    return details


_BACKLOG_SQL = {
    # Ledger entries without their mirrored request count (every entry with requests has that meter).
    "usage": """
        SELECT count(*) FROM nodestro_documents d
        JOIN nodestro_tenants t ON t.id = d.tenant_id
        LEFT JOIN nodestro_axentra_ids l ON l.kind = 'tenant' AND l.nodestro_id = d.tenant_id
        WHERE d.collection = 'usage_log' AND coalesce((d.data -> 'usage' ->> 'requests')::bigint, 0) > 0
          AND (d.data ->> 'at')::timestamptz >= now() - make_interval(days => %(days)s)
          AND NOT EXISTS (SELECT 1 FROM axentra_usage_events e WHERE e.product_id = %(product)s
                AND e.tenant_id = l.axentra_id
                AND e.idempotency_key = 'nodestro:usage:' || d.id || ':nodestro.ai.requests')""",
    # Finished top-level runs with AI requests whose settled usage is not mirrored.
    "runs": """
        SELECT count(*) FROM nodestro_documents d
        JOIN nodestro_tenants t ON t.id = d.tenant_id
        LEFT JOIN nodestro_axentra_ids l ON l.kind = 'tenant' AND l.nodestro_id = d.tenant_id
        WHERE d.collection = 'runs' AND d.data ->> 'status' IN ('succeeded', 'failed', 'cancelled')
          AND d.data ->> 'parent_run_id' IS NULL AND d.data ->> 'imported_at' IS NULL
          AND coalesce((d.data -> 'usage' ->> 'requests')::bigint, 0) > 0
          AND d.updated_at >= now() - make_interval(days => %(days)s)
          AND NOT EXISTS (SELECT 1 FROM axentra_usage_events e WHERE e.product_id = %(product)s
                AND e.tenant_id = l.axentra_id
                AND e.idempotency_key = 'nodestro:run:' || d.id || ':nodestro.ai.requests')""",
    # Approval audit events and MCP events without a mirrored audit event.
    "audit": """
        SELECT count(*) FROM nodestro_documents d
        JOIN nodestro_tenants t ON t.id = d.tenant_id
        LEFT JOIN nodestro_axentra_ids l ON l.kind = 'tenant' AND l.nodestro_id = d.tenant_id
        WHERE d.collection IN ('tool_approval_audit', 'mcp_events')
          AND NOT EXISTS (SELECT 1 FROM axentra_audit_events a WHERE a.product_id = %(product)s
                AND a.tenant_id = l.axentra_id AND a.details ->> 'nodestro_event' = d.data ->> 'id')""",
}


def reconciliation_backlog(services: AxentraServices) -> dict[str, int]:
    """How many Nodestro records ``axentra reconcile`` would mirror right now, by kind (``usage``, ``runs``,
    ``audit``; read-only, all tenants, counts only).

    Approximate on purpose (cheap enough for ``axentra status`` and a cached metrics gauge): usage and runs are
    matched by their request-count key, audit events by ``details.nodestro_event``; records older than
    :data:`USAGE_RECONCILE_DAYS` are not counted (Axentra refuses them). Every kind is counted whether its mirror is
    switched on or not, so the numbers also tell what switching it on would have to catch up."""
    from axentra.data.database import DbScope

    params = {"product": services.product_id, "days": USAGE_RECONCILE_DAYS}
    with services.platform.db.transaction(DbScope.platform(), read_only=True) as tx:
        return {kind: int(tx.scalar(sql, params) or 0) for kind, sql in _BACKLOG_SQL.items()}


def attach_platform_events(database: PostgresDatabase, link: AxentraLink) -> AxentraEvents | None:
    """Attach the audit/usage mirror to ``database`` when ``NODESTRO_AXENTRA_AUDIT`` or ``..._USAGE`` is on."""
    config = link.services.config
    if not (config.audit or config.usage):
        return None
    events = AxentraEvents(link, audit=config.audit, usage=config.usage, run_usage=config.run_usage)
    if config.usage:
        events.ensure_meters()
    database.platform_events = events
    return events


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
    "IDENTITY_BACKENDS",
    "MODULES",
    "PRODUCT_SLUG",
    "SECRET_FIELDS",
    "USAGE_METERS",
    "AxentraConfig",
    "AxentraEvents",
    "AxentraLink",
    "AxentraServices",
    "IdentityBackend",
    "KeyringSecretBox",
    "ReencryptReport",
    "SettledRun",
    "attach_platform_events",
    "available",
    "axentra_version",
    "build_settings",
    "from_environment",
    "keyring_secret_box",
    "modules",
    "reconciliation_backlog",
    "reencrypt_tenant_secrets",
    "require_readable_secrets",
    "run_usage_key",
    "secret_context",
    "secrets_rollback_problems",
    "settled_runs",
    "stored_secret_formats",
    "tenant_slug",
    "usage_key",
    "usage_quantities",
]
