"""The local model server of a workspace: provisioning, status, models and jobs (Ollama through ``orkexa.local``).

Where the server comes from depends on the operating profile:

* **desktop**: a *managed* Ollama, a child process of Nodestro on a free loopback port with its own model folder
  (``<data dir>/local-models``) and ``OLLAMA_NO_CLOUD=1``. Its runtime is bundled with the desktop app
  (``NODESTRO_OLLAMA_BINARY``) or installed on an explicit request, from a verified release archive (download or a
  local file). An Ollama the user runs already is only used after they choose it (the Ollama provider's base URL),
  never started, stopped or reconfigured.
* **local-container** (Nodestro Local Docker): a separate Ollama container on an internal network
  (``NODESTRO_OLLAMA_URL``), its model folder shared with Nodestro (``NODESTRO_OLLAMA_MODELS_DIR``) for offline
  imports.
* **server**: no local server. The operator provides model endpoints (the Ollama provider's base URL, checked against
  ``NODESTRO_MODEL_ENDPOINTS``).

Nothing starts or downloads on its own when Nodestro is imported or a workspace opens: the API starts the managed
server when the application starts (if it is installed and not switched off) or when an agent first needs it.
Downloads (runtime, models) are separate, explicit jobs, refused in the strict offline mode.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Literal
import uuid

from orkexa.local import (
    ManagedOllama,
    OllamaClient,
    OllamaError,
    check_model_name,
    detect_ollama,
    import_package,
    install_runtime,
    installed_runtime,
    scan_store,
    verify_package,
)
from pydantic import BaseModel, Field

from .local_models import (
    CATALOG,
    OLLAMA_VERSION,
    Hardware,
    Recommendation,
    SelectionPreferences,
    catalog_model,
    detect_hardware,
    recommend,
)
from .problems import NodestroError

logger = logging.getLogger(__name__)

Mode = Literal["managed", "container", "external", "unavailable"]
JobKind = Literal["pull", "install_runtime", "import_package", "import_gguf", "move_models"]
JobState = Literal["running", "done", "failed", "cancelled"]

#: Where the local model settings are stored (one document; the app settings stay untouched).
SETTINGS_COLLECTION = "settings"
SETTINGS_ID = "local_models"
#: How long ``/api/show`` results are reused (they change only with a pull or delete, which clear the cache).
_SHOW_TTL = 600.0


class InstalledModel(BaseModel):
    name: str
    size: int
    digest: str
    kind: Literal["chat", "embedding"]
    #: What the installed model reports in ``/api/show`` (``completion``, ``tools``, ``vision``, ``embedding``, ...).
    capabilities: list[str] = Field(default_factory=list)
    context_length: int | None = None
    embedding_length: int | None = None
    #: Part of Nodestro's catalog, and whether its digest is the verified one of the catalog.
    catalog: bool = False
    verified: bool = False


class LocalJob(BaseModel):
    id: str
    kind: JobKind
    target: str
    state: JobState = "running"
    completed: int = 0
    total: int | None = None
    message: str = ""
    error_code: str | None = None
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class LocalStatus(BaseModel):
    """The state of the local model server of this workspace."""

    mode: Mode
    #: Whether agents can use local models now (a server answers).
    running: bool = False
    #: Ollama's own address (without ``/v1``); loopback or the internal container network.
    base_url: str | None = None
    version: str | None = None
    pinned_version: str = OLLAMA_VERSION
    #: Managed mode: whether the runtime is installed (or bundled) and where it is.
    runtime_installed: bool = False
    runtime_bundled: bool = False
    runtime_path: str | None = None
    #: Whether Nodestro starts the managed server with the application.
    autostart: bool = True
    models_dir: str | None = None
    #: Free space on the model folder's drive (bytes; ``None``: not known here).
    free_bytes: int | None = None
    models: list[InstalledModel] = Field(default_factory=list)
    #: An Ollama that already runs on this computer (port 11434) and its version; used only when chosen.
    detected_external: str | None = None
    #: The strict offline mode is on: downloads are refused, the managed server runs without a proxy.
    offline: bool = False
    error_code: str | None = None
    error: str | None = None


class LocalModelService:
    """The local model server of one workspace (see the module documentation)."""

    def __init__(
        self,
        data_dir: Path,
        profile: str,
        *,
        store: Any,
        offline: Callable[[], bool],
        external_url: Callable[[], str | None],
        environ: Mapping[str, str] | None = None,
        hardware: Callable[[], Hardware] = detect_hardware,
        client_factory: Callable[[str], OllamaClient] | None = None,
    ) -> None:
        env = os.environ if environ is None else environ
        self.profile = profile
        self.home = data_dir / "local-models"
        self.store = store
        self._offline = offline
        self._external_url = external_url
        self._hardware_source = hardware
        self._hardware: Hardware | None = None
        self._client_factory = client_factory or (lambda url: OllamaClient(url, allow_remote=True))
        bundled = env.get("NODESTRO_OLLAMA_BINARY") or ""
        self.bundled_binary = Path(bundled) if bundled else None
        self.container_url = (env.get("NODESTRO_OLLAMA_URL") or "").rstrip("/") or None
        shared = env.get("NODESTRO_OLLAMA_MODELS_DIR") or ""
        self.container_models_dir = Path(shared) if shared else None
        self._managed: ManagedOllama | None = None
        self._lock = threading.RLock()
        self._jobs: dict[str, LocalJob] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._show: dict[str, tuple[float, dict[str, Any]]] = {}

    # ---- settings ------------------------------------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        return dict(self.store.get_raw(SETTINGS_COLLECTION, SETTINGS_ID) or {})

    def _save(self, **changes: Any) -> None:
        values = self.settings()
        values.update(changes)
        self.store.put_raw(SETTINGS_COLLECTION, SETTINGS_ID, values)

    @property
    def mode(self) -> Mode:
        if self.profile == "server":
            return "external" if self._external_url() else "unavailable"
        if self.profile == "local-container":
            if self.container_url:
                return "container"
            return "external" if self._external_url() else "unavailable"
        # Desktop: a base URL the user saved for the Ollama provider means their own server.
        return "external" if self._external_url() else "managed"

    def models_dir(self) -> Path:
        if self.mode == "container" and self.container_models_dir is not None:
            return self.container_models_dir
        stored = self.settings().get("models_dir")
        return Path(stored) if stored else self.home / "models"

    def autostart(self) -> bool:
        return self.settings().get("autostart", True) is not False

    def set_autostart(self, value: bool) -> None:
        self._save(autostart=bool(value))

    # ---- the server ----------------------------------------------------------------------------------------

    def runtime_binary(self) -> Path | None:
        if self.bundled_binary is not None and self.bundled_binary.is_file():
            return self.bundled_binary
        return installed_runtime(self.home)

    def _managed_server(self) -> ManagedOllama:
        with self._lock:
            if self._managed is None:
                self._managed = ManagedOllama(
                    self.home, binary=self.runtime_binary(), models_dir=self.models_dir(), offline=self._offline()
                )
            return self._managed

    def ollama_url(self) -> str | None:
        """Ollama's address agents use now (``None``: no server; the managed one is not started here)."""
        mode = self.mode
        if mode == "container":
            return self.container_url
        if mode == "external":
            url = self._external_url() or ""
            return url.removesuffix("/v1").rstrip("/") or None
        if mode == "managed" and self._managed is not None:
            return self._managed.base_url
        return None

    def openai_url(self, *, start: bool = True) -> str | None:
        """The OpenAI-compatible base URL (``.../v1``) of the server Nodestro provides (managed or container).

        ``start`` starts the managed server when it is installed and not running. ``None`` in the ``external`` mode
        (the provider's saved base URL is used as it is) and when no server can be provided."""
        mode = self.mode
        if mode == "container":
            return f"{self.container_url}/v1"
        if mode != "managed":
            return None
        if self._managed is not None and self._managed.base_url:
            return f"{self._managed.base_url}/v1"
        if not start or self.runtime_binary() is None:
            return None
        return f"{self.start()}/v1"

    def provides_endpoint(self) -> bool:
        """Whether Nodestro provides the Ollama endpoint itself (managed with a runtime, or the container)."""
        mode = self.mode
        return mode == "container" or (mode == "managed" and self.runtime_binary() is not None)

    def start(self) -> str:
        """Start the managed server (desktop) and return Ollama's address; idempotent."""
        if self.mode != "managed":
            raise NodestroError("local.not_managed", "Nodestro does not manage the local model server here")
        if self.runtime_binary() is None:
            raise NodestroError(
                "local.runtime_missing", "The local model runtime is not installed", version=OLLAMA_VERSION
            )
        server = self._managed_server()
        server._binary = self.runtime_binary()
        try:
            return server.start()
        except OllamaError as exc:
            raise _error(exc) from None

    def stop(self) -> None:
        with self._lock:
            if self._managed is not None:
                self._managed.stop()

    def restart(self) -> None:
        with self._lock:
            if self._managed is not None:
                self._managed.stop()
                self._managed = None
        if self.mode == "managed" and self.runtime_binary() is not None:
            self.start()

    def start_on_launch(self) -> None:
        """What the application does when it starts: the managed server, if installed and not switched off."""
        if self.mode == "managed" and self.autostart() and self.runtime_binary() is not None:
            try:
                self.start()
            except NodestroError as exc:
                logger.warning("The local model server did not start: %s", exc)

    def close(self) -> None:
        for event in self._cancel.values():
            event.set()
        self.stop()

    def client(self) -> OllamaClient:
        url = self.ollama_url()
        if url is None and self.mode == "managed" and self.runtime_binary() is not None:
            url = self.start()
        if url is None:
            raise NodestroError("local.not_running", "No local model server is running")
        return self._client_factory(url)

    def hardware(self) -> Hardware:
        if self._hardware is None:
            self._hardware = self._hardware_source()
        return self._hardware

    def status(self) -> LocalStatus:
        mode = self.mode
        status = LocalStatus(mode=mode, offline=self._offline(), autostart=self.autostart())
        if mode == "managed":
            binary = self.runtime_binary()
            status.runtime_installed = binary is not None
            status.runtime_bundled = binary is not None and binary == self.bundled_binary
            status.runtime_path = str(binary) if binary else None
            status.detected_external = detect_ollama()
        if mode in ("managed", "container"):
            folder = self.models_dir()
            status.models_dir = str(folder)
            status.free_bytes = _free_bytes(folder)
        url = self.ollama_url()
        status.base_url = url
        if url is None:
            return status
        try:
            client = self._client_factory(url)
            status.version = client.version()
            status.running = True
            status.models = self.installed(client)
        except OllamaError as exc:
            status.error_code, status.error = exc.code, str(exc)
        return status

    # ---- models --------------------------------------------------------------------------------------------

    def installed(self, client: OllamaClient | None = None) -> list[InstalledModel]:
        client = client or self.client()
        try:
            found = client.list_models()
        except OllamaError as exc:
            raise _error(exc) from None
        result = []
        for model in found:
            if model.remote:
                continue  # cloud models never count as local
            details = self.details(model.name, client=client)
            capabilities = list(details.get("capabilities") or model.capabilities)
            entry = catalog_model(model.name)
            result.append(
                InstalledModel(
                    name=model.name,
                    size=model.size,
                    digest=model.digest,
                    kind="embedding" if "embedding" in capabilities else "chat",
                    capabilities=capabilities,
                    context_length=details.get("context_length") or model.context_length,
                    embedding_length=details.get("embedding_length") or model.embedding_length,
                    catalog=entry is not None,
                    verified=entry is not None and entry.digest == model.digest,
                )
            )
        return result

    def details(self, name: str, *, client: OllamaClient | None = None) -> dict[str, Any]:
        """``/api/show`` of an installed model: capabilities, context, Orkexa capability overrides (cached)."""
        name = name.removeprefix("ollama:")
        cached = self._show.get(name)
        if cached is not None and time.monotonic() - cached[0] < _SHOW_TTL:
            return cached[1]
        try:
            details = (client or self.client()).show(name)
        except OllamaError as exc:
            if exc.code == "model_missing":
                return {}
            raise _error(exc) from None
        value = {
            "capabilities": list(details.capabilities),
            "context_length": details.context_length,
            "embedding_length": details.embedding_length,
            "overrides": details.capability_overrides(),
        }
        self._show[name] = (time.monotonic(), value)
        return value

    def capability_overrides(self, model: str) -> dict[str, str]:
        """The Orkexa capabilities an installed model reports (``{}`` when no server answers or it is missing)."""
        try:
            return dict(self.details(model).get("overrides") or {})
        except NodestroError:
            return {}

    def recommend(self, task: str, preferences: SelectionPreferences | None = None) -> Recommendation:
        installed: list[InstalledModel] = []
        with suppress(NodestroError):  # no server: recommend from the catalog alone
            installed = self.installed()
        return recommend(
            task,
            self.hardware(),
            [model.name for model in installed],
            capabilities={model.name: model.capabilities for model in installed},
            preferences=preferences,
        )

    def delete(self, name: str) -> None:
        name = _model_name(name)
        try:
            self.client().delete(name)
        except OllamaError as exc:
            raise _error(exc) from None
        self._show.pop(name, None)

    # ---- jobs ----------------------------------------------------------------------------------------------

    def jobs(self) -> list[LocalJob]:
        return sorted(self._jobs.values(), key=lambda job: job.started_at, reverse=True)

    def job(self, job_id: str) -> LocalJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise NodestroError("local.job_missing", f"No local model job {job_id}", job=job_id)
        return job

    def cancel(self, job_id: str) -> LocalJob:
        job = self.job(job_id)
        event = self._cancel.get(job_id)
        if event is not None:
            event.set()
        return job

    def _refuse_offline(self, what: str) -> None:
        if self._offline():
            raise NodestroError(
                "local.offline_download",
                f"The strict offline mode is on: {what} needs the internet; import it from a file instead",
            )

    def _busy(self, kind: JobKind, target: str) -> None:
        for job in self._jobs.values():
            if job.state == "running" and job.kind == kind and job.target == target:
                raise NodestroError("local.job_running", f"{target} is already being processed", job=job.id)

    def _run(self, kind: JobKind, target: str, work: Callable[[LocalJob, threading.Event], None]) -> LocalJob:
        with self._lock:
            self._busy(kind, target)
            job = LocalJob(id=uuid.uuid4().hex[:16], kind=kind, target=target, started_at=datetime.now(UTC))
            cancel = threading.Event()
            self._jobs[job.id] = job
            self._cancel[job.id] = cancel

        def body() -> None:
            try:
                work(job, cancel)
                job.state = "cancelled" if cancel.is_set() else "done"
            except OllamaError as exc:
                job.state = "cancelled" if exc.code == "cancelled" or cancel.is_set() else "failed"
                job.error_code, job.error = f"local.{exc.code}", str(exc)
            except NodestroError as exc:
                job.state = "failed"
                job.error_code, job.error = exc.code, str(exc)
            except Exception as exc:  # reported on the job; never stops the application
                logger.exception("Local model job %s failed", job.id)
                job.state = "failed"
                job.error_code, job.error = "local.job_failed", str(exc)
            finally:
                job.finished_at = datetime.now(UTC)
                self._cancel.pop(job.id, None)
                self._show.clear()

        threading.Thread(target=body, name=f"nodestro-local-{kind}", daemon=True).start()
        return job

    def pull(self, name: str) -> LocalJob:
        """Download a model (needs the internet; refused in the strict offline mode)."""
        name = _model_name(name)
        self._refuse_offline("downloading a model")
        client = self.client()

        def work(job: LocalJob, cancel: threading.Event) -> None:
            for item in client.pull(name, cancel=cancel):
                job.message = item.status
                if item.total:
                    job.total, job.completed = item.total, item.completed or 0

        return self._run("pull", name, work)

    def install_runtime(self, *, archive: str | None = None, download: bool = False) -> LocalJob:
        """Install the pinned runtime from a local release archive, or download it (desktop only)."""
        if self.mode != "managed":
            raise NodestroError("local.not_managed", "Nodestro does not manage the local model server here")
        if archive is None and not download:
            raise NodestroError("local.archive_or_download", "Choose a runtime archive or allow the download")
        if download:
            self._refuse_offline("downloading the runtime")
        source = Path(archive).expanduser() if archive else None
        if source is not None and not source.is_file():
            raise NodestroError("local.file_missing", f"{source} does not exist", path=str(source))

        def work(job: LocalJob, cancel: threading.Event) -> None:
            def progress(done: int, total: int | None) -> None:
                job.completed, job.total = done, total

            install_runtime(self.home, archive=source, download=download, progress=progress, cancel=cancel)
            with self._lock:
                self._managed = None  # the next start uses the new binary
            if self.autostart():
                self.start()

        return self._run("install_runtime", OLLAMA_VERSION, work)

    def import_package(self, path: str, names: list[str] | None = None) -> LocalJob:
        """Import an offline model package (checks every file's SHA-256; no network)."""
        source = _existing_dir(path)
        verify_package(source, hashes=False)  # structure and names now; checksums while copying
        target = self.models_dir()
        if self.mode not in ("managed", "container"):
            raise NodestroError(
                "local.not_managed", "Import into your own Ollama server with its own tools (orkexa-local import)"
            )

        def work(job: LocalJob, cancel: threading.Event) -> None:
            def progress(done: int, total: int) -> None:
                job.completed, job.total = done, total

            report = import_package(source, target, names=names, progress=progress, cancel=cancel)
            job.message = ", ".join(report.imported)

        return self._run("import_package", str(source), work)

    def import_gguf(self, name: str, path: str, *, projector: str | None = None) -> LocalJob:
        """Create a model from a local GGUF file (and a vision projector); no network."""
        name = _model_name(name)
        gguf = Path(path).expanduser()
        if not gguf.is_file() or gguf.suffix.lower() != ".gguf":
            raise NodestroError("local.not_gguf", f"{gguf} is not a GGUF file", path=str(gguf))
        extra = Path(projector).expanduser() if projector else None
        if extra is not None and not extra.is_file():
            raise NodestroError("local.file_missing", f"{extra} does not exist", path=str(extra))
        client = self.client()

        def work(job: LocalJob, cancel: threading.Event) -> None:
            job.total = gguf.stat().st_size + (extra.stat().st_size if extra else 0)
            for item in client.create_from_gguf(name, gguf, projector=extra):
                if cancel.is_set():
                    raise OllamaError("cancelled", "The import was cancelled")
                job.message = item.status

        return self._run("import_gguf", name, work)

    def set_models_dir(self, path: str, *, transfer: Literal["use", "copy"]) -> LocalJob | None:
        """Move the model folder of the managed server, e.g. to an external drive.

        ``use``: use the folder as it is (an existing Ollama model folder, or an empty one). ``copy``: copy the current
        models there first, check them and switch afterwards; the old folder is kept (never deleted here)."""
        if self.mode != "managed":
            raise NodestroError("local.not_managed", "Only the model folder of the managed server can be changed")
        target = Path(path).expanduser()
        if not target.is_absolute():
            raise NodestroError("local.path_relative", "Choose an absolute folder", path=str(target))
        current = self.models_dir()
        if target.resolve() == current.resolve():
            return None
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NodestroError(
                "local.models_dir", f"The folder {target} cannot be used: {exc}", path=str(target)
            ) from None
        if not os.access(target, os.W_OK):
            raise NodestroError("local.models_dir", f"The folder {target} is not writable", path=str(target))
        if transfer == "use":
            if (target / "blobs").is_dir():
                scan_store(target)  # refuses a folder that is not an Ollama model folder
            self._switch_models_dir(target)
            return None

        def work(job: LocalJob, cancel: threading.Event) -> None:
            models = scan_store(current) if (current / "blobs").is_dir() else []
            job.total = sum(model.size for model in models)
            if models:
                needed = job.total
                free = _free_bytes(target)
                if free is not None and free < needed:
                    raise NodestroError(
                        "local.disk_full", f"{target} has too little free space", needed=needed, free=free
                    )
                _copy_store(current, target, job, cancel)
                copied = {model.name: model.digest for model in scan_store(target)}
                for model in models:
                    if copied.get(model.name) != model.digest:
                        raise NodestroError("local.copy_failed", f"{model.name} was not copied completely")
            self._switch_models_dir(target)

        return self._run("move_models", str(target), work)

    def _switch_models_dir(self, target: Path) -> None:
        with self._lock:
            running = self._managed is not None and self._managed.base_url is not None
            self._save(models_dir=str(target))
            if self._managed is not None:
                self._managed.stop()
                self._managed = None
        self._show.clear()
        if running:
            self.start()


