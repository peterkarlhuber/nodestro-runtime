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
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import sys
from typing import Any, Literal

from orkexa.dynamic import DockerSandbox, Sandbox
from orkexa.providers.base import redact

from .agent_builder import AgentBuilder
from .agent_test import AgentTester
from .builtin_tools import BUILTIN_TOOLS
from .catalog import models
from .container_images import ContainerImages, RuntimeImageSandbox
from .engine import RunManager
from .extensions import Extensions, Integration, discover
from .input_files import FileCatalog, InputFiles
from .knowledge import KnowledgeBase, KnowledgeTables
from .launcher import launch
from .memory_spaces import MemorySpaces, MemoryTables
from .pricing import PricingService
from .problems import NodestroError
from .profile import RuntimeProfile
from .providers import (
    PROVIDERS,
    Connection,
    MissingApiKeyError,
    check_base_url,
    check_capabilities,
    check_endpoint_allowed,
    check_model,
    env_key,
    provider_of,
    provider_status,
    qualify,
)
from .python_envs import PythonEnvironments
from .python_tools import EnvironmentGate
from .reports import ResultFiles
from .resume import PublishedRuns
from .resume import resume as resume_run
from .sandbox_policy import SandboxPolicy
from .schemas import (
    DEFAULT_AGENT_MODEL,
    DEFAULT_ASSISTANT_MODEL,
    LANGUAGES,
    Run,
    RunIn,
    RunTrigger,
    Settings,
    SettingsIn,
    Workflow,
    WorkflowIn,
)
from .server_settings import SecretBox, is_encrypted
from .storage import BeforeMigration, DocumentStore, SQLiteStore, Store
from .tool_drafts import ToolDrafts
from .tool_library import ToolLibrary, make_sandbox
from .tool_provenance import StoredProvenance
from .usage_log import UsageLog
from .versions import WorkflowVersions

