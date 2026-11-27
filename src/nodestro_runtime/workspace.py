"""The workspace: one Nodestro data directory with every runtime service, usable from Python.

A :class:`Workspace` owns the database (``<data_dir>/nodestro.db``), the tool library, the
knowledge base, the memory spaces, the Python environments of tools, the input and result
files and the run manager (the one workflow engine). It reads the same settings and the same
data as the studio, so a script and a running Nodestro see the same workflows, agents,
providers and runs::

    from nodestro_runtime.workspace import Workspace
    from nodestro_runtime.schemas import RunIn

    async with Workspace("~/.nodestro") as workspace:
        workflow = workspace.workflow("wf_...")
        run = await workspace.run(workflow, RunIn(input="..."))
        finished = await workspace.wait(run.id)

No HTTP server and no interface are started. The HTTP API (``nodestro-api``) builds its
application context on top of this class and adds the studio services (architect,
assistant, publishing, automation); it never runs workflows in another way.

Provider access: a key from the environment wins over a stored one; stored keys are never
returned by any method meant for display (:meth:`settings`).

Lifecycle: runs execute as tasks of the event loop that started them. :meth:`aclose` (or
``async with``) cancels the runs of this process, waits until each has stored its final state
and then closes the database; :meth:`close` does the same without waiting. Paused runs stay
paused in the database and continue after the next start.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import logging
from pathlib import Path
import sys
import threading
from typing import Any, Literal

from orkexa import ConfigurationError
from orkexa.dynamic import Sandbox
from orkexa.embeddings import OllamaEmbeddings
from orkexa.local import OllamaError, check_model_name
from orkexa.providers.base import redact

from . import activity, docker_status, local_vault, output_limits, run_budget, sandbox_selection
from . import bedrock as bedrock_auth
from .agent_builder import AgentBuilder
from .agent_test import AgentTester
from .builtin_sandbox import LinuxSandbox
from .builtin_tools import BUILTIN_TOOLS
from .catalog import models
from .connections import ConnectionService
from .container_images import ContainerImages, RuntimeImageSandbox
from .conversations import Conversations
from .cost_tracking import PricedTracker, PriceMode
from .docker_status import DockerMonitorLike, DockerStatus
from .draft_tools import DRAFT_TOOLS
from .engine import EMBEDDING_PROVIDER, RunManager
from .extension_sandbox import EXTENSION_MODES, ExtensionSandbox
from .extensions import Extensions, Integration, discover, find_packages
from .folder_index import FILES as FOLDER_INDEX_FILES
from .folder_index import STATES as FOLDER_INDEX_STATES
from .folder_index import FolderIndex, FolderIndexer
from .folder_tools import FOLDER_TOOLS
from .folders import folders_enabled
from .input_files import FileCatalog, InputFiles
from .knowledge import FOLDER_SOURCE, KnowledgeBase, KnowledgeTables
from .launcher import launch
from .local_models import LOCAL_ASSISTANT_MODEL, LOCAL_DEFAULT_MODEL, LOCAL_EMBEDDING_MODEL, SelectionPreferences
from .local_service import LocalModelService, _loopback
from .mcp.hub import McpHub, McpLimits
from .mcp.models import SERVERS as MCP_SERVERS
from .mcp.models import McpServer, McpTool
from .mcp.netguard import EgressPolicy
from .mcp.service import McpService
from .mcp.vault import VAULT_KEY_ENV, TokenVault, vault_box_from_env
from .mcp_access import is_mcp_tool, unattended_mcp_enabled
from .memory_spaces import MemorySpaces, MemoryTables
from .notifications import NOTIFY_TOOLS, LocalInbox, RunNotifier, local_inbox_enabled
from .offline import OfflineScope, check_connection, check_provider
from .pricing import PricingService
from .problems import NodestroError
from .profile import RuntimeProfile
from .providers import (
    LOCAL_MAX_RETRIES,
    PROVIDERS,
    Connection,
    MissingApiKeyError,
    bedrock_auth_methods,
    bedrock_settings,
    check_base_url,
    check_capabilities,
    check_endpoint_allowed,
    check_limits,
    check_model,
    endpoint_client,
    env_key,
    local_request_timeout,
    provider_of,
    provider_status,
    qualify,
    verification_key,
)
from .python_envs import PythonEnvironments, installation_id, scope_of
from .python_tools import EnvironmentGate
from .reports import ResultFiles
from .resume import PublishedRuns
from .resume import resume as resume_run
from .sandbox_policy import SandboxPolicy
from .sandbox_selection import SandboxMode
from .schemas import (
    DEFAULT_AGENT_MODEL,
    DEFAULT_ASSISTANT_MODEL,
    LANGUAGES,
    AgentConfig,
    BuiltinSandboxStatus,
    Run,
    RunIn,
    RunTrigger,
    SandboxBackendStatus,
    Settings,
    SettingsIn,
    Workflow,
    WorkflowIn,
    keep_origin,
    now,
)
from .server_settings import TenantSecrets, is_encrypted
from .skills import Skills
from .storage import BeforeMigration, DocumentStore, SQLiteStore, Store, strip_copy
from .tool_adoption import audit_revocation
from .tool_approvals import (
    AccessRules,
    ToolApprovalEvent,
    ToolApprovals,
    ToolGate,
    ToolPreGrants,
    access_grants_enabled,
    approvals_enabled,
    checkpoints_enabled,
    max_paused,
    provenance_enabled,
    record_audit,
)
from .tool_drafts import ToolDrafts
from .tool_legacy import LegacyReport, keep_only_inherited, stamp_workflow
from .tool_library import RESERVED_NAMES, ToolLibrary, make_sandbox
from .tool_provenance import StoredProvenance
from .usage_log import UsageLog
from .versions import WorkflowVersion, WorkflowVersions

#: The settings fields of Amazon Bedrock (``providers.bedrock``); never credentials.
_BEDROCK_FIELDS = ("region", "auth", "profile_name", "data_residency")
#: The shortest time between two explicit provider checks of one workspace (they call AWS).
VERIFY_INTERVAL_SECONDS = 10.0

#: The database file inside the data directory (unchanged since Nodestro 0.1).
DATABASE = "nodestro.db"
#: Held while a workspace is open: a second process on the same data directory would mark the first
#: one's runs as interrupted, clean up its installations and run every schedule a second time.
LOCK_FILE = "nodestro.lock"


#: The installation's own bookkeeping (never the user's settings): which default models it started with.
INSTALLATION = "installation"
MODEL_DEFAULTS = "model_defaults"


def _sealed_digest(sealed: str | None, storage: str) -> str:
    """What changes when a connection's secret is set again, without the secret (RT-089d)."""
    return f"{storage}:{hashlib.sha256((sealed or '').encode()).hexdigest()[:16]}"


def _mcp_marker(server: McpServer, tool: McpTool) -> dict[str, Any]:
    """An MCP tool's server configuration and definition for a memory consent's digest (RT-089d): what decides where
    a call goes and what it does. Not its status, check times or tokens (they change while it is used)."""
    return {
        "server": server.id,
        "url": server.url,
        "transport": server.transport,
        "stdio": server.stdio.model_dump(mode="json") if server.stdio is not None else None,
        "auth": server.auth,
        "issuer": server.oauth.get("issuer"),
        "allow_loopback": server.allow_loopback,
        "tool": tool.name,
        "hash": tool.hash,
        "access": tool.access,
    }


def _retry_cap(value: Any) -> int:
    """The stored ``architect_retry_max_tokens``, else the default (also for an invalid stored value)."""
    if isinstance(value, int) and not isinstance(value, bool) and 1024 <= value <= 1_000_000:
        return value
    return output_limits.ARCHITECT_RETRY_DEFAULT


def _retrieval_mode(value: Any) -> Any:
    return value if value in ("fts", "vector", "hybrid") else "fts"


def _embedding_name(name: str) -> str:
    """An Ollama embedding model name (``ollama:`` prefix allowed); cloud models are refused."""
    bare = str(name).strip().removeprefix("ollama:")
    try:
        check_model_name(bare)
    except OllamaError as exc:
        raise NodestroError("config.embedding_model_invalid", str(exc), model=str(name)) from None
    return bare