def _copy_store(source: Path, target: Path, job: LocalJob, cancel: threading.Event) -> None:
    """Copy an Ollama model folder: blobs first (skipping complete ones), the manifests last."""
    blobs = target / "blobs"
    blobs.mkdir(exist_ok=True)
    for blob in sorted((source / "blobs").iterdir()):
        if cancel.is_set():
            raise OllamaError("cancelled", "Copying the models was cancelled")
        if not blob.is_file() or blob.name.endswith(".partial"):
            continue
        destination = blobs / blob.name
        if not (destination.is_file() and destination.stat().st_size == blob.stat().st_size):
            partial = destination.with_name(destination.name + ".partial")
            shutil.copyfile(blob, partial)
            os.replace(partial, destination)
        job.completed += blob.stat().st_size
    for layout in ("manifests", "manifests-v2"):
        if (source / layout).is_dir():
            shutil.copytree(source / layout, target / layout, symlinks=True, dirs_exist_ok=True)


def _free_bytes(folder: Path) -> int | None:
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _existing_dir(path: str) -> Path:
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise NodestroError("local.folder_missing", f"{folder} is not a folder", path=str(folder))
    return folder


def _model_name(name: str) -> str:
    name = name.removeprefix("ollama:").strip()
    try:
        check_model_name(name)
    except OllamaError as exc:
        raise _error(exc) from None
    return name


def _error(exc: OllamaError) -> NodestroError:
    return NodestroError(f"local.{exc.code}", str(exc), status=str(exc.status or ""))


__all__ = [
    "CATALOG",
    "InstalledModel",
    "LocalJob",
    "LocalModelService",
    "LocalStatus",
]
