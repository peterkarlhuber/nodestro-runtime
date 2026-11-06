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

from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Literal
import uuid

from orkexa.local import (
    MDENSEON,
    GGUFRecipe,
    ManagedOllama,
    ModelDetails,
    OllamaClient,
    OllamaError,
    artifact_for,
    build_model,
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
from .local_progress import (
    BUILD_STEPS,
    PULL_STEPS,
    SETUP_STEPS,
    JobKind,
    JobLayer,
    JobState,
    JobStep,
    LocalJob,
    PullProgress,
    Tracker,
    build_step,
    retryable,
)
from .problems import NodestroError

logger = logging.getLogger(__name__)

Mode = Literal["managed", "container", "external", "unavailable"]

#: Where the local model settings are stored (one document; the app settings stay untouched).
SETTINGS_COLLECTION = "settings"
SETTINGS_ID = "local_models"
#: Where the recent jobs are kept, so their outcome survives a restart (a running one is then ``local.interrupted``).
JOBS_COLLECTION = "local_jobs"
JOBS_ID = "recent"
#: How many finished jobs are kept (in memory and stored).
_KEEP_JOBS = 30
#: How often a running job's progress is stored at most (its steps and its end are stored at once).
_STORE_EVERY = 5.0
#: How long ``/api/show`` results are reused (they change only with a pull or delete, which clear the cache).
_SHOW_TTL = 600.0
#: The context window of the managed server and the Local Docker container (``NODESTRO_OLLAMA_CONTEXT_LENGTH``).
#: Ollama's own default without a large GPU is 4096 tokens, shared by the prompt, the thinking and the answer: too
#: small for agents (a thinking model ran out of it after about 3,500 output tokens). 32768 costs about 1.1 GB more
#: memory with qwen3.5:4b (measured: 4.8 GB instead of 3.7 GB loaded).
LOCAL_CONTEXT_LENGTH = 32768


class ModelSpeed(BaseModel):
    """How fast an installed model answered here, measured by :meth:`LocalModelService.benchmark` (never estimated)."""

    #: Generated tokens per second (Ollama's ``eval_count`` / ``eval_duration``).
    tokens_per_second: float
    #: Prompt tokens read per second (``prompt_eval_count`` / ``prompt_eval_duration``), when Ollama reports it.
    prompt_tokens_per_second: float | None = None
    measured_at: datetime
    #: The context window the model was loaded with, and where it ran (``/api/ps``: share of it in GPU memory).
    context_length: int | None = None
    processor: Literal["gpu", "cpu", "mixed"] | None = None


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
    #: The last measured speed on this computer (``None``: not measured yet).
    speed: ModelSpeed | None = None


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
    #: The context window (tokens) models get: ``configured`` when Nodestro starts the server with it (managed,
    #: Local Docker), ``server`` when read from a model the server has loaded (``/api/ps``), else ``unknown``.
    context_length: int | None = None
    context_source: Literal["configured", "server", "unknown"] = "unknown"
    #: Whether models run on a GPU: from the models loaded now (``/api/ps``), else from this computer's hardware
    #: when the server runs here; ``None`` when not known (an external server on another computer).
    accelerated: bool | None = None
    error_code: str | None = None
    error: str | None = None


class LocalAdvice(BaseModel):
    """A hint about the local models of this computer; the interface shows its own text for the ``code``.

    ``value`` and ``recommended`` depend on the code: tokens for ``local.context_small``, tokens per second times ten
    for ``local.model_slow``, bytes for ``local.memory_low``."""

    code: str
    severity: Literal["info", "warning"] = "info"
    model: str | None = None
    value: int | None = None
    recommended: int | None = None


#: Below this context window agents run out of room (prompt, thinking and answer share it).
SMALL_CONTEXT = 16384
#: Below this measured speed (tokens per second) a model is slow for agent work.
SLOW_TOKENS_PER_SECOND = 8.0
#: Below this memory only the smallest models run.
LOW_MEMORY = 8 * 1024**3
_PS_TTL = 30.0
#: How long a model's ``/api/show`` that got no answer (timeout, unreachable) is not asked again.
_FAILED_TTL = 30.0
_UNANSWERED = frozenset({"timeout", "unreachable"})
_BENCHMARK_PROMPT = (
    "Write a short, plain paragraph (about eighty words) that explains why a team should write down the decisions it "
    "makes in meetings. Do not use lists."
)


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
        preferences: Callable[[], SelectionPreferences] | None = None,
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
        self._preferences = preferences or SelectionPreferences
        self._hardware_source = hardware
        self._hardware: Hardware | None = None
        self._client_factory = client_factory or (lambda url: OllamaClient(url, allow_remote=True))
        bundled = env.get("NODESTRO_OLLAMA_BINARY") or ""
        self.bundled_binary = Path(bundled) if bundled else None
        self.container_url = (env.get("NODESTRO_OLLAMA_URL") or "").rstrip("/") or None
        shared = env.get("NODESTRO_OLLAMA_MODELS_DIR") or ""
        self.context_length = _context_length(env.get("NODESTRO_OLLAMA_CONTEXT_LENGTH"))
        self.container_models_dir = Path(shared) if shared else None
        self._managed: ManagedOllama | None = None
        self._lock = threading.RLock()
        self._jobs: dict[str, LocalJob] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._show: dict[str, tuple[float, dict[str, Any]]] = {}
        self._show_failed: dict[str, tuple[float, NodestroError]] = {}
        self._ps: tuple[float, list[dict[str, Any]]] | None = None
        self._trackers: dict[str, Tracker] = {}
        self._stored_at = 0.0
        self._closing = False
        #: Finds an Ollama the user runs on this computer (never started, stopped or changed by Nodestro).
        self.detect_external: Callable[[], str | None] = detect_ollama
        self._restore_jobs()

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
                    self.home,
                    binary=self.runtime_binary(),
                    models_dir=self.models_dir(),
                    offline=self._offline(),
                    environment={"OLLAMA_CONTEXT_LENGTH": str(self.context_length)},
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
        self._show_failed.clear()
        self._ps = None
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
        """Ends the application's use of the local models: running jobs stop and are recorded as interrupted (not
        as canceled by the user) with what they had reached, so that they can be continued after the next start."""
        self._closing = True
        with self._lock:
            for event in self._cancel.values():
                event.set()
            for job in self._jobs.values():
                if job.state == "running":
                    _interrupted(job)
        self._store_jobs(force=True)
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
            status.context_length, status.context_source = self._window([], [])
            status.accelerated = self._accelerated([])
            return status
        try:
            client = self._client_factory(url)
            status.version = client.version()
            status.running = True
            status.models = self.installed(client)
        except OllamaError as exc:
            status.error_code, status.error = exc.code, str(exc)
        loaded = self._loaded(client) if status.running else []
        status.context_length, status.context_source = self._window(loaded, status.models)
        status.accelerated = self._accelerated(loaded)
        return status

    def _window(
        self, loaded: list[dict[str, Any]], models: list[InstalledModel]
    ) -> tuple[int | None, Literal["configured", "server", "unknown"]]:
        if self.mode in ("managed", "container"):
            return self.context_length, "configured"
        chat = {model.name for model in models if model.kind == "chat"}
        # Embedding models load with their own small window; only chat models tell what agents get.
        windows = [
            int(item["context_length"])
            for item in loaded
            if isinstance(item.get("context_length"), int) and item.get("name") in chat
        ]
        return (min(windows), "server") if windows else (None, "unknown")

    def context_window(self, models: list[InstalledModel] | None = None) -> int | None:
        """The context window agents get (configured, or read from a loaded chat model); ``None``: not known.
        Never starts the managed server."""
        if self.mode in ("managed", "container"):
            return self.context_length
        if self.ollama_url() is None:
            return None
        try:
            found = models if models is not None else self.installed()
        except NodestroError:
            return None
        return self._window(self._loaded(), found)[0]

    def _loaded(self, client: OllamaClient | None = None) -> list[dict[str, Any]]:
        """The models the server has loaded now (``/api/ps``; cached briefly, ``[]`` when it cannot answer)."""
        cached = self._ps
        if cached is not None and time.monotonic() - cached[0] < _PS_TTL:
            return cached[1]
        try:
            loaded = (client or self.client()).running()
        except (OllamaError, NodestroError):
            loaded = []  # remembered like an answer: a server that does not answer is not asked again at once
        self._ps = (time.monotonic(), loaded)
        return loaded

    def _here(self) -> bool:
        """Whether the server runs on this computer (managed, the Local Docker container, a loopback address)."""
        return self.mode in ("managed", "container") or _loopback(self.ollama_url())

    def _accelerated(self, loaded: list[dict[str, Any]]) -> bool | None:
        if self._here():
            # This computer's hardware decides: Ollama 0.40 reports part of a model in GPU memory (``size_vram``)
            # even on a computer without any GPU device (measured: "28%/72% CPU/GPU" on a 4-vCPU VM).
            return self.hardware().gpu != "none"
        sizes = [(int(item.get("size") or 0), int(item.get("size_vram") or 0)) for item in loaded]
        sizes = [(size, vram) for size, vram in sizes if size > 0]
        if not sizes:
            return None
        if all(vram == 0 for _size, vram in sizes):
            return False
        # Another computer: only a model that is (almost) entirely in GPU memory counts; a split is not known.
        return True if any(vram * 10 >= size * 9 for size, vram in sizes) else None

    def accelerated(self) -> bool | None:
        """Whether local models run on a GPU (``None``: not known); never starts the managed server."""
        if self.ollama_url() is None:
            return None if self.mode == "external" else self.hardware().gpu != "none"
        return self._accelerated(self._loaded())

    def speeds(self) -> dict[str, ModelSpeed]:
        """The measured speeds of installed models, by model name (see :meth:`benchmark`)."""
        stored = self.settings().get("speeds") or {}
        result = {}
        for name, value in stored.items():
            with suppress(ValueError, TypeError):
                result[name] = ModelSpeed.model_validate(value)
        return result

    def benchmark(self, name: str) -> LocalJob:
        """Measure how fast an installed chat model answers here: a short fixed request without thinking.

        Loads the model (as an agent's first request would), so it takes a while on a computer without a GPU. The
        result is stored with the model (:meth:`speeds`) and shown instead of any estimate."""
        name = _model_name(name)
        client = self.client()
        details = self.details(name, client=client)
        if not details:
            raise NodestroError("local.model_missing", f"{name} is not installed", model=name)
        capabilities = details.get("capabilities") or []
        if "completion" not in capabilities:
            raise NodestroError("local.not_chat_model", f"{name} is not a chat model", model=name)

        def work(job: LocalJob, cancel: threading.Event) -> None:
            job.message = "loading"
            body: dict[str, Any] = {
                "model": name,
                "prompt": _BENCHMARK_PROMPT,
                "stream": False,
                "options": {"num_predict": 96, "temperature": 0},
            }
            if "thinking" in capabilities:
                body["think"] = False
            data = _cancellable(
                lambda: client.http.request("POST", "/api/generate", body, timeout=900.0, model=name) or {}, cancel
            )
            if data is None or cancel.is_set():
                return  # cancelled: the job ends now, the request is left to finish on its own (RT3-07)
            generated, spent = _int_of(data.get("eval_count")), _int_of(data.get("eval_duration"))
            if not generated or not spent:
                raise NodestroError("local.benchmark_failed", f"{name} reported no generation timing", model=name)
            read, reading = _int_of(data.get("prompt_eval_count")), _int_of(data.get("prompt_eval_duration"))
            self._ps = None
            loaded = next((item for item in self._loaded(client) if item.get("name") in (name, f"{name}:latest")), {})
            size, vram = int(loaded.get("size") or 0), int(loaded.get("size_vram") or 0)
            processor: Literal["gpu", "cpu", "mixed"] | None = None
            if self._here() and self.hardware().gpu == "none":
                processor = "cpu"  # whatever ``size_vram`` says (see _accelerated)
            elif size:
                processor = "gpu" if vram >= size else "cpu" if vram == 0 else "mixed"
            speed = ModelSpeed(
                tokens_per_second=round(generated / (spent / 1e9), 2),
                prompt_tokens_per_second=round(read / (reading / 1e9), 2) if read and reading else None,
                measured_at=datetime.now(UTC),
                context_length=loaded.get("context_length") if isinstance(loaded.get("context_length"), int) else None,
                processor=processor,
            )
            with self._lock:
                speeds = dict(self.settings().get("speeds") or {})
                speeds[name] = speed.model_dump(mode="json")
                self._save(speeds=speeds)
            job.message = f"{speed.tokens_per_second} tokens/s"
            job.completed = job.total = generated

        return self._run("benchmark", name, work)

    def advice(self, status: LocalStatus, *, reasoning_saving: bool = False) -> list[LocalAdvice]:
        """Hints for this computer: a small context window, no GPU, slow or too large models, little memory.

        ``reasoning_saving``: the workspace's CPU saving mode is on (``local_reasoning`` ``auto``); shown when it
        applies here."""
        hints: list[LocalAdvice] = []
        if status.context_length is not None and status.context_length < SMALL_CONTEXT:
            hints.append(
                LocalAdvice(
                    code="local.context_small",
                    severity="warning",
                    value=status.context_length,
                    recommended=LOCAL_CONTEXT_LENGTH,
                )
            )
        elif status.running and status.context_source == "unknown":
            hints.append(LocalAdvice(code="local.context_unknown", recommended=LOCAL_CONTEXT_LENGTH))
        hardware = self.hardware()
        if status.accelerated is False:
            hints.append(LocalAdvice(code="local.cpu_only", value=hardware.cpus))
            if reasoning_saving:
                hints.append(LocalAdvice(code="local.reasoning_saving"))
        if hardware.ram_bytes is not None and hardware.ram_bytes < LOW_MEMORY:
            hints.append(
                LocalAdvice(
                    code="local.memory_low", severity="warning", value=hardware.ram_bytes, recommended=LOW_MEMORY
                )
            )
        limit = max(hardware.ram_bytes or 0, hardware.gpu_memory_bytes or 0)
        for model in status.models:
            if model.kind != "chat":
                continue
            if model.speed is not None and model.speed.tokens_per_second < SLOW_TOKENS_PER_SECOND:
                hints.append(
                    LocalAdvice(
                        code="local.model_slow",
                        severity="warning",
                        model=model.name,
                        value=int(model.speed.tokens_per_second * 10),
                        recommended=int(SLOW_TOKENS_PER_SECOND * 10),
                    )
                )
            # Loaded, a model needs about a fifth more than its file (context and runtime buffers).
            if limit and model.size * 6 // 5 > limit:
                hints.append(
                    LocalAdvice(code="local.model_too_large", severity="warning", model=model.name, value=model.size)
                )
        return hints

    # ---- models --------------------------------------------------------------------------------------------

    def installed(self, client: OllamaClient | None = None) -> list[InstalledModel]:
        client = client or self.client()
        try:
            found = client.list_models()
        except OllamaError as exc:
            raise _error(exc) from None
        result = []
        speeds = self.speeds()
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
                    speed=speeds.get(model.name),
                )
            )
        return result

    def details(self, name: str, *, client: OllamaClient | None = None) -> dict[str, Any]:
        """``/api/show`` of an installed model: capabilities, context, Orkexa capability overrides (cached)."""
        name = name.removeprefix("ollama:")
        cached = self._show.get(name)
        if cached is not None and time.monotonic() - cached[0] < _SHOW_TTL:
            return cached[1]
        failed = self._show_failed.get(name)
        if failed is not None and time.monotonic() - failed[0] < _FAILED_TTL:
            raise failed[1]
        try:
            details = (client or self.client()).show(name)
        except OllamaError as exc:
            if exc.code == "model_missing":
                return {}
            error = _error(exc)
            if exc.code in _UNANSWERED:
                # A server that does not answer costs the client's whole timeout per request; a run start asks for
                # the same model several times (RT3-04), so the failure is remembered briefly too.
                self._show_failed[name] = (time.monotonic(), error)
            raise error from None
        value = {
            "capabilities": list(details.capabilities),
            "context_length": details.context_length,
            "embedding_length": details.embedding_length,
            "overrides": _overrides(details.capabilities, details.capability_overrides()),
        }
        self._show[name] = (time.monotonic(), value)
        return value

    def capability_overrides(self, model: str) -> dict[str, str]:
        """The Orkexa capabilities an installed model reports (``{}`` when it is missing).

        Never starts the managed server: while none runs, a suggested model has the capabilities the verified
        catalog lists (a running server's ``/api/show`` values replace them); any other model has none.
        """
        name = model.removeprefix("ollama:")
        if self.ollama_url() is None and name not in self._show:
            entry = catalog_model(name)
            if entry is None or entry.kind != "chat":
                return {}
            listed = tuple(entry.listed_capabilities)
            return _overrides(listed, ModelDetails(name=name, capabilities=listed).capability_overrides())
        try:
            return dict(self.details(model).get("overrides") or {})
        except (NodestroError, ValueError):
            # No answer, or an address the client refuses (e.g. one with credentials, saved before such addresses
            # were refused): nothing is known, and the agent check names what that leaves unknown.
            return {}

    def preferences(self) -> SelectionPreferences:
        """The stored selection preferences (e.g. European models first); a manual model choice always wins."""
        return self._preferences()

    def recommend(self, task: str, preferences: SelectionPreferences | None = None) -> Recommendation:
        installed: list[InstalledModel] = []
        with suppress(NodestroError):  # no server: recommend from the catalog alone
            installed = self.installed()
        return recommend(
            task,
            self.hardware(),
            [model.name for model in installed],
            capabilities={model.name: model.capabilities for model in installed},
            preferences=preferences if preferences is not None else self.preferences(),
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
        for tracker in list(self._trackers.values()):
            tracker.tick()
        return sorted(self._jobs.values(), key=lambda job: job.started_at, reverse=True)

    def job(self, job_id: str) -> LocalJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise NodestroError("local.job_missing", f"No local model job {job_id}", job=job_id)
        tracker = self._trackers.get(job_id)
        if tracker is not None:
            tracker.tick()
        return job

    def cancel(self, job_id: str) -> LocalJob:
        job = self.job(job_id)
        event = self._cancel.get(job_id)
        if event is not None:
            event.set()
        return job

    def _restore_jobs(self) -> None:
        """The jobs of the last run of the application: one that was still running was interrupted by the end of
        that run (its thread is gone). A download continues where it stopped when it is started again."""
        try:
            stored = self.store.get_raw(JOBS_COLLECTION, JOBS_ID) or {}
        except Exception:  # a store without the document, or an unreadable one: no history
            logger.warning("The recent local model jobs could not be read", exc_info=True)
            return
        for value in stored.get("jobs") or []:
            try:
                job = LocalJob.model_validate(value)
            except ValueError:
                continue
            if job.state == "running":
                _interrupted(job)
            self._jobs[job.id] = job

    def _store_jobs(self, *, force: bool = False) -> None:
        moment = time.monotonic()
        if not force and moment - self._stored_at < _STORE_EVERY:
            return
        self._stored_at = moment
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.started_at, reverse=True)
            finished = [job for job in jobs if job.state != "running"]
            for job in finished[_KEEP_JOBS:]:
                self._jobs.pop(job.id, None)
            kept = [job.model_dump(mode="json") for job in jobs if job.id in self._jobs]
        try:
            self.store.put_raw(JOBS_COLLECTION, JOBS_ID, {"jobs": kept})
        except Exception:  # the job itself goes on; only its record after a restart is missing
            logger.warning("The local model jobs could not be stored", exc_info=True)

    def _changed(self, job: LocalJob, important: bool) -> None:
        self._store_jobs(force=important)

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

    def _run(
        self,
        kind: JobKind,
        target: str,
        work: Callable[[LocalJob, threading.Event], None],
        *,
        steps: tuple[str, ...] = (),
    ) -> LocalJob:
        with self._lock:
            self._busy(kind, target)
            job = LocalJob(id=uuid.uuid4().hex[:16], kind=kind, target=target, started_at=datetime.now(UTC))
            job.updated_at = job.started_at
            cancel = threading.Event()
            self._jobs[job.id] = job
            self._cancel[job.id] = cancel
            tracker = Tracker(job, steps, changed=self._changed)
            self._trackers[job.id] = tracker
        self._store_jobs(force=True)

        def body() -> None:
            try:
                work(job, cancel)
                job.state = "cancelled" if cancel.is_set() else "done"
            except OllamaError as exc:
                job.state = "cancelled" if exc.code == "cancelled" or cancel.is_set() else "failed"
                job.error_code, job.error = f"local.{exc.code}", str(exc)
            except NodestroError as exc:
                job.state = "cancelled" if exc.code == "local.cancelled" or cancel.is_set() else "failed"
                job.error_code, job.error = exc.code, str(exc)
            except Exception as exc:  # reported on the job; never stops the application
                logger.exception("Local model job %s failed", job.id)
                job.state = "failed"
                job.error_code, job.error = "local.job_failed", str(exc)
            finally:
                if self._closing and job.state != "done":
                    _interrupted(job)
                elif job.state == "done":
                    tracker.complete()
                else:
                    tracker.fail()
                    tracker.log(f"{job.state}: {job.error_code or ''} {job.error or ''}".rstrip())
                job.retryable = retryable(job)
                job.finished_at = job.updated_at = datetime.now(UTC)
                self._cancel.pop(job.id, None)
                self._trackers.pop(job.id, None)
                self._show.clear()
                self._show_failed.clear()
                self._store_jobs(force=True)

        threading.Thread(target=body, name=f"nodestro-local-{kind}", daemon=True).start()
        return job

    def running_download(self, name: str) -> LocalJob | None:
        """The download (or build) of ``name`` that runs now, if any."""
        name = _model_name(name)
        with self._lock:
            for job in self._jobs.values():
                if job.state == "running" and job.kind in ("pull", "build") and job.target == name:
                    return job
        return None

    def pull(self, name: str, *, attach: bool = False) -> LocalJob:
        """Download a model (needs the internet; refused in the strict offline mode).

        ``attach`` returns the download of the same model that runs already instead of refusing a second one
        (``local.job_running``): a second click, another tab or another dialog follows the same job.

        A catalog model that is built from pinned upstream weights (``source="build"``) downloads those and is built.
        The job goes through Ollama's own steps (manifest, download, verify, finalize) with the bytes of all files
        added up; a download that was interrupted continues where it stopped (Ollama keeps partial files)."""
        name = _model_name(name)
        if attach:
            running = self.running_download(name)
            if running is not None:
                return running
            try:
                return self.pull(name)
            except NodestroError as exc:  # started by another request in between: follow that one
                if exc.code == "local.job_running" and isinstance(exc.params.get("job"), str):
                    return self.job(exc.params["job"])
                raise
        entry = catalog_model(name)
        if entry is not None and entry.source == "build":
            return self.build(name, download=True)
        self._refuse_offline("downloading a model")
        client = self.client()
        folder = self.models_dir() if self.mode in ("managed", "container") else None

        def work(job: LocalJob, cancel: threading.Event) -> None:
            tracker = self._trackers[job.id]
            tracker.enter("check")
            present: dict[str, int] = {}
            if folder is not None:
                free = _free_bytes(folder)
                present, partial = _present_bytes(folder)
                needed = entry.download_bytes if entry is not None else None
                detail = f"models folder {folder}; free {free} bytes" + (
                    f"; download {needed} bytes; partial files {partial} bytes" if needed else ""
                )
                if needed and free is not None and free + partial < needed:
                    tracker.log(f"check: {detail}")
                    raise NodestroError(
                        "local.disk_full",
                        f"{folder} has {free} bytes free; {name} needs about {needed - partial} more",
                        needed=needed - partial,
                        free=free,
                        path=str(folder),
                    )
                tracker.finish("check", detail=detail)
            else:
                tracker.skip("check", note="not_here")
            progress = PullProgress(tracker, present)
            for item in client.pull(name, cancel=cancel):
                progress.update(item.status, item.digest, item.total, item.completed)

        return self._run("pull", name, work, steps=PULL_STEPS)

    def build(
        self, name: str, *, weights: str | None = None, template: str | None = None, download: bool = False
    ) -> LocalJob:
        """Build a catalog model from its pinned upstream files (e.g. ``mdenseon:307m``) and check its reference
        vectors. ``weights`` and ``template`` are files on this computer (offline); ``download`` fetches the missing
        ones (refused in the strict offline mode)."""
        name = _model_name(name)
        recipe = _RECIPES.get(name)
        if recipe is None:
            raise NodestroError("local.not_buildable", f"{name} is not built from upstream files", model=name)
        if download:
            self._refuse_offline("downloading model weights")
        files = {
            key: Path(value).expanduser() for key, value in (("weights", weights), ("template", template)) if value
        }
        for path in files.values():
            if not path.is_file():
                raise NodestroError("local.file_missing", f"{path} does not exist", path=str(path))
        if not download and len(files) < 2:
            raise NodestroError(
                "local.archive_or_download", "Choose the weights and template files or allow the download"
            )
        client = self.client()
        self.home.mkdir(parents=True, exist_ok=True)

        def work(job: LocalJob, cancel: threading.Event) -> None:
            tracker = self._trackers[job.id]

            def progress(text: str) -> None:
                job.message = text
                step = build_step(text)
                if step is not None:
                    tracker.enter(step)

            def transferred(done: int, total: int) -> None:
                tracker.progress(done, total)

            build_model(
                client,
                recipe,
                weights=files.get("weights"),
                template=files.get("template"),
                download=download,
                work_dir=self.home,  # next to the models, not a small temporary file system
                progress=progress,
                cancel=cancel,
                transferred=transferred,
            )

        return self._run("build", name, work, steps=BUILD_STEPS)

    def install_runtime(self, *, archive: str | None = None, download: bool = False) -> LocalJob:
        """Install the pinned runtime from a local release archive, or download it (desktop only).

        The same job as :meth:`setup`; the server starts afterwards only when it starts with the application."""
        if self.mode != "managed":
            raise NodestroError("local.not_managed", "Nodestro does not manage the local model server here")
        if archive is None and not download:
            raise NodestroError("local.archive_or_download", "Choose a runtime archive or allow the download")
        if download:
            self._refuse_offline("downloading the runtime")
        return self.setup(archive=archive, download=download, start=self.autostart())

    def setup(self, *, archive: str | None = None, download: bool = False, start: bool = True) -> LocalJob:
        """Set up the local model server, step by step (one job, see :data:`SETUP_STEPS`).

        Managed (desktop): check what is installed, the prerequisites (offline mode, folder, free space), download
        and verify the pinned runtime if needed (a partial download continues), install it, configure and start the
        server, check that it answers, detect the hardware. Steps that are not needed are skipped with the reason.
        Local Docker and a saved address (the user's own server): only check that the server answers and, when it
        runs on this computer, the hardware; such a server is never started, stopped or configured by Nodestro.

        ``download`` allows the runtime download (refused in the strict offline mode); ``archive`` installs from a
        release archive on this computer. Only one setup runs at a time."""
        mode = self.mode
        if mode == "unavailable":
            raise NodestroError("local.unavailable", "No local model server is available here")
        source = Path(archive).expanduser() if archive else None
        if source is not None and not source.is_file():
            raise NodestroError("local.file_missing", f"{source} does not exist", path=str(source))
        if mode == "managed" and source is None and self.runtime_binary() is None:
            # Refused at once (not as a failed job): nothing to set up without the runtime or the right to fetch it.
            if not download:
                raise NodestroError("local.archive_or_download", "Choose a runtime archive or allow the download")
            self._refuse_offline("downloading the runtime")
        with self._lock:
            for job in self._jobs.values():
                if job.state == "running" and job.kind in ("setup", "install_runtime"):
                    raise NodestroError(
                        "local.job_running", "The local model server is already being set up", job=job.id
                    )

            def work(job: LocalJob, cancel: threading.Event) -> None:
                self._setup(self._trackers[job.id], mode, source, download, start, cancel)

            return self._run("setup", "server", work, steps=SETUP_STEPS)

    def _setup(
        self, steps: Tracker, mode: Mode, source: Path | None, download: bool, start: bool, cancel: threading.Event
    ) -> None:
        def stop_if_cancelled() -> None:
            if cancel.is_set():
                raise NodestroError("local.cancelled", "The setup was cancelled")

        # 1. This system and what is installed already.
        steps.enter("check")
        binary = self.runtime_binary() if mode == "managed" else None
        if mode == "managed":
            external = self.detect_external()
            if external:
                steps.log(
                    f"An Ollama {external} already runs on 127.0.0.1:11434; Nodestro runs its own server next to it "
                    "and never stops or changes that one"
                )
            if binary is not None:
                note = "bundled" if binary == self.bundled_binary else "installed"
                steps.finish("check", note=note, detail=f"Ollama runtime {binary}")
            else:
                try:
                    artifact = artifact_for()
                except OllamaError as exc:
                    raise _error(exc) from None
                steps.finish(
                    "check",
                    note="external_found" if external else "missing",
                    detail=f"Ollama {OLLAMA_VERSION} is not installed; archive {artifact.name}"
                    + (f"; your own Ollama {external} runs on port 11434 and is left alone" if external else ""),
                )
        else:
            url = self.ollama_url()
            steps.finish("check", note=mode, detail=f"Ollama at {url}" if url else None)
        stop_if_cancelled()

        # 2.-5. Prerequisites, download, verify and install (managed, only without a runtime).
        if mode == "managed" and binary is None:
            steps.enter("prerequisites")
            if source is None and not download:
                raise NodestroError("local.archive_or_download", "Choose a runtime archive or allow the download")
            if source is None:
                self._refuse_offline("downloading the runtime")
            try:
                self.home.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise NodestroError(
                    "local.models_dir", f"{self.home} cannot be used: {exc}", path=str(self.home)
                ) from None
            if not os.access(self.home, os.W_OK):
                raise NodestroError("local.models_dir", f"{self.home} is not writable", path=str(self.home))
            free = _free_bytes(self.home)
            steps.finish("prerequisites", detail=f"folder {self.home}; free {free} bytes")
            if source is not None:
                steps.skip("download", note="archive", detail=str(source))

            def phase(name: str) -> None:
                steps.enter({"download": "download", "verify": "verify", "unpack": "install"}[name])

            def progress(done: int, total: int | None) -> None:
                downloading = steps.job.phase == "download"
                if downloading and steps.job.progress_step != "download" and done:
                    steps.job.resumed_bytes = done  # a partial download of an earlier attempt continues
                    steps.log(f"download: continues after {done} bytes of an earlier attempt")
                steps.progress(done, total, rate=downloading)

            try:
                binary = install_runtime(
                    self.home, archive=source, download=source is None, progress=progress, cancel=cancel, phase=phase
                )
            except OllamaError as exc:
                raise _error(exc) from None
            steps.finish("install", detail=f"Ollama runtime {binary}")
            with self._lock:
                self._managed = None  # the next start uses the new binary
        else:
            note = {"managed": "installed", "container": "container", "external": "external"}.get(mode, mode)
            if mode == "managed" and binary is not None and binary == self.bundled_binary:
                note = "bundled"
            for step in ("prerequisites", "download", "verify", "install"):
                steps.skip(step, note=note)
        stop_if_cancelled()

        # 6.-7. Configure and start (only Nodestro's own server).
        if mode == "managed":
            steps.enter("configure")
            folder = self.models_dir()
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise NodestroError(
                    "local.models_dir", f"The folder {folder} cannot be used: {exc}", path=str(folder)
                ) from None
            if not os.access(folder, os.W_OK):
                raise NodestroError("local.models_dir", f"The folder {folder} is not writable", path=str(folder))
            steps.finish(
                "configure",
                detail=f"OLLAMA_MODELS={folder}; OLLAMA_CONTEXT_LENGTH={self.context_length}; OLLAMA_NO_CLOUD=1; "
                f"loopback only; offline={self._offline()}",
            )
            if not start:
                for step in ("start", "reachable"):
                    steps.skip(step, note="autostart_off")
            else:
                running = self._managed is not None and self._managed.base_url is not None
                steps.enter("start")
                url = self.start()
                steps.finish("start", note="already_running" if running else None, detail=url)
        else:
            steps.skip("configure", note="not_managed")
            steps.skip("start", note="not_managed")
        stop_if_cancelled()

        # 8. Does it answer?
        url = self.ollama_url()
        if url is not None:
            steps.enter("reachable", detail=url)
            try:
                version = self._client_factory(url).version()
            except OllamaError as exc:
                raise _error(exc) from None
            differs = "version_differs" if mode != "external" and version != OLLAMA_VERSION else None
            steps.finish("reachable", note=differs, detail=f"Ollama {version} at {url}")
            self._show_failed.clear()
            self._ps = None
        elif start or mode != "managed":
            raise NodestroError("local.not_running", "No local model server is running")

        # 9. Hardware (only of this computer).
        if self._here():
            steps.enter("hardware")
            hardware = self.hardware()
            gpu_memory = f", {hardware.gpu_memory_bytes} bytes" if hardware.gpu_memory_bytes else ""
            steps.finish(
                "hardware",
                note=f"gpu_{hardware.gpu}",
                detail=f"{hardware.system}/{hardware.machine}; {hardware.cpus} CPU cores; "
                f"memory {hardware.ram_bytes} bytes; GPU {hardware.gpu}{gpu_memory}",
            )
        else:
            steps.skip("hardware", note="remote")
        steps.enter("ready")
        steps.finish("ready", note="ready" if url is not None else "installed")

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
        self._show_failed.clear()
        if running:
            self.start()