class DataDirectoryLock:
    """An exclusive advisory lock on a data directory (released by :meth:`release` or when the process ends)."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / LOCK_FILE
        handle = open(self.path, "a+b")  # noqa: SIM115 - kept open while the lock is held
        try:
            _lock(handle.fileno())
        except OSError:
            handle.close()
            raise NodestroError(
                "workspace.in_use",
                f"The data directory {data_dir} is used by another Nodestro process; stop it first or choose "
                "another data directory",
                path=str(data_dir),
            ) from None
        self._handle: Any = handle

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()  # closing the file releases the lock
            self._handle = None


logger = logging.getLogger(__name__)


def _key_field(provider: str) -> str:
    """The name a provider key is bound to when encrypted (see :class:`~nodestro_runtime.server_settings.SecretBox`)."""
    return f"providers.{provider}.api_key"


def _lock(descriptor: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def backup_migrated_database(store: SQLiteStore, directory: Path, current: int, target: int) -> Path:
    """Copy the whole database to ``directory`` before migrating it from ``current`` to ``target``; the copy's path.

    The copy is written under a temporary name and renamed when complete, so a backup that exists is whole. It holds no
    granted folder's index (N-12 review L-b): the copy outlives a revocation, and the index is built again from the
    folder while its grant holds (:func:`~nodestro_runtime.storage.strip_copy`)."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target_path = directory / f"pre-migration-{current}-to-{target}-{stamp}.db"
    partial = target_path.with_suffix(".db.partial")
    try:
        store.snapshot(partial)
        strip_copy(partial, FOLDER_SOURCE, (FOLDER_INDEX_FILES, FOLDER_INDEX_STATES))
        partial.replace(target_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target_path


def strip_migration_copies(directory: Path) -> list[Path]:
    """Remove every granted folder's index from the copies made before schema migrations in ``directory`` (Welle 5):
    copies written by a release before N-12 review L-b still hold folder text, which outlives the grant. Runs at each
    start of a workspace on its own database (no migration copy is being written then); a copy without folder index
    is opened and left as it is. Fail closed: a copy that cannot be stripped at all is deleted (its folder text must
    not outlive the grant), and so are leftovers of an interrupted copy (``*.db.partial`` and the temporary files of
    the snapshot next to it). Returns the copies changed or deleted. Never raises."""
    changed: list[Path] = []
    try:
        if not directory.is_dir():
            return changed
        copies = sorted(directory.glob("pre-migration-*.db"))
        leftovers = sorted(directory.glob("pre-migration-*.db.partial*"))
    except OSError:
        logger.exception("The copies made before migrations could not be listed")
        return changed
    for path in leftovers:
        try:
            if not path.is_dir():
                path.unlink(missing_ok=True)
        except OSError:
            logger.exception("A leftover of an interrupted copy before a migration could not be removed")
    for path in copies:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if strip_copy(path, FOLDER_SOURCE, (FOLDER_INDEX_FILES, FOLDER_INDEX_STATES)):
                changed.append(path)
        except Exception:  # never stops the others or the start
            logger.exception("A copy before a migration could not be stripped of folder indexes; it is deleted")
            try:
                path.unlink(missing_ok=True)
                changed.append(path)
            except OSError:
                logger.exception("A copy before a migration that may hold folder text could not be deleted")
    if changed:
        logger.info("Removed granted folders' indexes from %d copies made before migrations", len(changed))
    return changed


#: The tenant of a workspace on its own SQLite database (the desktop).
LOCAL_TENANT = "local"


@dataclass(frozen=True)
class WorkspaceStorage:
    """Storage handed to a workspace instead of its own SQLite database: one tenant of a server database.

    The workspace uses these stores as they are: it neither migrates nor closes the database behind them (its
    owner does, see :mod:`nodestro_runtime.storage_postgres`); its ``data_dir`` holds only the tenant's files
    (tools, Python environments, uploaded inputs). :func:`nodestro_runtime.postgres_tables.tenant_storage` builds
    one for PostgreSQL."""

    tenant_id: str
    store: DocumentStore
    knowledge: KnowledgeTables
    memory: MemoryTables
    #: Encrypts the tenant's provider keys at rest (ADR-0003 point 6); ``None`` stores them as given (tests).
    secrets: TenantSecrets | None = None
    #: The model endpoints (origins) the operator allows for the endpoint providers (``compatible``, ``ollama``,
    #: ``vllm``); any other base URL is refused, so a tenant cannot make the server reach internal services.
    endpoints: tuple[str, ...] = ()
    #: The server's reference prices (shared by every tenant, written only by the server's price sync). ``None``:
    #: the tenant sees Orkexa's built-in prices until the server provides them. Tenants never write to it.
    price_reference: DocumentStore | None = None
    #: The operator's Amazon Bedrock rules for this tenant (its own role only, a forced data residency, the allowed
    #: regions; :meth:`nodestro_runtime.bedrock.BedrockOperatorConfig.for_tenant`). ``None`` or a policy without a
    #: role: Bedrock is not available to the tenant. The server's own AWS credentials are never used for a tenant.
    bedrock: bedrock_auth.BedrockServerPolicy | None = None
    #: Where the bytes of uploaded input files live: ``input_blobs(<data_dir>/inputs)`` returns the blob store
    #: (:mod:`nodestro_runtime.axentra_storage`, ``NODESTRO_FILES_BACKEND=axentra``). ``None``: the volume below it.
    input_blobs: Callable[[Path], Any] | None = None


class Workspace:
    """Every runtime service of one data directory; one instance per process and directory."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        client_factory: Callable[[str], Any] | None = None,
        python_runner: Any | None = None,
        ocr_runner: Any | None = None,
        integrations: Iterable[Integration] | None = None,
        profile: RuntimeProfile | None = None,
        before_migration: BeforeMigration | None = None,
        storage: WorkspaceStorage | None = None,
        vault_key: str | None = None,
        verify_vault_key: bool = False,
    ) -> None:
        """``client_factory(provider)`` replaces the SDK clients, ``python_runner`` uv, ``ocr_runner`` the
        OCR (tests). ``integrations`` replaces the installed ones (:func:`~nodestro_runtime.extensions.discover`).

        ``vault_key`` is the key from the operating system's keychain that the desktop app hands to the backend
        (base64, see :mod:`nodestro_runtime.mcp.vault`); without it, ``NODESTRO_VAULT_KEY`` is looked up. With such a
        key, provider API keys, MCP tokens and the secrets of service connections are stored only encrypted, and
        provider keys stored in plain text by an earlier release are encrypted when the workspace opens. A server
        tenant always uses its own secret box from ``storage`` and ignores ``vault_key``.

        ``verify_vault_key`` (Nodestro Local Docker) requires such a key (``secrets.vault_key_missing``) and refuses one
        that does not fit the secrets already sealed in the data directory (``secrets.vault_key_mismatch``), after the
        schema migration (which touches no secret) and before any secret is sealed or written
        (:mod:`nodestro_runtime.local_vault`).

        ``profile`` chooses the operating profile (:mod:`nodestro_runtime.profile`); without one, the desktop
        profile on ``data_dir`` is used, as before. Given both, ``data_dir`` must be the profile's data
        directory. A profile that cannot be started is refused before any file or database is touched.

        The database is brought to the schema version of this release before anything else uses it
        (:meth:`~nodestro_runtime.storage.SQLiteStore.migrate`; nothing happens while the version is current).
        ``before_migration`` writes the backup a migration needs first; without one, the database file is copied
        to ``<data_dir>/backups/pre-migration-<from>-to-<to>-<time>.db``. A database of a newer release is refused
        (``storage.schema_too_new``) and the workspace does not open.

        ``storage`` replaces the SQLite database with the stores of one tenant (:class:`WorkspaceStorage`); the
        data directory then holds only that tenant's files. Without it the tenant is ``local``."""
        if profile is None:
            if data_dir is None:
                raise TypeError("Workspace needs a data directory or a profile")
            profile = RuntimeProfile.desktop(data_dir)
        elif data_dir is not None:
            profile.matches(data_dir)
        if profile.kind == "server":
            # A server's workspaces are its tenants: they run on handed-in storage, opened by the server after it
            # validated its settings. A server workspace on a directory of its own does not exist.
            if storage is None:
                raise NodestroError(
                    "profile.invalid",
                    "Invalid runtime profile: a server workspace runs on a tenant's storage; start the server "
                    "with the nodestro-server command",
                    problems=[{"field": "storage", "value": "None", "reason": "a server workspace needs storage"}],
                )
            self.profile = profile
        else:
            self.profile = profile.validate()
        data_dir = profile.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        # A tenant's workspace on a server is opened by several API and worker processes at once (ADR-0004): the
        # database coordinates them (job leases), so the directory lock of the desktop does not apply.
        self._directory_lock: DataDirectoryLock | None = DataDirectoryLock(data_dir) if storage is None else None
        # Installed integrations are optional: one that does not fit is left out (and logged) instead
        # of stopping the runtime; integrations passed explicitly must fit.
        # Foreign installed integrations are never imported here (N-22): their tools run only in the Docker sandbox
        # (loaded below, once the Python environments exist), and only while the extension tools switch is on.
        self.extensions = Extensions(
            discover(data_dir=self.data_dir) if integrations is None else integrations,
            strict=integrations is not None,
            installed=integrations is None,
            packages=find_packages(data_dir=self.data_dir) if integrations is None else (),
            reserved={tool.name for tool in (*BUILTIN_TOOLS, *NOTIFY_TOOLS, *DRAFT_TOOLS, *FOLDER_TOOLS) if tool.name},
        )
        self.store: DocumentStore
        self._sqlite: SQLiteStore | None = None
        self._secrets: TenantSecrets | None = None
        # The desktop's key box (keychain key): seals provider keys, MCP tokens and connection secrets of this data
        # directory. ``None`` without a key: MCP tokens and connection secrets are kept for the session only, provider
        # keys are stored as before (see _seal).
        local_box = None
        if storage is None:
            local_box = vault_box_from_env({VAULT_KEY_ENV: vault_key}) if vault_key else vault_box_from_env()
            database = data_dir / DATABASE
            try:
                self._sqlite = Store(database)
            except BaseException:
                # A refused database (storage.unsafe_file) must not keep the directory locked: once the user repaired
                # it (storage.repair_desktop_database_modes), the same process opens the workspace again.
                if self._directory_lock is not None:
                    self._directory_lock.release()
                raise
            self.store = self._sqlite
            self.tenant_id = LOCAL_TENANT
            fresh = self._sqlite._is_empty()
            try:
                self._sqlite.migrate(before_migration=before_migration or self._backup_before_migration)
            except BaseException:
                self._sqlite.close()
                if self._directory_lock is not None:
                    self._directory_lock.release()
                raise
            self.knowledge = KnowledgeBase(database, ocr=ocr_runner, engine=self.extensions.ocr)
            self.memory = MemorySpaces(database)
            self._store_model_defaults(fresh=fresh)
            if verify_vault_key:
                try:
                    if local_box is None:
                        raise local_vault.missing()
                    local_vault.verify_store(self._sqlite, local_box, LOCAL_TENANT)
                except BaseException:
                    self._sqlite.close()
                    if self._directory_lock is not None:
                        self._directory_lock.release()
                    raise
            if local_box is not None:
                self._secrets = local_box
                self._seal_plain_provider_keys()
        else:
            self.store = storage.store
            self.tenant_id = storage.tenant_id
            self._secrets = storage.secrets
            self.knowledge = KnowledgeBase(storage.knowledge, ocr=ocr_runner, engine=self.extensions.ocr)
            # RT-089c: on a server every memory space has one owner (the tenant's store records them).
            self.memory = MemorySpaces(storage.memory, owners=storage.store)
        self.knowledge.activity = self.store  # uploads and deletions go to the activity log
        # N-12: only an installation on this computer can index granted folders; its knowledge search stays limited
        # to the knowledge files.
        self._folders_here = storage is None and self.profile.kind != "server"
        self.knowledge.folder_sources = self._folders_here
        # A server's own environment variables belong to no tenant: its tenants use only their stored keys.
        self._environ: Mapping[str, str] | None = None if storage is None else {}
        self._endpoints: tuple[str, ...] | None = None if storage is None else storage.endpoints
        # Amazon Bedrock: a tenant uses only the role its operator assigned to it (never the server's own chain).
        self._server_tenant = storage is not None
        self._bedrock_policy = storage.bedrock if storage is not None else None
        self._bedrock_roles = bedrock_auth.RoleCache()
        self._bedrock_checked = 0.0
        self._bedrock_check_lock = threading.Lock()
        self.knowledge.ocr_enabled = lambda: bool(self._stored("ocr_enabled"))
        self.knowledge.retrieval = self._knowledge_retrieval
        self.knowledge.embedding_model = self._embedding_model
        # A workspace on handed-in storage serves a tenant of a server: its tool code runs only in containers,
        # whatever the profile or a stored (for example imported) setting says (ADR-0006).
        self.sandbox_policy = SandboxPolicy("server" if storage is not None else self.profile.kind)
        # The owner label of this installation's sandbox containers: its own on a desktop and in Nodestro Local Docker
        # (two installations on one daemon never reap each other's containers), the shared one of a server's
        # processes, which reap only expired containers.
        self.sandbox_owner = docker_status.OWNER if storage is not None else self._installation_owner()
        sandbox = make_sandbox(self._sandbox_kind(), self.sandbox_policy, self._container_sandbox())
        self.tools = ToolLibrary(
            data_dir / "tools",
            sandbox=sandbox,
            extra_tools=self.extensions.tools(),
            extra_specs=self.extensions.specs(),
            policy=self.sandbox_policy,
            provenance=StoredProvenance(self.store),
        )
        self.tools.on_adoption_revoked = audit_revocation(self.store)
        self._apply_sandbox_mode()
        # MCP (ADR-0007): the egress policy is the operator's (process environment), tokens are sealed with the
        # tenant's secret box on a server and with the keychain key from the desktop app; one hub per workspace.
        secret_box = storage.secrets if storage is not None else local_box
        self.mcp = McpService(
            self.store,
            McpHub(
                EgressPolicy.from_env(server=storage is not None, loopback=self.profile.kind == "desktop"),
                limits=McpLimits.from_env(),
            ),
            TokenVault(secret_box, self.tenant_id),
        )
        # Service connections (NAL-A1, ADR-0001): the same key store as MCP tokens, under their own namespace.
        self.connections = ConnectionService(
            self.store,
            TokenVault(secret_box, self.tenant_id, namespace="connection"),
            mode="server" if storage is not None else self.profile.kind,
        )
        self.connections.used_by = self._connection_users
        self.tools.mcp = self.mcp
        self.builder = AgentBuilder(self.tools, self.knowledge, self.memory)
        # The skill library (NAL-B3): agents resolve their assigned skill versions from it.
        self.skills = Skills(self.store)
        self.builder.skill_source = self.skills.resolver()
        self.builder.resolve_connection = self.connections.resolve
        self.builder.find_connection = self.connections.find
        self.builder.mode = self.connections.mode
        self.builder.declared = lambda provider: dict(self._providers().get(provider, {}).get("capabilities") or {})
        self.builder.declared_limits = lambda provider: dict(self._providers().get(provider, {}).get("limits") or {})
        # Local models (Ollama): the managed server of the desktop, the container of Nodestro Local Docker, or the
        # operator's endpoint on a server. Nothing starts here (see local_service).
        self.local_models = LocalModelService(
            data_dir,
            "server" if storage is not None else self.profile.kind,
            store=self.store,
            offline=self.strict_offline,
            preferences=self.selection_preferences,
            external_url=lambda: self._providers().get("ollama", {}).get("base_url") or None,
            environ={} if storage is not None else None,
        )
        self.local_models.idle = lambda: not self.runs.has_live_runs()
        self.builder.reported = self.local_models.capability_overrides
        self.builder.local_window = self._local_window
        self.builder.output_overrides = lambda: output_limits.stored_overrides(self._stored("output_limits"))
        self.tools.generation_max_tokens = lambda model, connection: (
            self.builder.output_limit("tool_generation", model, connection).value
        )
        self._client_factory = client_factory
        self.runs = RunManager(
            self.store,
            self.builder,
            connect=self.connection,
            default_budget=lambda: self.settings().default_budget_usd,
            assistant_model=lambda: self.settings().assistant_model,
        )
        self.runs.budget_policy = self._budget_policy
        # N-08: knowledge text (and a search query of granted content) reaches an embedding server outside this
        # computer only with a grant on its provider; nothing is checked while the provenance switch is off.
        # The check judges the address the embedding model really sends to; a model built for an earlier address is
        # built again first (RT-094 review M-1).
        self.knowledge.embedding_check = lambda address: self.runs.check_cloud_access(
            EMBEDDING_PROVIDER, shared_content=True, endpoint=address or ""
        )
        self.knowledge.embedding_address = self._embedding_address
        # N-12 review H-1: granted folder text is embedded only on this computer, or elsewhere with provenance on and a
        # grant; never merely because a switch is off.
        self.knowledge.folder_embedding_allowed = self.runs.folder_embedding_allowed
        # The internal inbox (NAL-A3): everywhere but on a server whose Axentra inbox is on (that stays the only one).
        self.inbox: LocalInbox | None = None
        if local_inbox_enabled(server=storage is not None):
            self.inbox = LocalInbox(self.store)
            self.runs.notifier = RunNotifier(self.inbox)
            self.runs.listeners.append(self.runs.notifier)
        #: The access grants and denies active now (N-06), read at every use by the tool gate and the folder tools.
        access_records = AccessRules(self.store).current
        #: The search indexes of granted folders (N-12) and the thread that keeps them current; ``None`` while the
        #: folder switch is off or on a server.
        self.folder_index: FolderIndex | None = None
        self.folder_indexer: FolderIndexer | None = None
        if folders_enabled():
            # N-10: the folder tools read granted folders (their own switch, default off).
            self.runs.folder_records = access_records
            self.runs.protected_folder = str(self.data_dir.resolve())
            if self._folders_here:
                self.folder_index = FolderIndex(
                    self.store, self.knowledge, records=access_records, protected=str(self.data_dir.resolve())
                )
                self.folder_indexer = FolderIndexer(self.folder_index)
                self.runs.folder_index = self.folder_index
                # A reindex whose model may not embed folder text drops every folder index first (rebuilt after it).
                self.knowledge.folder_reset = self.folder_index.purge
        if approvals_enabled():
            # Per-call tool approvals (S-2): off unless switched on explicitly (after the security acceptance).
            self.runs.tool_gate = ToolGate(
                ToolApprovals(self.store),
                pre_grants=ToolPreGrants(self.store).of_scope,
                mcp_unattended=unattended_mcp_enabled,
                # M6b: pause runs at a waiting call instead of waiting in a thread (a second switch, default off).
                checkpoints=checkpoints_enabled(),
                max_paused=max_paused(),
                # N-06: the access model v2 (a third switch, default off; activation needs gate G-2).
                access=access_records if access_grants_enabled() else None,
                offline=self.strict_offline,
                protected_folders=(str(self.data_dir.resolve()),),
                local_mcp=self._local_mcp_servers,
                # N-08: egress protection by content provenance (a fourth switch, default off; needs the third).
                provenance=provenance_enabled(),
            )
            # Changing MCP tools are usable only behind this gate (WP-5): every such call is asked (tool_policy.decide).
            self.mcp.write_gate = True
        self.tester = AgentTester(
            self.builder,
            connect=self.connection,
            default_budget=lambda: self.settings().default_budget_usd,
            # The same per-call rules as runs (independent review H-1).
            tool_gate=lambda: self.runs.tool_gate,
        )
        self.usage_log = UsageLog(self.store)
        # The price catalog: on a desktop reference prices and overrides live in this database; a server tenant
        # reads the server's reference prices and keeps only its own overrides (PRICING-1, see pricing.py).
        self.pricing = PricingService(
            self.store,
            reference_store=None if storage is None else storage.price_reference,
            reference_writable=storage is None,
            known_providers=PROVIDERS,
        )
        # Bedrock prices depend on the region: the strict price mode checks the current connection's region.
        self.pricing.conditions_source = self._price_conditions
        for priced in (self.runs, self.tester):
            priced.pricing = self.pricing
            priced.price_mode = self.price_mode
        self.versions = WorkflowVersions(self.store)
        self.tool_drafts = ToolDrafts(self.store, self.tools)
        self.tools.untrusted_sources = lambda: [draft.code for draft in self.tool_drafts.pending()]
        self.python = PythonEnvironments(
            self.store,
            data_dir / "python",
            self.tools,
            runner=python_runner,
            sandbox_kind=self._sandbox_kind,
            docker_state=self._docker_state,
            workflow_exists=lambda workflow_id: self.store.get_raw("workflows", workflow_id) is not None,
            mcp_exists=lambda server_id: self.store.get_raw(MCP_SERVERS, server_id) is not None,
            # One Docker daemon serves every tenant of a server: a tenant's action never removes a shared image.
            remove_images=storage is None,
            # Accepted source builds run in a container with unfiltered network: desktop only, until a filtered build
            # network exists (RT-038, RT-040). A server tenant and Nodestro Local Docker refuse them.
            source_builds=self.sandbox_policy.profile == "desktop",
        )
        # Local MCP servers (WP-8) run in a container of their approved packages' image, never on the host.
        self.mcp.python = self.python
        if isinstance(self.python.images, ContainerImages):
            self.python.images.owner = self.sandbox_owner
        # Docker's preflight and self-test run in the background (never in a settings read), shared by every workspace
        # of the process; the reaper removes this installation's left-over containers: all of them at the start where
        # this process owns them (desktop, Nodestro Local Docker), and expired ones every minute.
        self.docker_monitor: DockerMonitorLike = docker_status.monitor_for(
            self._runtime_sandbox, environ=ContainerImages()._cli_environment(), owner=self.sandbox_owner
        )
        self.docker_monitor.start_reaper(remove_all_first=storage is None)
        self.docker_monitor.status()
        self.tools.dependencies = EnvironmentGate(self.python)
        # Extension tools are offered on this computer only (EXTENSION_MODES): a server's tenants never start their
        # listing containers, switch or not.
        if storage is None and self.profile.kind in EXTENSION_MODES:
            self.tools.add_extension_tools(
                self.extensions.load_packages(
                    ExtensionSandbox(self._extension_sandbox),
                    reserved={*self.tools.builtin, *self.tools.registry.names(), *RESERVED_NAMES},
                )
            )
        self.tool_drafts.python = self.python
        self.runs.tool_sandbox = self.python.workflow_sandbox
        self.tester.tool_sandbox = self.python.workflow_sandbox
        self.runs.resolve_workflow = self.resolve_workflow
        self.runs.tool_marker = self.tool_marker
        # RT-089d: a memory consent covers the custom tools, connections and MCP servers an agent uses.
        self.runs.consent_means = self.consent_means
        self.runs.consent_environment = self.consent_environment
        self.runs.local_model_server = self.local_model_server_here
        self.tester.consent_means = self.consent_means
        # Files uploaded as run inputs: own storage, never knowledge by themselves (see input_files.py).
        inputs = data_dir / "inputs"
        blobs = storage.input_blobs(inputs) if storage is not None and storage.input_blobs is not None else None
        self.inputs = InputFiles(self.store, inputs, ocr=self._input_ocr, blobs=blobs)
        self.runs.inputs = self.inputs
        self.runs.file_names = self.file_catalog
        self.files = ResultFiles(self.store)
        self.runs.result_files = self.files
        #: Conversations with own agents (N-04): each answer is a run of this manager; the API serves them only while
        #: ``NODESTRO_CONVERSATIONS=1``.
        self.conversations = Conversations(self)
        # An answer's run whose conversation was deleted while it ran is deleted once it stops (RT-089).
        self.runs.listeners.append(self.conversations.run_stopped)
        # N-09: the answers' runs read their conversation's offline flag at every call.
        self.runs.conversation_offline = self.conversations.is_offline
        self.runs.conversation_granted = self.conversations.is_granted
        self.runs.mark_conversation_granted = self.conversations.mark_granted
        self.runs.after_run = self.files.write_outputs
        #: Runs of published releases (set by the HTTP API, which owns publishing; ``None``: none).
        self.published: PublishedRuns | None = None
        self.compose()
        # On a server, other processes execute this tenant's runs right now; runs whose worker died are found by
        # the job reaper (nodestro_runtime.jobs.reap), never by a process that merely opens the workspace.
        if storage is None:
            # Runs that were executing when Nodestro stopped can never finish; they become continuable.
            self.runs.recover()
            # Answers' runs of conversations deleted while they ran, left when Nodestro stopped (RT-089).
            try:
                self.conversations.sweep_orphan_runs()
            except Exception:  # housekeeping; it never stops Nodestro from starting
                logging.getLogger(__name__).exception("Deleting the runs of deleted conversations failed")
            # Runs paused at a tool call (M6b): overdue questions expire before anything continues; with the
            # switch off their questions are closed. Continuing them needs the event loop (the app's sweep).
            try:
                self.runs.sweep_tool_waits(resume=False)
            except Exception:  # housekeeping; the app's periodic sweep tries again
                logging.getLogger(__name__).exception("Sweeping runs paused at tool calls failed")
            # The same for installations of Python environments (the last working environment is kept).
            try:
                self.python.recover()
            except Exception:  # housekeeping; it never stops Nodestro from starting
                logging.getLogger(__name__).exception("Recovering interrupted Python installations failed")
            # N-12: the folder indexes are reconciled in the background (ended grants dropped, changes indexed);
            # with the switch off nothing of a granted folder stays in the index.
            try:
                if self.folder_indexer is not None:
                    self.folder_indexer.start()
                elif self._folders_here:
                    FolderIndex(
                        self.store, self.knowledge, records=lambda: ((), ()), protected=str(self.data_dir.resolve())
                    ).purge()
            except Exception:  # housekeeping; it never stops Nodestro from starting
                logging.getLogger(__name__).exception("Reconciling the folder indexes failed")
            # N-12 (Welle 5): copies an older release made before a migration may still hold folder text.
            strip_migration_copies(self.data_dir / "backups")
        # Uploads nobody used (e.g. a run dialog that was closed) are removed after a day.
        try:
            self.inputs.cleanup()
        except Exception:  # cleaning up is housekeeping; it never stops Nodestro from starting
            logging.getLogger(__name__).exception("Cleaning up input files failed")
        if storage is None:
            # Activity events past their retention (a server prunes in its housekeeping, not per workspace).
            try:
                activity.prune(self.store, days=activity.retention_days())
            except Exception:  # housekeeping; it never stops Nodestro from starting
                logging.getLogger(__name__).exception("Pruning the activity log failed")

    def _store_model_defaults(self, *, fresh: bool) -> None:
        """Record once which default models this installation started with (D4): a new installation uses local models,
        an existing one keeps the cloud models it used until now, so an update changes no model. The user's settings
        are not touched; a default model the user saves always wins."""
        if self.store.get_raw(INSTALLATION, MODEL_DEFAULTS) is None:
            local = fresh and self.profile.kind != "server"
            self.store.put_raw(INSTALLATION, MODEL_DEFAULTS, {"origin": "local" if local else "kept"})

    def _default_models(self) -> tuple[str, str]:
        """The default and assistant model when the settings name none (see :meth:`_store_model_defaults`)."""
        marker = self.store.get_raw(INSTALLATION, MODEL_DEFAULTS) or {}
        if marker.get("origin") == "local":
            return LOCAL_DEFAULT_MODEL, LOCAL_ASSISTANT_MODEL
        return DEFAULT_AGENT_MODEL, DEFAULT_ASSISTANT_MODEL

    def _backup_before_migration(self, current: int, target: int) -> None:
        """Copy the database before a schema migration (the default ``before_migration``)."""
        backup_migrated_database(self.sqlite_store, self.data_dir / "backups", current, target)

    @property
    def sqlite_store(self) -> SQLiteStore:
        """The workspace's own SQLite database (backups, maintenance, diagnostics of the desktop).

        A workspace on handed-in storage has none (``storage.not_sqlite``): its database belongs to the server."""
        if self._sqlite is None:
            raise NodestroError(
                "storage.not_sqlite", "This workspace keeps its data in the server database, not in a SQLite file"
            )
        return self._sqlite

    def compose(self) -> None:
        """Add services before interrupted runs are recovered; applications built on the runtime override it."""

    def file_catalog(self) -> FileCatalog:
        """Every file a run can reference (input files and knowledge files)."""
        return FileCatalog(self.inputs, self.knowledge.store)

    def _input_ocr(self) -> Any:
        """The OCR for scanned PDFs among the input files, when it is switched on and available."""
        if not self._stored("ocr_enabled"):
            return None
        return self.knowledge.runner()

    def background_work(self) -> list[str]:
        """What this workspace does in this process beyond a request right now (``kind:id`` entries; empty: idle).

        A server closes a tenant's idle workspace only while this is empty (nodestro-api API-001): executing runs,
        tool generation jobs, live run chat answers, Python environment installations, local model jobs and a
        semantic reindex all live in this process and would be lost."""
        found = [f"run:{run.id}" for run in self.runs.live_runs()]
        found += [f"tool_job:{item}" for item in self.tools.active_jobs()]
        found += [f"run_chat:{item}" for item in self.runs.chat.answering()]
        found += [f"python_install:{item}" for item in self.python.installing()]
        found += [f"local_model_job:{item}" for item in self.local_models.running_jobs()]
        if self.knowledge.reindexing():
            found.append("reindex:knowledge")
        return found

    def close(self) -> None:
        """Release the database connections; closing twice is harmless.

        Runs still executing are asked to stop first; they cannot store their final state once
        the database is closed, so they are marked interrupted at the next start. Prefer
        :meth:`aclose`, which waits for them.
        """
        if self.store.closed:
            return
        live = [run_id for run_id in list(self.runs._contexts) if self.runs.is_live(run_id)]
        if live:
            logging.getLogger(__name__).warning(
                "Closing the workspace while %d runs are executing; they are cancelled", len(live)
            )
            for run_id in live:
                self.runs.cancel(run_id)
        if self.folder_indexer is not None:
            self.folder_indexer.stop()
        self.mcp.hub.close()
        self.local_models.close()
        self.knowledge.close()
        self.memory.close()
        self.store.close()
        if self._directory_lock is not None:
            self._directory_lock.release()

    def folder_access_changed(self, folder_id: str | None = None) -> None:
        """A person granted or revoked a folder, or changed an access grant or deny (N-12): the index of every folder
        grant that ended is dropped now, and the indexes are brought up to date soon (``folder_id``: only that one).
        Nothing happens while folder grants are off."""
        if self.folder_index is None:
            return
        self.folder_index.drop_ended()
        if self.folder_indexer is not None:
            self.folder_indexer.wake(folder_id)

    async def aclose(self) -> None:
        """Cancel the runs of this process, wait until they stored their final state, then :meth:`close`."""
        if not self.store.closed:
            await self.runs.shutdown()
        self.close()

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> Workspace:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---- settings ----------------------------------------------------------------

    def _connection_users(self, connection_id: str) -> list[str]:
        """The agents (``workflow / node``) that bind the service connection ``connection_id`` (current versions)."""
        found: list[str] = []
        for _collection, text in self.store.search_raw(("workflows",), connection_id):
            try:
                workflow = Workflow.model_validate_json(text)
            except ValueError:
                continue
            for node in workflow.nodes:
                agent = node.agent
                if agent is None:
                    continue
                if any(item.connection_id == connection_id for item in agent.tool_settings.values()):
                    found.append(f"{workflow.name} / {node.label or agent.name or node.id}")
        return sorted(found)

    def _stored(self, key: str) -> Any:
        values = self.store.get_raw("settings", "app") or {}
        return values.get(key)

    def _container_sandbox(self) -> Callable[[], Sandbox] | None:
        """In every profile, tool code without libraries runs in Nodestro's runtime image (no bind mounts, so the Docker
        daemon may run elsewhere or in a VM; no image anybody builds by hand; Orkexa's ``orkexa-sandbox`` image is not
        used)."""
        # The Python environments are created after the tool library; the image is resolved on the first call.
        return self._runtime_sandbox

    def _runtime_sandbox(self) -> RuntimeImageSandbox:
        """The container sandbox for tool code without libraries: Nodestro's runtime image (built on first use, no
        image to build by hand, no bind mounts). Its self-test also proves the daemon (:attr:`docker_monitor`)."""
        return RuntimeImageSandbox(
            lambda: self.python.runtime_image(),
            timeout=15.0,
            environ=ContainerImages()._cli_environment(),
            owner=self.sandbox_owner,
        )

    def _extension_sandbox(self, timeout: float) -> RuntimeImageSandbox:
        """The container sandbox for tools of installed integrations (N-22): Nodestro's runtime image, this
        installation's owner label, the call's time limit."""
        return RuntimeImageSandbox(
            lambda: self.python.runtime_image(),
            timeout=timeout,
            environ=ContainerImages()._cli_environment(),
            owner=self.sandbox_owner,
        )

    def _installation_owner(self) -> str:
        """This installation's container owner (:func:`~nodestro_runtime.docker_status.installation_owner`) from its
        data directory and random installation id; a data directory whose id cannot be written uses its path only."""
        try:
            installation = installation_id(self.data_dir / "python")
        except OSError:
            installation = ""
        return docker_status.installation_owner(self.data_dir, installation)

    def _sandbox_kind(self) -> SandboxMode:
        """The sandbox setting in effect (:mod:`.sandbox_selection`): ``docker`` wherever only containers may run
        code; on the desktop the stored choice, and ``auto`` when none was made (also for installations of earlier
        releases that never chose: an explicitly stored ``subprocess`` stays ``subprocess``, which since Docker-first
        runs no AI-generated code at all; the built-in Linux sandbox is used only after ``linux`` was chosen)."""
        stored = self._stored("sandbox")
        if self.sandbox_policy.profile != "desktop" or stored == "docker":
            return "docker"
        if stored == "subprocess":
            return "subprocess"
        return "linux" if stored == "linux" else "auto"

    def _apply_sandbox_mode(self) -> None:
        """Tell the tool library the sandbox mode: the ``auto`` mode's container fallback (taken away in other modes),
        and whether the built-in Linux sandbox was chosen (``linux``)."""
        mode = self._sandbox_kind()
        auto = self.sandbox_policy.profile == "desktop" and mode == "auto"
        self.tools.container_fallback = self._auto_container if auto else None
        self.tools.sandbox_mode = mode
        self.tools.docker_status = self._docker_state

    def _auto_container(self) -> Sandbox | None:
        """In ``auto`` mode: a container for untrusted code without libraries, while Docker passed its preflight and
        self-test (:meth:`docker_status`)."""
        if not self._docker_state().approved:
            return None
        return self._runtime_sandbox()

    def _providers(self, values: dict[str, Any] | None = None, *, sealed: bool = False) -> dict[str, dict[str, Any]]:
        """The stored settings per provider; a top-level ``api_key`` counts as Anthropic's key.

        With a secret box (server tenants, and a desktop with its keychain key), keys are stored encrypted and returned
        decrypted, unless ``sealed``. A key that cannot be decrypted (or was stored in plain text) counts as missing: it
        has to be entered again. Without a box, an encrypted key (written while a keychain key was available) counts as
        missing too: it is never handed to a provider as if it were the key itself."""
        values = values if values is not None else (self.store.get_raw("settings", "app") or {})
        providers: dict[str, dict[str, Any]] = {
            name: dict(item) for name, item in (values.get("providers") or {}).items() if isinstance(item, dict)
        }
        legacy = values.get("api_key")
        if legacy and not providers.get("anthropic", {}).get("api_key"):
            providers.setdefault("anthropic", {})["api_key"] = legacy
        if self._secrets is not None and not sealed:
            for name, entry in providers.items():
                if entry.get("api_key"):
                    try:
                        entry["api_key"] = self._secrets.decrypt(self.tenant_id, _key_field(name), entry["api_key"])
                    except NodestroError:
                        logger.warning("The stored %s key of tenant %s is unreadable", name, self.tenant_id)
                        entry.pop("api_key")
        elif self._secrets is None and not sealed:
            for name, entry in providers.items():
                if entry.get("api_key") and is_encrypted(entry["api_key"]):
                    logger.warning("The stored %s key is encrypted and no key store is available", name)
                    entry.pop("api_key")
        return providers

    def _seal_plain_provider_keys(self) -> None:
        """Encrypt provider keys that an earlier release (or a session without the keychain) stored in plain text.

        Runs once per opening, inside one transaction: every plain key is replaced by its encrypted form, or nothing
        changes. Keys that are already encrypted stay as they are; a key that cannot be encrypted is never dropped."""
        if self._secrets is None:
            return
        secrets = self._secrets
        with self.store.transaction():
            values = self.store.get_raw("settings", "app") or {}
            changed = local_vault.seal_plain_provider_keys(
                values, lambda field, plain: secrets.encrypt(self.tenant_id, field, plain)
            )
            if changed:
                self.store.put_raw("settings", "app", values)
        # Copies of the database from before a schema migration may hold the same keys in plain text.
        copies = local_vault.seal_plain_copies(self.data_dir, secrets, self.tenant_id)
        if copies:
            logger.info("Encrypted %d provider key(s) in copies of the database", copies)
        if not changed:
            return
        # The plain keys must not stay in the write-ahead log, in the database file's old page or in pages an earlier
        # release freed without secure_delete (once: only when plain keys were sealed).
        scrub = getattr(self.store, "scrub", None)
        if callable(scrub):
            scrub()
        logger.info("Encrypted %d stored provider key(s) with the keychain key", len(changed))

    def _seal(self, provider: str, key: str) -> str:
        if self._secrets is None:
            # No key store (no desktop keychain key, no NODESTRO_VAULT_KEY): stored as before, in the database file
            # that only this user can read (tabrion's 0600 profile). The desktop app always hands its keychain key.
            return key
        return self._secrets.encrypt(self.tenant_id, _key_field(provider), key)

    def settings(self) -> Settings:
        values = self.store.get_raw("settings", "app") or {}
        budget = values.get("default_budget_usd", 2.0)
        stored = self._providers(values)
        fallback_model, fallback_assistant = self._default_models()
        default_model = values.get("default_model") or fallback_model
        docker = self._docker_state()  # one snapshot; never waits for the daemon
        return Settings(
            providers=[self._provider_status(name, stored.get(name, {})) for name in PROVIDERS],
            default_provider=provider_of(default_model),
            default_model=default_model,
            assistant_model=values.get("assistant_model") or fallback_assistant,
            default_budget_usd=budget,
            sandbox=self._sandbox_kind(),
            docker_available=docker.approved,
            builtin_sandbox=self._builtin_sandbox(),
            sandbox_selection=sandbox_selection.select(self.sandbox_policy, self._sandbox_kind(), docker),
            sandbox_backends=sandbox_selection.backends(self.sandbox_policy, docker),
            language=values.get("language") if values.get("language") in LANGUAGES else None,
            architect_budget_usd=values.get("architect_budget_usd", 2.0),
            architect_max_repairs=int(values.get("architect_max_repairs") or 0)
            if values.get("architect_max_repairs") is not None
            else 2,
            output_limits=output_limits.stored_overrides(values.get("output_limits")),
            output_limit_defaults=dict(output_limits.OPERATIONS),
            architect_retry_truncated=values.get("architect_retry_truncated") is not False,
            architect_retry_max_tokens=_retry_cap(values.get("architect_retry_max_tokens")),
            ocr_enabled=bool(values.get("ocr_enabled")),
            ocr_available=self.knowledge.ocr_available(),
            price_mode="strict" if values.get("price_mode") == "strict" else "standard",
            max_run_budget_usd=run_budget.stored_amount(values.get("max_run_budget_usd")),
            operator_max_run_budget_usd=run_budget.operator_cap(server=self._server_tenant),
            budget_decision_hours=run_budget.decision_hours(values.get("budget_decision_hours")),
            knowledge_retrieval=_retrieval_mode(values.get("knowledge_retrieval")),
            embedding_model=values.get("embedding_model") or None,
            strict_offline=bool(values.get("strict_offline")),
            prefer_european_models=bool(values.get("prefer_european_models")),
            local_reasoning="full" if values.get("local_reasoning") == "full" else "auto",
            reasoning_summaries=bool(values.get("reasoning_summaries")),
            model_defaults="local" if fallback_model == LOCAL_DEFAULT_MODEL else "kept",
        )

    def output_limit(
        self, operation: str, model: str, connection: Connection | None = None
    ) -> output_limits.EffectiveLimit:
        """The output limit one of Nodestro's own AI operations sends to ``model`` (setting, else built-in default,
        capped by the model's maximum; see :mod:`nodestro_runtime.output_limits`)."""
        return self.builder.output_limit(operation, model, connection)

    def _budget_policy(self) -> tuple[Decimal | None, int]:
        """The highest limit a budget decision may set (the tenant's and the operator's maximum) and the hours an
        attended run waits for a decision (BUDGET-1)."""
        current = self.settings()
        return (
            run_budget.effective_cap(current.max_run_budget_usd, current.operator_max_run_budget_usd),
            current.budget_decision_hours,
        )

    def price_mode(self) -> PriceMode:
        """``standard`` (unknown and stale prices only warn) or ``strict`` (see :mod:`.cost_tracking`)."""
        return self.settings().price_mode

    def tracker(self, budget: float | None = None) -> PricedTracker:
        """A usage tracker for one AI operation of this workspace, priced with its catalog and price mode."""
        return PricedTracker(
            Decimal(str(budget)) if budget is not None else None, pricing=self.pricing, mode=self.price_mode()
        )

    def _builtin_sandbox(self) -> BuiltinSandboxStatus:
        if self.sandbox_policy.profile == "local-container":
            # ``server`` keeps its meaning for clients: this profile runs tool code in containers only.
            return BuiltinSandboxStatus(
                reason="server", detail="Nodestro Local Docker runs tool code in sandbox containers only"
            )
        if self.sandbox_policy.profile != "desktop":
            return BuiltinSandboxStatus(reason="server", detail="A server runs tool code in containers only")
        status = LinuxSandbox.status()
        return BuiltinSandboxStatus(
            available=status.available, reason=status.reason, detail=status.detail, system=status.system
        )

    def _docker_state(self, *, refresh: bool = False, wait: float = 0.0) -> DockerStatus:
        return self.docker_monitor.status(refresh=refresh, wait=wait)

    def docker_status(self, refresh: bool = False, *, wait: float = 0.0) -> SandboxBackendStatus:
        """The Docker sandbox on this computer (:mod:`nodestro_runtime.docker_status`), from the background check: it
        never waits for the daemon unless ``wait`` (seconds) is given. ``checking`` until the first check finished;
        ``approved`` only after the preflight and the in-container self-test passed. ``refresh`` checks again now
        (in the background), the self-test included."""
        return self._docker_state(refresh=refresh, wait=wait).backend()

    def _docker_available(self) -> bool:
        """Whether Docker passed its preflight and self-test (the earlier flag; never blocks)."""
        return self._docker_state().approved

    def update_settings(self, update: SettingsIn) -> Settings:
        values = self.store.get_raw("settings", "app") or {}
        changes = update.model_dump(exclude_unset=True)
        if changes.get("sandbox") and changes["sandbox"] != "docker":
            self.sandbox_policy.check_construction("none")  # before anything is stored
        # A top-level ``api_key`` is Anthropic's key; it moves into the provider settings (once, idempotent).
        providers = self._providers(values, sealed=True)
        if self._secrets is not None:  # with a key store a plain-text key is never kept (it is entered again)
            for entry in providers.values():
                if entry.get("api_key") and not is_encrypted(entry["api_key"]):
                    entry.pop("api_key")
        values.pop("api_key", None)
        requested = changes.pop("providers", None) or {}
        legacy = changes.pop("api_key", None)
        if legacy is not None:
            requested.setdefault("anthropic", {}).setdefault("api_key", legacy)
        for name, change in requested.items():
            if name not in PROVIDERS:
                raise NodestroError("provider.unknown", f"Unknown provider {name!r}", provider=name)
            entry = providers.setdefault(name, {})
            if PROVIDERS[name].auth == "aws":
                self._update_bedrock(entry, change)
                continue
            for key in _BEDROCK_FIELDS:
                if change.get(key):
                    raise NodestroError(
                        "provider.field_not_supported",
                        f"{key} is a setting of Amazon Bedrock, not of {PROVIDERS[name].label}",
                        provider=name,
                        field=key,
                    )
            for key in ("api_key", "base_url"):
                if change.get(key) is None:
                    continue
                if key == "base_url" and entry.get(key) and change[key].strip() == redact(entry[key]):
                    continue  # the shown (redacted) URL sent back unchanged keeps the stored one
                text = check_base_url(change[key]) if key == "base_url" else change[key].strip()
                if text and key == "base_url":
                    check_endpoint_allowed(text, self._endpoints)
                if text:
                    entry[key] = self._seal(name, text) if key == "api_key" else text
                else:
                    entry.pop(key, None)
            if change.get("capabilities") is not None:
                entry["capabilities"] = check_capabilities(change["capabilities"])
            if change.get("limits") is not None:
                if not PROVIDERS[name].declare_capabilities and change["limits"]:
                    raise NodestroError(
                        "provider.field_not_supported",
                        f"The limits of {PROVIDERS[name].label} models come from Orkexa and cannot be declared",
                        provider=name,
                        field="limits",
                    )
                limits = check_limits(change["limits"])
                if limits:
                    entry["limits"] = limits
                else:
                    entry.pop("limits", None)
        values["providers"] = {name: entry for name, entry in providers.items() if entry}
        for key in ("default_model", "assistant_model"):
            if changes.get(key):
                changes[key] = check_model(changes[key])
        if changes.get("embedding_model"):
            changes["embedding_model"] = _embedding_name(changes["embedding_model"])
        if changes.get("knowledge_retrieval") in ("vector", "hybrid") and not (
            changes.get("embedding_model") or values.get("embedding_model")
        ):
            changes["embedding_model"] = LOCAL_EMBEDDING_MODEL
        if "output_limits" in changes:
            requested_limits = changes.pop("output_limits") or {}
            merged: dict[str, Any] = dict(output_limits.stored_overrides(values.get("output_limits")))
            merged.update(requested_limits)  # null or 0 removes an override (check_overrides drops it)
            values["output_limits"] = output_limits.check_overrides(merged)
        if changes.pop("clear_max_run_budget", None):
            values.pop("max_run_budget_usd", None)
            changes.pop("max_run_budget_usd", None)
        if changes.get("max_run_budget_usd") is not None:
            changes["max_run_budget_usd"] = str(changes["max_run_budget_usd"])
        values.update(changes)
        self.store.put_raw("settings", "app", values)
        if changes.get("sandbox"):
            self.tools.set_sandbox(make_sandbox(changes["sandbox"], self.sandbox_policy, self._container_sandbox()))
            self._apply_sandbox_mode()
        if "strict_offline" in changes:
            self.local_models.sync_offline()  # the managed server's network route follows the mode (RT-059)
        return self.settings()

    # ---- Amazon Bedrock ------------------------------------------------------------

    def _local_container(self) -> bool:
        return not self._server_tenant and self.profile.kind == "local-container"

    def _bedrock_policy_for_tenant(self) -> bedrock_auth.BedrockServerPolicy | None:
        """The operator's policy, only if it was made for this workspace's own tenant."""
        policy = self._bedrock_policy
        if policy is None or policy.tenant_id != self.tenant_id:
            return None
        return policy

    def _provider_status(self, name: str, stored: Mapping[str, Any]) -> Any:
        status = provider_status(
            name,
            stored,
            self._environ,
            server=self._server_tenant,
            bedrock_policy=self._bedrock_policy_for_tenant(),
            local_container=self._local_container(),
        )
        if name == "ollama" and not stored.get("base_url"):
            if self.local_models.provides_endpoint():
                status.configured = True
                status.local_server = True
            elif self._client_factory is not None and self._client_factory(name) is not None:
                status.configured = True  # an injected client (tests, embedding applications) is the server
        return status

    def _knowledge_retrieval(self) -> tuple[Any, str | None]:
        values = self.store.get_raw("settings", "app") or {}
        return _retrieval_mode(values.get("knowledge_retrieval")), values.get("embedding_model") or None

    def _embedding_address(self) -> str | None:
        """The address of the server the knowledge embeddings use now (the one agents use, as the N-08 check reads
        it: :meth:`RunManager.connect`); ``None``: none."""
        return (self.runs.connect("ollama").base_url or "").removesuffix("/v1").rstrip("/") or None

    def _embedding_model(self, name: str) -> OllamaEmbeddings:
        """An Ollama embedding model on the server agents use (the managed one, the container, or the operator's)."""
        base = self._embedding_address()
        if not base:
            raise NodestroError("local.not_running", "No local model server is running")
        return OllamaEmbeddings(_embedding_name(name), base_url=base, allow_remote=True)

    def strict_offline(self) -> bool:
        """Whether the strict offline mode is on: only local models and local tools, no downloads (D12)."""
        return bool(self._stored("strict_offline"))

    def selection_preferences(self) -> SelectionPreferences:
        """What the local model selection prefers: European models when the setting is on (D13)."""
        return SelectionPreferences(region="eu" if self._stored("prefer_european_models") else None)

    def reasoning_saving(self, accelerated: bool | Literal["ask"] | None = "ask") -> bool:
        """Whether the CPU saving mode applies now: the setting ``local_reasoning`` is ``auto`` (the default) and the
        local model server has no GPU (see ``agent_builder.reasoning_reduction``). An unknown server never saves.
        ``accelerated``: what :meth:`LocalModelService.accelerated` already said (asked again by default)."""
        if self._stored("local_reasoning") == "full":
            return False
        if accelerated == "ask":
            accelerated = self.local_models.accelerated()
        return accelerated is False

    def _update_bedrock(self, entry: dict[str, Any], change: Mapping[str, Any]) -> None:
        """Check and store a change of the Bedrock settings (region, sign-in, profile name, residency)."""
        for key in ("api_key", "base_url"):
            if change.get(key):
                raise NodestroError(
                    "provider.field_not_supported",
                    f"Amazon Bedrock uses AWS credentials, not {key}: Nodestro neither stores AWS keys nor sends "
                    "signed requests to custom endpoints",
                    provider="bedrock",
                    field=key,
                )
        for field in ("capabilities", "limits"):
            if change.get(field):
                raise NodestroError(
                    "provider.field_not_supported",
                    f"Amazon Bedrock's {field} come from Orkexa and cannot be declared",
                    provider="bedrock",
                    field=field,
                )
        server = self._server_tenant
        policy = self._bedrock_policy_for_tenant()
        if server and (policy is None or policy.role is None) and any(change.get(key) for key in _BEDROCK_FIELDS):
            raise bedrock_auth.not_available()
        methods = bedrock_auth_methods(server=server, local_container=self._local_container())
        before = {key: entry.get(key) for key in _BEDROCK_FIELDS}
        if change.get("auth") is not None:
            auth = change["auth"].strip()
            if auth and auth not in bedrock_auth.AUTH_METHODS:
                raise NodestroError(
                    "provider.auth_invalid", f"Unknown AWS sign-in method {auth[:40]!r}", auth=auth[:40]
                )
            if auth and auth not in methods:
                raise NodestroError(
                    "provider.auth_not_allowed",
                    f"The AWS sign-in method {auth!r} is not available here; allowed: {', '.join(methods)}",
                    auth=auth,
                    allowed=", ".join(methods),
                )
            if auth:
                entry["auth"] = auth
            else:
                entry.pop("auth", None)
        if change.get("profile_name") is not None:
            name = change["profile_name"].strip()
            if name and "profile" not in methods:
                raise NodestroError(
                    "provider.field_not_supported",
                    "AWS profiles are a desktop setting; this installation signs in to AWS another way",
                    provider="bedrock",
                    field="profile_name",
                )
            if name:
                entry["profile_name"] = bedrock_auth.check_profile_name(name)
            else:
                entry.pop("profile_name", None)
        if change.get("region") is not None:
            region = change["region"].strip()
            if region:
                entry["region"] = bedrock_auth.check_region(region)
            else:
                entry.pop("region", None)
        if change.get("data_residency") is not None:
            residency = change["data_residency"].strip()
            if residency not in ("", "eu_only"):
                raise NodestroError(
                    "provider.data_residency_invalid", "Data residency must be eu_only or empty", value=residency[:40]
                )
            if not residency and server and policy is not None and policy.data_residency == "eu_only":
                raise NodestroError(
                    "provider.data_residency_forced",
                    "The server's operator requires EU-only data residency for Amazon Bedrock; it cannot be relaxed",
                )
            if residency:
                entry["data_residency"] = residency
            else:
                entry.pop("data_residency", None)
        settings, problem = bedrock_settings(
            entry, server=server, policy=policy, local_container=self._local_container()
        )
        if settings.region is not None:
            if problem == "provider.region_not_allowed":
                regions = policy.regions() if policy is not None else None
                raise NodestroError(
                    "provider.region_not_allowed",
                    f"The region {settings.region} is not allowed on this server",
                    region=settings.region,
                    allowed=", ".join(regions or ()),
                )
            bedrock_auth.check_regions((settings.region,), settings.data_residency)
        if {key: entry.get(key) for key in _BEDROCK_FIELDS} != before:
            entry.pop("verification", None)  # a changed configuration has to be checked again

    def _price_conditions(self, provider: str) -> dict[str, str]:
        """The tariff conditions known before a request (Bedrock: the region of the settings in effect, read anew on
        every call; for a server tenant its own region). Empty when Bedrock cannot be used."""
        if provider not in PROVIDERS or PROVIDERS[provider].auth != "aws":
            return {}
        settings, problem = bedrock_settings(
            self._providers().get(provider, {}),
            server=self._server_tenant,
            policy=self._bedrock_policy_for_tenant(),
            local_container=self._local_container(),
        )
        if problem is not None or settings.region is None:
            return {}
        return {"aws_region": settings.region}

    def _bedrock_connection(self, stored: Mapping[str, Any]) -> Connection:
        policy = self._bedrock_policy_for_tenant()
        server = self._server_tenant
        settings, problem = bedrock_settings(
            stored, server=server, policy=policy, local_container=self._local_container()
        )
        if problem == "provider.bedrock_not_available":
            error = bedrock_auth.not_available()
            raise MissingApiKeyError("bedrock", code=error.code, message=str(error))
        if problem == "provider.not_configured":
            raise MissingApiKeyError("bedrock")
        if problem == "provider.auth_not_allowed":
            raise MissingApiKeyError(
                "bedrock",
                code=problem,
                message="The stored AWS sign-in method of Amazon Bedrock is not available here; choose another one",
            )
        if problem is not None:  # a region outside the operator's list or outside the EU with eu_only
            raise NodestroError(
                problem,
                f"The Bedrock region {settings.region} is not allowed by the data residency or the operator",
                region=settings.region or "",
            )
        client = self._client_factory("bedrock") if self._client_factory is not None else None
        return Connection(
            provider="bedrock",
            client=client,
            bedrock=settings,
            session_factory=self._bedrock_session_factory(settings, policy),
            require_session=server,
        )

    def _bedrock_session_factory(
        self, settings: bedrock_auth.BedrockSettings, policy: bedrock_auth.BedrockServerPolicy | None
    ) -> Callable[[], Any]:
        """A new boto3 session per call: the tenant's assumed role on a server, the user's own chain on a desktop."""
        if self._server_tenant:
            if policy is None or policy.role is None or settings.region is None:
                raise bedrock_auth.not_available()
            credentials = self._bedrock_roles.get(policy, settings.region)
            return credentials.session
        return lambda: bedrock_auth.desktop_session(settings)

    def verify_provider(self, provider: str, *, model: str | None = None) -> Any:
        """Check a provider's configuration with free calls only and store the result (Amazon Bedrock).

        STS ``GetCallerIdentity`` plus one Bedrock control-plane read (:func:`nodestro_runtime.bedrock.verify`;
        ``model`` chooses ``GetInferenceProfile``/``GetFoundationModel`` instead of ``ListFoundationModels``); no
        model is invoked. At most one check per :data:`VERIFY_INTERVAL_SECONDS` (``provider.verify_rate_limited``).
        Returns the provider's status; the result names no AWS identity.
        """
        import time

        if provider not in PROVIDERS:
            raise NodestroError("provider.unknown", f"Unknown provider {provider!r}", provider=provider)
        if PROVIDERS[provider].auth != "aws":
            raise NodestroError(
                "provider.verify_not_supported",
                f"{PROVIDERS[provider].label} has no verification check",
                provider=provider,
            )
        with self._bedrock_check_lock:  # two concurrent requests: only one of them checks
            elapsed = time.monotonic() - self._bedrock_checked
            if self._bedrock_checked and elapsed < VERIFY_INTERVAL_SECONDS:
                raise NodestroError(
                    "provider.verify_rate_limited",
                    "The AWS settings were checked a moment ago; try again shortly",
                    retry_after=int(VERIFY_INTERVAL_SECONDS - elapsed) + 1,
                )
            self._bedrock_checked = time.monotonic()
        values = self.store.get_raw("settings", "app") or {}
        stored = self._providers(values).get(provider, {})
        connection_settings, _problem = bedrock_settings(
            stored,
            server=self._server_tenant,
            policy=self._bedrock_policy_for_tenant(),
            local_container=self._local_container(),
        )
        try:
            connection = self._bedrock_connection(stored)
            assert connection.bedrock is not None and connection.session_factory is not None
            result = bedrock_auth.verify(connection.session_factory(), connection.bedrock, model=model)
        except NodestroError as exc:
            result = {"state": "failed", "checked_at": bedrock_auth.now_text(), "code": exc.code}
        values = self.store.get_raw("settings", "app") or {}
        providers = dict(values.get("providers") or {})
        entry = dict(providers.get(provider) or {})
        result["for"] = verification_key(connection_settings, self._bedrock_policy_for_tenant())
        entry["verification"] = result
        providers[provider] = entry
        values["providers"] = providers
        self.store.put_raw("settings", "app", values)
        return self._provider_status(provider, self._providers(values).get(provider, {}))

    def reload_after_restore(self) -> None:
        """Pick up restored data that is cached in memory: the tool registry and its sandbox.

        A backup can come from anywhere, so the provenance of its tools is unknown: every restored custom tool
        becomes ``untrusted`` (ADR-0006)."""
        self.tools.provenance.clear()
        self.tools.reload(make_sandbox(self._sandbox_kind(), self.sandbox_policy, self._container_sandbox()))
        self._apply_sandbox_mode()
        self.inputs.forget()

    # ---- workflows ------------------------------------------------------------------

    def workflow(self, workflow_id: str) -> Workflow:
        """A saved workflow as it is now; raises :class:`KeyError` when there is none."""
        found = self.store.get("workflows", workflow_id, Workflow)
        if found is None:
            raise KeyError(workflow_id)
        return found

    def save_workflow(self, workflow: Workflow | WorkflowIn) -> Workflow:
        """Store a workflow (a :class:`WorkflowIn` becomes a new one) and record its version, as the studio does."""
        saved = workflow if isinstance(workflow, Workflow) else Workflow(**workflow.model_dump())
        # The workflow and its version are stored together: a crash in between never leaves a
        # saved workflow without its version.
        with self.store.transaction():
            self.store.lock("workflows", saved.id)  # ``previous`` stays the state this save replaces
            previous = self.store.get("workflows", saved.id, Workflow)
            # Legacy exemptions (S-2, D10b) come only from the activation step, never from a save, copy or import.
            saved = keep_origin(keep_only_inherited(saved, previous), previous)
            self.store.put("workflows", saved.id, saved)
            self.versions.record(saved, previous=previous)
        return saved

    def legacy_tool_bindings(self) -> LegacyReport | None:
        """What the activation step stamped (``None``: approvals were never activated in this workspace)."""
        raw = self.store.get_raw("tool_approvals_activation", "legacy")
        return None if raw is None else LegacyReport.model_validate(raw)

    def record_legacy_tool_bindings(
        self, added: LegacyReport, *, publications_stamped_at: datetime | None = None
    ) -> LegacyReport:
        """Add newly stamped exemptions to the stored activation report (earlier ones stay listed) and return it;
        ``publications_stamped_at`` records that the published releases were stamped."""
        stored = self.legacy_tool_bindings() or LegacyReport()
        combined = LegacyReport(
            bindings=[*stored.bindings, *added.bindings],
            workflows=stored.workflows + added.workflows,
            versions=stored.versions + added.versions,
            stamped_at=stored.stamped_at or added.stamped_at,
            stamped_by=stored.stamped_by or added.stamped_by,
            publications_stamped_at=stored.publications_stamped_at or publications_stamped_at,
        )
        self.store.put_raw("tool_approvals_activation", "legacy", combined.model_dump(mode="json"))
        return combined

    def stamp_legacy_tool_bindings(self, *, by: str = "system") -> LegacyReport:
        """The activation step of per-call tool approvals (S-2, D10b): every existing tool binding of every stored
        workflow and workflow version becomes a visible legacy exemption, bound to the tool's current version.

        Runs **once**: when the stored report ``tool_approvals_activation/legacy`` (:meth:`legacy_tool_bindings`)
        exists, a repeated call stamps nothing (it is audited as repeated), so an assignment made after the first
        call can never become an exemption by running the step again. Returns what this call stamped. Runs only
        when an administrator activates approvals; nothing calls it on its own."""

        def version_of(name: str) -> int | None:
            try:
                return self.tools.get(name).version
            except KeyError:
                return None

        with self.store.transaction():
            self.store.lock("tool_approvals_activation", "legacy")  # once, also when two processes start it
            earlier = self.legacy_tool_bindings()
            if earlier is not None:
                record_audit(
                    self.store,
                    ToolApprovalEvent(
                        kind="legacy_stamped",
                        actor=by,
                        details={"bindings": 0, "repeated": True, "first_stamped_at": str(earlier.stamped_at)},
                    ),
                )
                return LegacyReport(stamped_at=earlier.stamped_at, stamped_by=earlier.stamped_by)
            report = LegacyReport(stamped_at=now(), stamped_by=by)
            for key, stored in self.store.items("workflows", Workflow):
                stamped, found = stamp_workflow(stored, version_of)
                if found:
                    self.store.put_raw("workflows", key, stamped.model_dump(mode="json"))
                    report.workflows += 1
                    report.bindings.extend(item.model_copy(update={"workflow_id": key}) for item in found)
            for key, version in self.store.items("workflow_versions", WorkflowVersion):
                snapshot, found = stamp_workflow(version.snapshot, version_of)
                if found:
                    updated = version.model_copy(update={"snapshot": snapshot})
                    self.store.put_raw("workflow_versions", key, updated.model_dump(mode="json"))
                    report.versions += 1
                    report.bindings.extend(
                        item.model_copy(update={"workflow_id": version.workflow_id, "workflow_version": version.number})
                        for item in found
                    )
            self.record_legacy_tool_bindings(report)
            record_audit(
                self.store,
                ToolApprovalEvent(
                    kind="legacy_stamped",
                    actor=by,
                    details={
                        "bindings": len(report.bindings),
                        "workflows": report.workflows,
                        "versions": report.versions,
                    },
                ),
            )
        return report

    def resolve_workflow(self, workflow_id: str, version: int | None) -> tuple[Workflow, int | None]:
        """A saved workflow in one of its versions (``None``: the latest), with the version's number.

        Sub-workflows run in a fixed version, so later changes never alter a run that started.
        """
        current = self.store.get("workflows", workflow_id, Workflow)
        if current is None:
            raise KeyError(workflow_id)
        if version is None:
            versions = self.versions.all(workflow_id)
            if not versions:
                return current, None
            chosen = versions[-1]
        else:
            chosen = self.versions.get(workflow_id, version)
        workflow = Workflow(
            **chosen.snapshot.model_dump(), id=workflow_id, created_at=current.created_at, updated_at=chosen.created_at
        )
        return workflow, chosen.number

    def current_version(self, workflow: Workflow) -> int | None:
        """The number of the saved version equal to ``workflow`` (``None`` for unsaved changes)."""
        found = next((item for item in self.versions.summaries(workflow) if item.current), None)
        return found.number if found is not None else None

    def tool_marker(self, name: str) -> str:
        """What identifies the current code of a tool; changes when the tool changes."""
        if name in self.tools.builtin:
            return f"builtin:{name}"
        try:
            info = self.tools.get(name)
        except KeyError:
            return f"{name}:missing"
        digest = hashlib.sha256((info.code or "").encode()).hexdigest()[:12]
        return f"{name}:v{info.version}:{digest}"

    def consent_means(self, config: AgentConfig) -> dict[str, Any]:
        """What ``config`` uses outside its own configuration, for the digest of a memory consent (RT-089d,
        :mod:`.memory_consents`), by tool: the tool's version and code (:meth:`tool_marker`), the bound connection's
        type, provider, settings, reachable addresses and secret (sealed, so a new secret changes it; never the secret
        itself), and for an MCP tool its server's address, transport, command and sign-in kind and the tool's
        definition. A change of any of them makes a consent stale, so content allowed for one destination never
        reaches another."""
        found: dict[str, Any] = {}
        for name in sorted(set(config.tools)):
            entry: dict[str, Any] = {"tool": self.tool_marker(name)}
            bound = config.tool_settings.get(name)
            if bound is not None and bound.connection_id:
                connection = self.connections.find(bound.connection_id)
                entry["connection"] = (
                    None
                    if connection is None
                    else {
                        "id": connection.id,
                        "type": connection.type,
                        "provider": connection.provider,
                        "settings": connection.settings,
                        "egress": sorted(connection.egress),
                        "secret": _sealed_digest(connection.secret_sealed, connection.secret_storage),
                    }
                )
            if is_mcp_tool(name):
                server_tool = self.mcp.find(name)
                entry["mcp"] = None if server_tool is None else _mcp_marker(*server_tool)
                if server_tool is not None and server_tool[0].transport == "stdio":
                    # Welle 5: a local server's approved packages (the lock of its ``mcp:`` environment) too.
                    lock = self._lock_digest(scope_of("mcp", server_tool[0].id))
                    if lock is not None:
                        entry["mcp"]["environment"] = lock
            found[name] = entry
        # Welle 5 (RT-089d): the address of every model server the agent sends to, for providers with a configurable
        # one (an OpenAI-compatible server, Ollama, vLLM): content allowed for one server never reaches another. Keyed
        # ``model:<provider>`` (no tool name has a colon), only for such providers, so other digests stay the same.
        models = [config.model, *([config.quality.judge_model] if config.quality.enabled else [])]
        stored = self._providers(sealed=True)
        for model in models:
            try:
                provider = provider_of(model)
            except NodestroError:
                continue
            info = PROVIDERS.get(provider)
            if info is not None and info.base_url:
                found[f"model:{provider}"] = {"endpoint": (stored.get(provider, {}).get("base_url") or None)}
        return found

    def consent_environment(self, workflow_id: str) -> str | None:
        """The approved Python environment a workflow's custom tools run in, for the digest of a memory consent
        (RT-089d, Welle 5): the SHA-256 of its lock (the exact versions of the libraries the tools import), ``None``
        when the workflow has none. Approving other library versions makes a consent stale."""
        return self._lock_digest(scope_of("workflow", workflow_id))

    def _lock_digest(self, scope: str) -> str | None:
        """The SHA-256 of the approved lock of a Python environment (``None``: none)."""
        env = self.python.get(scope)
        if env is None or not env.lock:
            return None
        return hashlib.sha256(f"{env.python_version}\n{env.lock}".encode()).hexdigest()

    # ---- running ----------------------------------------------------------------------

    async def run(
        self, workflow: Workflow, request: RunIn, *, trigger: RunTrigger = "manual", started_by: str | None = None
    ) -> Run:
        """Start a run the way the studio does (pre-check, providers, approved libraries).

        Returns the queued run at once; :meth:`wait` waits for it to stop.
        """
        return await launch(self, workflow, request, trigger=trigger, started_by=started_by)

    async def wait(self, run_id: str, *, timeout: float | None = None) -> Run:
        """The run once it finished or paused (waiting for a person); raises :class:`KeyError` for an unknown id."""
        found = await asyncio.wait_for(self.runs.wait(run_id), timeout)
        if found is None:
            raise KeyError(run_id)
        return found

    def decide(self, run_id: str, node_id: str, decision: str, comment: str = "", *, agent_note: str = "") -> Run:
        """Decide a waiting approval (``approved`` or ``rejected``); the run continues by itself.

        ``comment`` is for people only; ``agent_note`` is the only text later agents read (E-0R-1 A)."""
        return self.runs.decide(run_id, node_id, decision, comment, agent_note=agent_note)

    def cancel(self, run_id: str) -> bool:
        """Ask a running run to stop; ``False`` when it is not running."""
        return self.runs.cancel(run_id)

    async def resume(
        self, run_id: str, *, mode: Literal["continue", "restart"] = "continue", confirm: bool = False
    ) -> Run:
        """Continue a failed or cancelled run, reusing every result that is still valid."""
        return await resume_run(self, run_id, mode=mode, confirm=confirm)

    # ---- providers ------------------------------------------------------------------

    def connection(self, provider: str) -> Connection:
        """How agents of ``provider`` reach it; raises :class:`MissingApiKeyError` when it is not configured.

        A key from the environment wins over a stored one. In the strict offline mode a configured cloud provider is
        refused with ``check.offline_cloud_model`` before any request (assistants, architects, run chat answers and
        orchestrator decisions all connect here; the workflow check refuses runs earlier).
        """
        info = PROVIDERS.get(provider)
        if info is None:
            raise NodestroError("provider.unknown", f"Unknown provider {provider!r}", provider=provider)
        stored = self._providers().get(provider, {})
        if info.auth == "aws":
            return self._refuse_offline(self._bedrock_connection(stored), info.label)
        key = env_key(provider, self._environ)[0] or stored.get("api_key") or None
        base_url = stored.get("base_url") or None
        reported = None
        known: list[bool | None] = []

        def accelerated() -> bool | None:
            """Whether the local models run on a GPU, asked once per connection (it may ask the server)."""
            if not known:
                known.append(self.local_models.accelerated())
            return known[0]

        if provider == "ollama":
            reported = self.local_models.capability_overrides
            if base_url is None and self._client_factory is None:
                # The server Nodestro provides: the managed one (started now if needed) or the container's.
                try:
                    base_url = self.local_models.openai_url()
                except NodestroError as exc:
                    raise MissingApiKeyError(provider, code=exc.code, message=str(exc)) from None
                if base_url is None and self.local_models.mode == "managed":
                    raise MissingApiKeyError(
                        provider,
                        code="local.runtime_missing",
                        message="The local model runtime is not installed: set it up in the settings (local models)",
                    )
        if base_url and info.base_url:
            check_endpoint_allowed(base_url, self._endpoints)  # also a URL stored before the operator changed the list
        client = self._client_factory(provider) if self._client_factory is not None else None
        if client is None and base_url and info.base_url and (self._endpoints is not None or provider == "ollama"):
            # A server: the endpoint was checked against the allowlist, and a redirect must not lead elsewhere.
            # A local model server gets more time per request than the SDK's default, taken from its hardware, and
            # no SDK retries (RT-058/RT-068; local_request_timeout).
            local = provider == "ollama"
            client = endpoint_client(
                base_url,
                key,
                timeout=local_request_timeout(accelerated()) if local else None,
                max_retries=LOCAL_MAX_RETRIES if local else None,
            )
        if client is None and ((info.needs_key and not key) or (info.base_url and not base_url)):
            raise MissingApiKeyError(provider)
        return self._refuse_offline(
            Connection(
                provider=provider,
                api_key=key,
                base_url=base_url if info.base_url else None,
                capabilities=dict(stored.get("capabilities") or {}),
                client=client,
                reported=reported,
                reasoning_saving=provider == "ollama" and self.reasoning_saving(accelerated()),
                limits=dict(stored.get("limits") or {}),
                window=self._local_window if provider == "ollama" else None,
                reasoning_summaries=bool(self._stored("reasoning_summaries")),
            ),
            info.label,
        )

    def _local_window(self) -> int | None:
        """The context window of the local model server (configured, or read from a loaded chat model); ``None`` when
        it is not known or the server does not answer (never starts the managed server)."""
        try:
            return self.local_models.context_window()
        except (NodestroError, OSError, ConfigurationError):  # e.g. a stored address the client refuses
            return None

    def _refuse_offline(self, connection: Connection, label: str) -> Connection:
        """``connection``, unless the strict offline mode is on and it is not the local model server (D12).

        Every provider but Ollama counts as a cloud model here, also an OpenAI-compatible or vLLM server on this
        computer, as in the workflow check (``check.offline_cloud_model``): Nodestro cannot tell where it sends data.
        An unconfigured provider keeps its own error (``MissingApiKeyError``), which is raised before this."""
        if connection.provider != "ollama" and self.strict_offline():
            raise _offline_cloud_model(label, connection.provider)
        return connection

    def local_model_server_here(self) -> bool:
        """Whether the Ollama server agents reach is on this computer (N-20, review M2): a saved base URL or an external
        server whose host is not loopback is elsewhere, so a workflow using it reaches outside. The managed server and
        the container's (``local-container`` profile) are Nodestro's own."""
        stored = self._providers().get("ollama", {}).get("base_url")
        if stored:
            return _loopback(str(stored))
        service = self.local_models
        if service.mode == "external":
            return _loopback(service.ollama_url())
        return True

    def provider_settings(self, provider: str) -> dict[str, Any]:
        """The stored settings of one provider (may contain its key: never return it from an endpoint)."""
        return dict(self._providers().get(provider, {}))

    def secret_keys(self) -> list[str]:
        """Every API key Nodestro knows (environment and settings), e.g. to mask them in snapshots."""
        stored = self._providers()
        keys = [env_key(name, self._environ)[0] or "" for name in PROVIDERS]
        keys += [str(item.get("api_key") or "") for item in stored.values()]
        return [key for key in dict.fromkeys(keys) if key]

    def _local_mcp_servers(self) -> set[str]:
        """The local (``stdio``) MCP servers: they run on this computer, so strict offline does not refuse them."""
        return {server.id for server in self.mcp.servers() if server.transport == "stdio"}

    def connect_model(
        self,
        model: str,
        *,
        actor: str = "system",
        attended: bool = True,
        probe: bool = False,
        offline: OfflineScope | None = None,
        shared_content: bool = False,
    ) -> Connection:
        """The connection of a model's provider; a cloud model in the strict offline mode is refused first (also
        when its provider is not configured: a key would not help), then one the access model v2 blocks (N-06).

        ``actor``: who asks (the audit, and whether the error names the source). ``attended``: ``False`` when nobody
        waits for the answer (a benchmark judge), so unattended denies and limits apply. ``probe``: only asks whether
        the model can be used (model choice): a refusal is not audited. The connection test of the diagnostics
        (``connection(provider)``) is not checked: it sends no content.

        ``offline`` (N-09): the content to send is offline content (:meth:`RunManager.content_offline_scope`), so only
        a local model on this computer is connected; a cloud model is refused before its connection is resolved.

        ``shared_content`` (N-08): the content to send holds content of a granted source
        (:meth:`RunManager.content_granted`), so a cloud model needs a grant (only while the provenance switch is
        on)."""
        provider = provider_of(model)
        if provider != "ollama" and self.strict_offline():
            raise _offline_cloud_model(model, provider)
        if offline is not None:
            check_provider(provider, offline)
        # N-06: a cloud model the access model v2 blocks is refused before anything is sent (only while it is on).
        self.runs.check_cloud_access(
            provider, actor=actor, attended=attended, audit=not probe, shared_content=shared_content
        )
        connection = self.connection(provider)
        if offline is not None:
            check_connection(connection, offline, docker=self.builder.mode == "local-container")
        return connection

    def allowed_models(self) -> list[str]:
        """The models the assistants may choose for new agents: the defaults and the suggested models
        of every configured provider (qualified names)."""
        settings = self.settings()
        configured = {item.id for item in settings.providers if self._reachable(item.id)}
        found = [qualify(settings.default_model)]
        found += [info.id for info in models() if info.recommended and info.provider in configured]
        return list(dict.fromkeys(found))

    def _reachable(self, provider: str) -> bool:
        try:
            self.connection(provider)
        except NodestroError:
            return False
        return True

    def require_providers(self, wanted: set[str]) -> None:
        """Fail fast before work starts: every provider of ``models`` must be configured."""
        for provider in sorted({provider_of(model) for model in wanted}):
            self.connection(provider)


def _offline_cloud_model(model: str, provider: str) -> NodestroError:
    """The refusal of a cloud model in the strict offline mode (the workflow check's code and wording)."""
    return NodestroError(
        "check.offline_cloud_model",
        f"{model} needs the internet; the strict offline mode allows local models only",
        model=model,
        provider=provider,
    )


__all__ = ["DATABASE", "LOCAL_TENANT", "MissingApiKeyError", "Workspace", "WorkspaceStorage"]
