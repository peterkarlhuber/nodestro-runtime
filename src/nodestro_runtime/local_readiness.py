"""Whether a local model an action needs is ready here, and what can be done when it is not.

One mechanism for every place that needs a local (Ollama) model: the Workflow Architect, the assistant, agent
design and tests, run starts, the semantic knowledge index. :func:`diagnose` names *why* a model cannot be used
(:data:`Cause`, never lumped together as "missing") and *what* may be done about it (:data:`Action`), computed here
from the facts only the backend knows:

- the operating mode of the local model server (``managed`` by the desktop, the Local Docker ``container``, an
  ``external`` server the user runs, on this computer or another one, or a Nodestro **server**, whose model server
  belongs to its operator),
- whether the person may install anything (the permission ``install``; never a server's tenants),
- the strict offline mode (no downloads at all),
- the free space of the model folder, the catalog's download size and whether the model fits this computer,
- a download of the same model that already runs (shown and followed instead of starting a second one).

:func:`alternatives` lists models that are ready now and can do what the action needs (tool calling, structured
output, image input, embeddings), for the person to choose; nothing here switches a model by itself.

Interactive callers (:func:`require_ready`) refuse the action with ``model.local_unavailable`` and the
:class:`ModelProblem` as its parameters; the studio shows one dialog for it and continues the action afterwards.
Unattended runs (schedules, batches, published APIs) never download anything: the workflow check reports
``check.local_model_missing`` (also while the server is not running, from the model folder) and the run is refused
with that problem, which an authorized person fixes from the run, the schedule or the batch.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from orkexa.local import OllamaError, check_model_name, scan_store
from pydantic import BaseModel, Field

from .local_models import Hardware, catalog_model, fits
from .local_service import InstalledModel, LocalJob, _free_bytes, _loopback, _present_bytes
from .problems import NodestroError
from .providers import provider_of

if TYPE_CHECKING:
    from .workspace import Workspace

#: The error code of an action refused because its local model is not ready (params: a :class:`ModelProblem`).
UNAVAILABLE = "model.local_unavailable"

Cause = Literal[
    # The model is not on the model server that answers (and downloading it is possible in principle).
    "model_missing",
    # Desktop: the local model runtime (Ollama) is not installed yet.
    "runtime_missing",
    # Desktop: the runtime is installed, but the server does not start.
    "server_stopped",
    # Local Docker's container or an external server does not answer.
    "server_unreachable",
    # No local model server is configured here at all (a server without the operator's endpoint).
    "server_missing",
    # Not a valid Ollama model name (or an Ollama cloud model, which never runs locally).
    "model_name_invalid",
    # Missing, and its download does not fit into the free space of the model folder.
    "disk_full",
    # The model (installed, or as the catalog lists it) cannot do what the action needs.
    "capability_missing",
    # Missing, and the catalog says it does not run on this computer's hardware.
    "hardware_insufficient",
]

Action = Literal[
    # Download the model on the model server that agents use (a job with progress; attaches to a running one).
    "download",
    # Set up the local model server (desktop; downloads the pinned runtime when allowed).
    "setup_server",
    # Check again (a server that may answer now).
    "retry",
    # Use another model that is ready, for this action only.
    "choose_model",
    # Ask an administrator / the operator to install the model (no permission or a server).
    "ask_admin",
    # Open the local model settings (import from a file, move the model folder, the server address).
    "open_settings",
]

#: Where the model has to be installed: this computer, the Local Docker container, another computer the user's own
#: server runs on, or the Nodestro server (its operator's).
Location = Literal["this_computer", "container", "remote", "server"]

#: Why the download is not offered although the model is missing: no permission, the server's operator decides, the
#: strict offline mode.
Blocked = Literal["permission", "operator", "offline"]


class ModelNeeds(BaseModel):
    """What an action needs of its model (Orkexa capability names; ``embedding`` for the knowledge index)."""

    kind: Literal["chat", "embedding"] = "chat"
    capabilities: list[str] = Field(default_factory=list)
    #: The output limit the action asks for (informative: a smaller maximum is named, never silently used).
    output_tokens: int | None = None


class ModelProblem(BaseModel):
    """Why a local model cannot be used for an action now, and what may be done about it."""

    cause: Cause
    #: The qualified model id (``ollama:qwen3.5:9b``) and the Ollama tag.
    model: str
    name: str
    #: What the model is needed for (``architect``, ``assistant``, ``agent_test``, ``agent_design``, ``run``,
    #: ``knowledge_index``, ``check``).
    action: str
    mode: Literal["managed", "container", "external", "unavailable"]
    location: Location
    #: The actions this person may take now, in the order the interface offers them.
    actions: list[Action] = Field(default_factory=list)
    blocked: Blocked | None = None
    offline: bool = False
    #: The model is part of Nodestro's verified catalog; its download size, the memory it needs (bytes).
    catalog: bool = False
    download_bytes: int | None = None
    memory_bytes: int | None = None
    #: Free bytes of the model folder and bytes of partial downloads there (managed, container); ``None``: unknown.
    free_bytes: int | None = None
    partial_bytes: int | None = None
    #: Whether the model runs on this computer's hardware (``None``: not known, e.g. a remote server).
    fits: bool | None = None
    #: Capabilities the action needs that the model lacks.
    missing_capabilities: list[str] = Field(default_factory=list)
    #: A download or setup of this model (or of the server) that runs now: follow it instead of starting another.
    job: LocalJob | None = None
    #: The newest earlier download of the model that stopped (cancelled, failed, interrupted): starting it again
    #: continues where it stopped.
    previous_job: LocalJob | None = None
    #: The host of a remote model server (no address details beyond the host).
    host: str | None = None


class ModelAlternative(BaseModel):
    """A model that is ready now and can do what the action needs."""

    model: str
    provider: str
    local: bool
    #: Context window and maximum output (tokens) where known.
    context_window: int | None = None
    max_output_tokens: int | None = None
    #: The model's maximum output is below what the action asks for (it is then capped, and the job says so).
    limited: bool = False


class Readiness(BaseModel):
    """The answer of a readiness check: ``problem`` is ``None`` when the model can be used now."""

    model: str
    ready: bool
    problem: ModelProblem | None = None
    alternatives: list[ModelAlternative] = Field(default_factory=list)


def is_local(model: str) -> bool:
    try:
        return provider_of(model) == "ollama"
    except NodestroError:
        return False


def local_name(model: str) -> str:
    """The Ollama tag of a model id, with the implicit ``:latest``."""
    name = model.removeprefix("ollama:").strip()
    return name if ":" in name else f"{name}:latest"


def _location(ws: Workspace) -> Location:
    service = ws.local_models
    if service.profile == "server":
        return "server"
    if service.mode == "container":
        return "container"
    if service.mode == "external" and not _loopback(service.ollama_url()):
        return "remote"
    return "this_computer"


def _host(url: str | None) -> str | None:
    if not url:
        return None
    with suppress(ValueError):
        return urlsplit(url).hostname
    return None


def _jobs_of(ws: Workspace, name: str) -> tuple[LocalJob | None, LocalJob | None]:
    """The running download of ``name`` (or the running server setup), and its newest stopped download."""
    running: LocalJob | None = None
    stopped: LocalJob | None = None
    for job in ws.local_models.jobs():  # newest first
        mine = job.kind in ("pull", "build") and local_name(job.target) == name
        if job.state == "running" and (mine or job.kind in ("setup", "install_runtime")):
            running = running or job
        elif mine and stopped is None and job.state != "done":
            stopped = job
        elif mine and job.state == "done":
            break  # an older stopped download is history once a later one succeeded
    return running, stopped


def _lacking(needs: ModelNeeds, capabilities: Iterable[str], *, kind: str) -> list[str]:
    """Needed capabilities an Ollama model lacks (Ollama's names: ``tools``, ``vision``, ``completion``)."""
    have = set(capabilities)
    if needs.kind == "embedding":
        return [] if kind == "embedding" or "embedding" in have else ["embedding"]
    if kind == "embedding":
        return ["completion"]
    ollama = {"tools": "tools", "image_input": "vision", "reasoning": "thinking"}
    return [name for name in needs.capabilities if name in ollama and ollama[name] not in have]


def _offered(
    cause: Cause, *, location: Location, can_install: bool, offline: bool, switchable: bool
) -> tuple[list[Action], Blocked | None]:
    """The actions for ``cause`` that this person may take in this operating mode."""
    blocked: Blocked | None = None
    if location == "server":
        blocked = "operator"
    elif not can_install:
        blocked = "permission"
    actions: list[Action] = []
    if cause in ("model_missing", "hardware_insufficient"):
        if blocked is None and offline:
            blocked = "offline"
        if blocked is None:
            actions.append("download")
    elif cause == "runtime_missing":
        if blocked is None:
            # Offline the runtime is installed from an archive file in the settings; online the setup downloads it.
            actions.append("open_settings" if offline else "setup_server")
            if offline:
                blocked = "offline"
    elif cause in ("server_stopped", "server_unreachable") or cause == "disk_full":
        actions.append("retry")
        if blocked is None:
            actions.append("open_settings")
    elif cause == "server_missing" and blocked is None:
        actions.append("open_settings")
    if switchable:
        actions.append("choose_model")
    if blocked in ("operator", "permission") and cause not in ("model_name_invalid", "capability_missing"):
        actions.append("ask_admin")
    return actions, blocked


def diagnose(
    ws: Workspace,
    model: str,
    *,
    action: str,
    needs: ModelNeeds | None = None,
    can_install: bool,
    switchable: bool = False,
    start: bool = True,
) -> ModelProblem | None:
    """Why ``model`` cannot be used for ``action`` now (``None``: it can, or it is not a local model).

    ``start`` lets the check start the managed server of the desktop, as the action itself would; the server is
    never started for unattended checks. Nothing is ever downloaded here."""
    if not is_local(model) or _injected(ws):
        return None
    needs = needs or ModelNeeds()
    service = ws.local_models
    offline = ws.strict_offline()
    location = _location(ws)
    raw = model.removeprefix("ollama:").strip()
    name = local_name(model)

    def found(cause: Cause, **extra: Any) -> ModelProblem:
        actions, blocked = _offered(
            cause, location=location, can_install=can_install, offline=offline, switchable=switchable
        )
        return ModelProblem(
            cause=cause,
            model=f"ollama:{raw}",
            name=raw,
            action=action,
            mode=service.mode,
            location=location,
            actions=actions,
            blocked=blocked,
            offline=offline,
            host=_host(service.ollama_url()) if location == "remote" else None,
            **extra,
        )

    try:
        check_model_name(raw)
    except OllamaError:
        return found("model_name_invalid")
    entry = catalog_model(name)
    facts: dict[str, Any] = {}
    if entry is not None:
        facts = {"catalog": True, "download_bytes": entry.download_bytes, "memory_bytes": entry.memory_bytes}
    running, stopped = _jobs_of(ws, name)
    mode = service.mode
    if mode == "unavailable":
        return found("server_missing", **facts)
    if mode == "managed" and service.runtime_binary() is None:
        setup = running if running is not None and running.kind in ("setup", "install_runtime") else None
        return found("runtime_missing", job=setup, **facts)
    if mode == "managed" and service.ollama_url() is None and not start:
        return _from_folder(ws, model, name, action=action, needs=needs, found=found, facts=facts)
    try:
        client = service.client()  # the managed server is started here, as the action would start it
        if not client.is_running():
            return found("server_stopped" if mode == "managed" else "server_unreachable", **facts)
        installed = {local_name(item.name): item for item in service.installed(client)}
    except (NodestroError, ValueError, OllamaError):
        return found("server_stopped" if mode == "managed" else "server_unreachable", **facts)
    item = installed.get(name)
    if item is not None:
        lacking = _lacking(needs, item.capabilities, kind=item.kind)
        return found("capability_missing", missing_capabilities=lacking, **facts) if lacking else None
    if entry is not None:
        listed = _lacking(needs, entry.listed_capabilities, kind=entry.kind)
        if listed:
            return found("capability_missing", missing_capabilities=listed, **facts)
    pull = running if running is not None and running.kind in ("pull", "build") else None
    folder = service.models_dir() if mode in ("managed", "container") else None
    if folder is not None:
        facts["free_bytes"] = _free_bytes(folder)
        with suppress(OSError):
            facts["partial_bytes"] = _present_bytes(folder)[1]
    free, partial = facts.get("free_bytes"), facts.get("partial_bytes") or 0
    if pull is None and entry is not None and free is not None and free + partial < entry.download_bytes:
        return found("disk_full", previous_job=stopped, **facts)
    if entry is not None and location in ("this_computer", "container"):
        facts["fits"] = fits(entry, _hardware(ws))
        if not facts["fits"]:
            return found("hardware_insufficient", job=pull, previous_job=stopped, **facts)
    return found("model_missing", job=pull, previous_job=stopped, **facts)


def _injected(ws: Workspace) -> bool:
    """Whether the workspace reaches Ollama through an injected client (tests, embedding applications): no local
    model server is involved then, and the client answers for its models itself."""
    factory = getattr(ws, "_client_factory", None)
    return factory is not None and factory("ollama") is not None


def _hardware(ws: Workspace) -> Hardware:
    return ws.local_models.hardware()


def _from_folder(
    ws: Workspace, model: str, name: str, *, action: str, needs: ModelNeeds, found: Any, facts: dict[str, Any]
) -> ModelProblem | None:
    """The managed server does not run and must not be started (an unattended check): its model folder tells."""
    try:
        stored = {local_name(item.name) for item in scan_store(ws.local_models.models_dir())}
    except (OllamaError, OSError):
        stored = set()
    if name in stored:
        return None
    problem: ModelProblem = found("model_missing", **facts)
    return problem


def alternatives(
    ws: Workspace, needs: ModelNeeds | None = None, *, exclude: str | None = None, limit: int = 8
) -> list[ModelAlternative]:
    """Models that are ready now and can do what the action needs: installed local models (by what the server
    reports) first, then the suggested models of configured cloud providers (not in the strict offline mode).

    A capability a cloud model's provider marks ``unsupported`` excludes it; ``unknown`` does not (the action's own
    checks still apply when it runs). Nothing is switched here: the person chooses."""
    needs = needs or ModelNeeds()
    found: list[ModelAlternative] = []
    skip = local_name(exclude) if exclude and is_local(exclude) else exclude
    service = ws.local_models
    if service.ollama_url() is not None or service.provides_endpoint():
        with suppress(NodestroError, ValueError, OllamaError):
            for item in service.installed():
                if local_name(item.name) == skip or _lacking(needs, item.capabilities, kind=item.kind):
                    continue
                found.append(_alternative(ws, f"ollama:{item.name}", needs, local=True, installed=item))
    if needs.kind == "chat" and not ws.strict_offline():
        for model in ws.allowed_models():
            if is_local(model) or model == skip:
                continue
            caps = ws.builder.capabilities(model)
            if any(_support(caps, name) == "unsupported" for name in needs.capabilities):
                continue
            found.append(_alternative(ws, model, needs, local=False))
    return found[:limit]


def _support(caps: Any, name: str) -> str:
    try:
        return str(caps.support(name))
    except KeyError:
        return "unknown"


def _alternative(
    ws: Workspace, model: str, needs: ModelNeeds, *, local: bool, installed: InstalledModel | None = None
) -> ModelAlternative:
    limits = ws.builder.limits(model) if not local else None
    window = installed.context_length if installed is not None else (limits.context_window if limits else None)
    maximum = limits.max_output_tokens if limits is not None else None
    return ModelAlternative(
        model=model,
        provider=provider_of(model),
        local=local,
        context_window=window,
        max_output_tokens=maximum,
        limited=bool(needs.output_tokens and maximum and maximum < needs.output_tokens),
    )


def readiness(
    ws: Workspace,
    model: str,
    *,
    action: str,
    needs: ModelNeeds | None = None,
    can_install: bool,
    switchable: bool = False,
) -> Readiness:
    """The check the studio runs before an action and again after a download or setup (the dialog's "continue")."""
    problem = diagnose(ws, model, action=action, needs=needs, can_install=can_install, switchable=switchable)
    others = alternatives(ws, needs, exclude=model) if problem is not None and switchable else []
    if problem is not None and not others and "choose_model" in problem.actions:
        problem.actions.remove("choose_model")
    return Readiness(model=model, ready=problem is None, problem=problem, alternatives=others)


def require_ready(
    ws: Workspace,
    model: str,
    *,
    action: str,
    needs: ModelNeeds | None = None,
    can_install: bool,
    switchable: bool = False,
) -> None:
    """Refuse an interactive action whose local model is not ready (``model.local_unavailable``), before anything is
    stored, sent or charged. The parameters carry the :class:`ModelProblem` and the alternatives."""
    found = readiness(ws, model, action=action, needs=needs, can_install=can_install, switchable=switchable)
    if found.problem is None:
        return
    problem = found.problem
    raise NodestroError(
        UNAVAILABLE,
        f"{problem.model} cannot be used now ({problem.cause})",
        model=problem.model,
        cause=problem.cause,
        problem=problem.model_dump(mode="json"),
        alternatives=[item.model_dump(mode="json") for item in found.alternatives],
    )


def check_override(ws: Workspace, model: str, configured: str, *, needs: ModelNeeds | None = None) -> str:
    """A model chosen for one action instead of the configured one: allowed only when it is one of the
    :func:`alternatives` (ready, capable, of a configured provider), so a request cannot pick any model at all."""
    if model == configured:
        return model
    allowed = {item.model for item in alternatives(ws, needs, exclude=configured, limit=100)}
    if model not in allowed:
        raise NodestroError(
            "model.choice_invalid", f"{model} cannot be used for this action here", model=model, configured=configured
        )
    return model


__all__ = [
    "UNAVAILABLE",
    "Action",
    "Cause",
    "ModelAlternative",
    "ModelNeeds",
    "ModelProblem",
    "Readiness",
    "alternatives",
    "check_override",
    "diagnose",
    "is_local",
    "local_name",
    "readiness",
    "require_ready",
]