#: The database file inside the data directory (unchanged since Nodestro 0.1).
DATABASE = "nodestro.db"
#: Held while a workspace is open: a second process on the same data directory would mark the first
#: one's runs as interrupted, clean up its installations and run every schedule a second time.
LOCK_FILE = "nodestro.lock"


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

    The copy is written under a temporary name and renamed when complete, so a backup that exists is whole."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target_path = directory / f"pre-migration-{current}-to-{target}-{stamp}.db"
    partial = target_path.with_suffix(".db.partial")
    try:
        store.snapshot(partial)
        partial.replace(target_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target_path


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
    secrets: SecretBox | None = None
    #: The model endpoints (origins) the operator allows for the endpoint providers (``compatible``, ``ollama``,
    #: ``vllm``); any other base URL is refused, so a tenant cannot make the server reach internal services.
    endpoints: tuple[str, ...] = ()
    #: The server's reference prices (shared by every tenant, written only by the server's price sync). ``None``:
    #: the tenant sees Orkexa's built-in prices until the server provides them. Tenants never write to it.
    price_reference: DocumentStore | None = None


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
    ) -> None:
        """``client_factory(provider)`` replaces the SDK clients, ``python_runner`` uv, ``ocr_runner`` the
        OCR (tests). ``integrations`` replaces the installed ones (:func:`~nodestro_runtime.extensions.discover`).

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
        self.extensions = Extensions(
            discover() if integrations is None else integrations,
            strict=integrations is not None,
            reserved={tool.name for tool in BUILTIN_TOOLS if tool.name},
        )
        self.store: DocumentStore
        self._sqlite: SQLiteStore | None = None
        self._secrets: SecretBox | None = None
        if storage is None:
            database = data_dir / DATABASE
            self._sqlite = Store(database)
            self.store = self._sqlite
            self.tenant_id = LOCAL_TENANT
            try:
                self._sqlite.migrate(before_migration=before_migration or self._backup_before_migration)
            except BaseException:
                self._sqlite.close()
                if self._directory_lock is not None:
                    self._directory_lock.release()
                raise
            self.knowledge = KnowledgeBase(database, ocr=ocr_runner, engine=self.extensions.ocr)
            self.memory = MemorySpaces(database)
        else:
            self.store = storage.store
            self.tenant_id = storage.tenant_id
            self._secrets = storage.secrets
            self.knowledge = KnowledgeBase(storage.knowledge, ocr=ocr_runner, engine=self.extensions.ocr)
            self.memory = MemorySpaces(storage.memory)
        # A server's own environment variables belong to no tenant: its tenants use only their stored keys.
        self._environ: Mapping[str, str] | None = None if storage is None else {}
        self._endpoints: tuple[str, ...] | None = None if storage is None else storage.endpoints
        self.knowledge.ocr_enabled = lambda: bool(self._stored("ocr_enabled"))
        # A workspace on handed-in storage serves a tenant of a server: its tool code runs only in containers,
        # whatever the profile or a stored (for example imported) setting says (ADR-0006).
        self.sandbox_policy = SandboxPolicy("server" if storage is not None else self.profile.kind)
        sandbox = make_sandbox(self._sandbox_kind(), self.sandbox_policy, self._container_sandbox())
        self.tools = ToolLibrary(
            data_dir / "tools",
            sandbox=sandbox,
            extra_tools=self.extensions.tools(),
            policy=self.sandbox_policy,
            provenance=StoredProvenance(self.store),
        )
        self.builder = AgentBuilder(self.tools, self.knowledge, self.memory)
        self.builder.declared = lambda provider: dict(self._providers().get(provider, {}).get("capabilities") or {})
        self._client_factory = client_factory
        self._docker: bool | None = None
        self.runs = RunManager(
            self.store,
            self.builder,
            connect=self.connection,
            default_budget=lambda: self.settings().default_budget_usd,
            assistant_model=lambda: self.settings().assistant_model,
        )
        self.tester = AgentTester(
            self.builder, connect=self.connection, default_budget=lambda: self.settings().default_budget_usd
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
        self.versions = WorkflowVersions(self.store)
        self.tool_drafts = ToolDrafts(self.store, self.tools)
        self.tools.untrusted_sources = lambda: [draft.code for draft in self.tool_drafts.pending()]
        self.python = PythonEnvironments(
            self.store,
            data_dir / "python",
            self.tools,
            runner=python_runner,
            sandbox_kind=self._sandbox_kind,
            workflow_exists=lambda workflow_id: self.store.get_raw("workflows", workflow_id) is not None,
        )
        self.tools.dependencies = EnvironmentGate(self.python)
        self.tool_drafts.python = self.python
        self.runs.tool_sandbox = self.python.workflow_sandbox
        self.tester.tool_sandbox = self.python.workflow_sandbox
        self.runs.resolve_workflow = self.resolve_workflow
        self.runs.tool_marker = self.tool_marker
        # Files uploaded as run inputs: own storage, never knowledge by themselves (see input_files.py).
        self.inputs = InputFiles(self.store, data_dir / "inputs", ocr=self._input_ocr)
        self.runs.inputs = self.inputs
        self.runs.file_names = self.file_catalog
        self.files = ResultFiles(self.store)
        self.runs.after_run = self.files.write_outputs
        #: Runs of published releases (set by the HTTP API, which owns publishing; ``None``: none).
        self.published: PublishedRuns | None = None
        self.compose()
        # On a server, other processes execute this tenant's runs right now; runs whose worker died are found by
        # the job reaper (nodestro_runtime.jobs.reap), never by a process that merely opens the workspace.
        if storage is None:
            # Runs that were executing when Nodestro stopped can never finish; they become continuable.
            self.runs.recover()
            # The same for installations of Python environments (the last working environment is kept).
            try:
                self.python.recover()
            except Exception:  # housekeeping; it never stops Nodestro from starting
                logging.getLogger(__name__).exception("Recovering interrupted Python installations failed")
        # Uploads nobody used (e.g. a run dialog that was closed) are removed after a day.
        try:
            self.inputs.cleanup()
        except Exception:  # cleaning up is housekeeping; it never stops Nodestro from starting
            logging.getLogger(__name__).exception("Cleaning up input files failed")

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
        self.knowledge.close()
        self.memory.close()
        self.store.close()
        if self._directory_lock is not None:
            self._directory_lock.release()

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

    def _stored(self, key: str) -> Any:
        values = self.store.get_raw("settings", "app") or {}
        return values.get(key)

    def _container_sandbox(self) -> Callable[[], Sandbox] | None:
        """On a server, tool code without libraries runs in Nodestro's runtime image (no bind mounts, no image an
        operator builds by hand); the desktop keeps Orkexa's ``orkexa-sandbox`` image."""
        if self.sandbox_policy.profile == "desktop":
            return None
        # The Python environments are created after the tool library; the image is resolved on the first call.
        return lambda: RuntimeImageSandbox(
            lambda: self.python.runtime_image(), timeout=15.0, environ=ContainerImages()._cli_environment()
        )

    def _sandbox_kind(self) -> Literal["subprocess", "docker"]:
        """The sandbox setting in effect: the stored one, or ``docker`` wherever only containers may run code."""
        if self.sandbox_policy.profile != "desktop" or self._stored("sandbox") == "docker":
            return "docker"
        return "subprocess"

    def _providers(self, values: dict[str, Any] | None = None, *, sealed: bool = False) -> dict[str, dict[str, Any]]:
        """The stored settings per provider; a top-level ``api_key`` counts as Anthropic's key.

        With a secret box (server tenants), keys are stored encrypted and returned decrypted, unless ``sealed``. A key
        that cannot be decrypted (or was stored in plain text) counts as missing: it has to be entered again."""
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
        return providers

    def _seal(self, provider: str, key: str) -> str:
        if self._secrets is None:
            return key
        return self._secrets.encrypt(self.tenant_id, _key_field(provider), key)

    def settings(self) -> Settings:
        values = self.store.get_raw("settings", "app") or {}
        budget = values.get("default_budget_usd", 2.0)
        stored = self._providers(values)
        default_model = values.get("default_model") or DEFAULT_AGENT_MODEL
        return Settings(
            providers=[provider_status(name, stored.get(name, {}), self._environ) for name in PROVIDERS],
            default_provider=provider_of(default_model),
            default_model=default_model,
            assistant_model=values.get("assistant_model") or DEFAULT_ASSISTANT_MODEL,
            default_budget_usd=budget,
            sandbox=self._sandbox_kind(),
            docker_available=self._docker_available(),
            language=values.get("language") if values.get("language") in LANGUAGES else None,
            architect_budget_usd=values.get("architect_budget_usd", 2.0),
            architect_max_repairs=int(values.get("architect_max_repairs") or 0)
            if values.get("architect_max_repairs") is not None
            else 2,
            ocr_enabled=bool(values.get("ocr_enabled")),
            ocr_available=self.knowledge.ocr_available(),
        )

    def _docker_available(self) -> bool:
        if self._docker is None:
            self._docker = DockerSandbox.available()
        return self._docker

    def update_settings(self, update: SettingsIn) -> Settings:
        values = self.store.get_raw("settings", "app") or {}
        changes = update.model_dump(exclude_unset=True)
        if changes.get("sandbox") and changes["sandbox"] != "docker":
            self.sandbox_policy.check_construction("none")  # before anything is stored
        # A top-level ``api_key`` is Anthropic's key; it moves into the provider settings (once, idempotent).
        providers = self._providers(values, sealed=True)
        if self._secrets is not None:  # a key stored in plain text is never kept on a server (enter it again)
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
        values["providers"] = {name: entry for name, entry in providers.items() if entry}
        for key in ("default_model", "assistant_model"):
            if changes.get(key):
                changes[key] = check_model(changes[key])
        values.update(changes)
        self.store.put_raw("settings", "app", values)
        if changes.get("sandbox"):
            self.tools.set_sandbox(make_sandbox(changes["sandbox"], self.sandbox_policy, self._container_sandbox()))
        return self.settings()

    def reload_after_restore(self) -> None:
        """Pick up restored data that is cached in memory: the tool registry and its sandbox.

        A backup can come from anywhere, so the provenance of its tools is unknown: every restored custom tool
        becomes ``untrusted`` (ADR-0006)."""
        self.tools.provenance.clear()
        self.tools.reload(make_sandbox(self._sandbox_kind(), self.sandbox_policy, self._container_sandbox()))
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
            previous = self.store.get("workflows", saved.id, Workflow)
            self.store.put("workflows", saved.id, saved)
            self.versions.record(saved, previous=previous)
        return saved

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

    # ---- running ----------------------------------------------------------------------

    async def run(self, workflow: Workflow, request: RunIn, *, trigger: RunTrigger = "manual") -> Run:
        """Start a run the way the studio does (pre-check, providers, approved libraries).

        Returns the queued run at once; :meth:`wait` waits for it to stop.
        """
        return await launch(self, workflow, request, trigger=trigger)

    async def wait(self, run_id: str, *, timeout: float | None = None) -> Run:
        """The run once it finished or paused (waiting for a person); raises :class:`KeyError` for an unknown id."""
        found = await asyncio.wait_for(self.runs.wait(run_id), timeout)
        if found is None:
            raise KeyError(run_id)
        return found

    def decide(self, run_id: str, node_id: str, decision: str, comment: str = "") -> Run:
        """Decide a waiting approval (``approved`` or ``rejected``); the run continues by itself."""
        return self.runs.decide(run_id, node_id, decision, comment)

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

        A key from the environment wins over a stored one.
        """
        info = PROVIDERS.get(provider)
        if info is None:
            raise NodestroError("provider.unknown", f"Unknown provider {provider!r}", provider=provider)
        stored = self._providers().get(provider, {})
        key = env_key(provider, self._environ)[0] or stored.get("api_key") or None
        base_url = stored.get("base_url") or None
        if base_url and info.base_url:
            check_endpoint_allowed(base_url, self._endpoints)  # also a URL stored before the operator changed the list
        client = self._client_factory(provider) if self._client_factory is not None else None
        if client is None and ((info.needs_key and not key) or (info.base_url and not base_url)):
            raise MissingApiKeyError(provider)
        return Connection(
            provider=provider,
            api_key=key,
            base_url=base_url if info.base_url else None,
            capabilities=dict(stored.get("capabilities") or {}),
            client=client,
        )

    def provider_settings(self, provider: str) -> dict[str, Any]:
        """The stored settings of one provider (may contain its key: never return it from an endpoint)."""
        return dict(self._providers().get(provider, {}))

    def secret_keys(self) -> list[str]:
        """Every API key Nodestro knows (environment and settings), e.g. to mask them in snapshots."""
        stored = self._providers()
        keys = [env_key(name, self._environ)[0] or "" for name in PROVIDERS]
        keys += [str(item.get("api_key") or "") for item in stored.values()]
        return [key for key in dict.fromkeys(keys) if key]

    def connect_model(self, model: str) -> Connection:
        """The connection of a model's provider."""
        return self.connection(provider_of(model))

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


__all__ = ["DATABASE", "LOCAL_TENANT", "MissingApiKeyError", "Workspace", "WorkspaceStorage"]