#: The orkexa recipes of catalog models with ``source="build"``.
_RECIPES: dict[str, GGUFRecipe] = {MDENSEON.name: MDENSEON}


class BuildInput(BaseModel):
    """One pinned upstream file a catalog model is built from (what an offline build needs on this computer)."""

    role: Literal["weights", "template"]
    url: str
    sha256: str
    #: Bytes; for the template the length of the header the build reads (a longer file with it is accepted).
    size: int | None = None


def build_inputs(name: str) -> list[BuildInput]:
    """The pinned files of a catalog model built from upstream files (``[]`` for every other model)."""
    recipe = _RECIPES.get(name.removeprefix("ollama:"))
    if recipe is None:
        return []
    return [
        BuildInput(role="weights", url=recipe.weights.url, sha256=recipe.weights.sha256, size=recipe.weights.size),
        BuildInput(role="template", url=recipe.template.url, sha256=recipe.template.sha256, size=recipe.template.size),
    ]


def _int_of(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _loopback(url: str | None) -> bool:
    """Whether the server's address is this computer (its hardware is the one :func:`detect_hardware` sees)."""
    if not url:
        return False
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


def _cancellable(work: Callable[[], Any], cancel: threading.Event) -> Any:
    """``work()`` in a helper thread; ``None`` as soon as ``cancel`` is set (its result is then dropped). For one
    blocking request that cannot be interrupted, e.g. a benchmark that may wait minutes for a model to load."""
    result: dict[str, Any] = {}
    finished = threading.Event()

    def run() -> None:
        try:
            result["value"] = work()
        except BaseException as exc:  # handed to the caller below
            result["error"] = exc
        finally:
            finished.set()

    threading.Thread(target=run, name="nodestro-local-request", daemon=True).start()
    while not finished.wait(0.2):
        if cancel.is_set():
            return None
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _context_length(value: str | None) -> int:
    """The context window from ``NODESTRO_OLLAMA_CONTEXT_LENGTH`` (2048 to 262144), else the default."""
    try:
        number = int(value or "")
    except ValueError:
        return LOCAL_CONTEXT_LENGTH
    return number if 2048 <= number <= 262_144 else LOCAL_CONTEXT_LENGTH


def _overrides(capabilities: Iterable[str], known: Mapping[str, str]) -> dict[str, str]:
    """Orkexa's capability values plus reasoning: Ollama reports ``thinking`` for models that reason and takes the
    effort as ``reasoning_effort`` on its OpenAI-compatible API (sent with ``send_reasoning_effort``)."""
    values = dict(known)
    if "completion" in capabilities:
        values.setdefault("reasoning", "supported" if "thinking" in capabilities else "unsupported")
    return values


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


def _interrupted(job: LocalJob) -> None:
    """A job that the end of the application stopped: failed with ``local.interrupted``, continued when retried."""
    job.state = "failed"
    job.error_code = "local.interrupted"
    job.error = "Nodestro stopped while this job was running"
    job.finished_at = job.finished_at or job.updated_at or job.started_at
    for step in job.steps:
        if step.state == "running":
            step.state = "failed"
    job.phase, job.rate, job.eta_seconds, job.eta_scope = None, None, None, None
    job.stalled_seconds = None
    job.retryable = retryable(job)


def _present_bytes(folder: Path) -> tuple[dict[str, int], int]:
    """What an Ollama model folder holds already, by digest (``sha256:<hex>``), and the bytes of partial downloads.

    A complete file counts with its size. A partial download (``sha256-<hex>-partial``, which Ollama creates at the
    file's full size) counts with the bytes Ollama recorded in its part files (``-partial-<n>``: JSON with
    ``Completed``); Ollama continues it from there."""
    blobs = folder / "blobs"
    present: dict[str, int] = {}
    partial = 0
    try:
        entries = list(blobs.iterdir())
    except OSError:
        return present, 0
    for blob in entries:
        name = blob.name
        if not name.startswith("sha256-"):
            continue
        digest, _, rest = name.partition("-partial")
        key = "sha256:" + digest.removeprefix("sha256-")
        if not _:
            with suppress(OSError):
                present[key] = blob.stat().st_size
            continue
        if not rest.startswith("-"):
            continue  # the preallocated file itself
        try:
            done = json.loads(blob.read_text(encoding="utf-8")).get("Completed")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(done, int) and done > 0:
            present[key] = present.get(key, 0) + done
            partial += done
    return present, partial


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
    "SETUP_STEPS",
    "InstalledModel",
    "JobLayer",
    "JobState",
    "JobStep",
    "LocalJob",
    "LocalModelService",
    "LocalStatus",
]
