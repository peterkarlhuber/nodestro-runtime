"""Local models: the catalog of verified Ollama models and the task- and hardware-aware model selection.

Nodestro runs agents on local models through Ollama (``ollama:<tag>``) without a cloud API key. This module knows
which models Nodestro suggests (:data:`CATALOG`, each tag, digest and size checked against the Ollama registry), what
a kind of work needs (:data:`TASKS`) and what this computer can run (:func:`detect_hardware`). :func:`recommend`
combines them: the best model for a task that fits the hardware, preferring models that are already installed.

A recommendation is a suggestion. A model a person chose always wins, and nothing here changes a stored workflow,
agent or setting: callers store the recommended name explicitly when they create something new.

The capabilities listed here are the ones the Ollama library publishes. Agents use the capabilities the installed
model reports in ``/api/show`` (:mod:`nodestro_runtime.local_service`), never the ones derived from a name.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import os
import platform
import shutil
import subprocess
import sys
from typing import Literal

from pydantic import BaseModel, Field

#: The Ollama release Nodestro is validated with (the one ``orkexa.local`` installs).
OLLAMA_VERSION = "0.40.14"
#: The models new installations use (stored explicitly in their settings, never as an implicit fallback).
LOCAL_DEFAULT_MODEL = "ollama:qwen3.5:4b"
LOCAL_ASSISTANT_MODEL = "ollama:qwen3.5:9b"
LOCAL_EMBEDDING_MODEL = "qwen3-embedding:0.6b"

ModelKind = Literal["chat", "embedding"]
Quality = Literal["recommended", "acceptable", "limited", "insufficient"]
#: Where the model's publisher is based (its origin, not where it runs: every catalog model runs on this computer).
Region = Literal["eu", "us", "cn", "other"]

#: The six interface languages of Nodestro (the ones a model must list for "all languages of the studio").
STUDIO_LANGUAGES = ("de", "en", "es", "fr", "it", "pt")

_GB = 1_000_000_000


class LocalModelInfo(BaseModel):
    """A model of the catalog (facts checked against the Ollama registry on 2026-11-03)."""

    #: The Ollama tag, e.g. ``qwen3.5:4b`` (agents store ``ollama:<name>``).
    name: str
    kind: ModelKind
    family: str
    #: Download size in bytes (the default Q4_K_M/Q8_0 build).
    download_bytes: int
    #: The digest of the registry manifest, as ``ollama list`` shows it after a download.
    digest: str
    #: The capabilities the Ollama library lists (``/api/show`` of the installed model is what counts).
    listed_capabilities: list[str]
    min_ollama: str
    license: str
    #: Estimated memory to run it with a moderate context (RAM, or GPU memory when it runs there), in bytes.
    memory_bytes: int
    #: Rank among the chat models (1 small ... 3 large); 0 for embedding models.
    rank: int = 0
    #: Whether it runs at a usable speed on a CPU only (a dense 27B model does not).
    cpu_friendly: bool = True
    context: int | None = None
    dimensions: int | None = None
    notes: str = ""
    #: The publisher and its region (origin only: open weights run offline wherever they come from).
    vendor: str = ""
    region: Region = "other"
    #: Languages the publisher documents (ISO 639-1); an unlisted language is unverified, not unsupported.
    languages: list[str] = Field(default_factory=list)
    #: The tasks it is meant for (keys of :data:`TASKS`); empty: every task its capabilities and rank allow.
    tasks: list[str] = Field(default_factory=list)
    #: An alternative to the standard models (Qwen): recommended only when the person prefers its region, or when no
    #: standard model fits. Without a preference the standard models win between equally suited ones.
    alternative: bool = False


class SelectionPreferences(BaseModel):
    """What the person prefers beyond the task (D13): a publisher region and the languages of the work.

    A preference only decides between models that are equally suited: it never makes a model look better than it
    is, and the recommendation names it in its reasons."""

    region: Region | None = None
    languages: list[str] = Field(default_factory=list)


CATALOG: tuple[LocalModelInfo, ...] = (
    LocalModelInfo(
        name="qwen3.5:4b",
        kind="chat",
        family="qwen3.5",
        download_bytes=3_300_000_000,
        digest="d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04",
        listed_capabilities=["completion", "vision", "tools", "thinking"],
        min_ollama="0.30.0",
        license="Apache-2.0",
        vendor="Alibaba (Qwen)",
        region="cn",
        languages=list(STUDIO_LANGUAGES),
        memory_bytes=6 * _GB,
        rank=1,
        context=262_144,
        notes="Small and fast: classifying, extracting, short texts; tool use works with limits",
    ),
    LocalModelInfo(
        name="qwen3.5:9b",
        kind="chat",
        family="qwen3.5",
        download_bytes=6_600_000_000,
        digest="56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90",
        listed_capabilities=["completion", "vision", "tools", "thinking"],
        min_ollama="0.30.0",
        license="Apache-2.0",
        vendor="Alibaba (Qwen)",
        region="cn",
        languages=list(STUDIO_LANGUAGES),
        memory_bytes=10 * _GB,
        rank=2,
        notes="Everyday agent work: writing, tools, documents and images",
    ),
    LocalModelInfo(
        name="qwen3.6:35b-a3b",
        kind="chat",
        family="qwen3.6",
        download_bytes=22_600_000_000,
        digest="a7eb95c53bcf96b4bdd008d0fab4a5dac88047d9c1a7a9ab88ed453423fbd87c",
        listed_capabilities=["completion", "vision", "tools", "thinking"],
        min_ollama="0.30.0",
        license="Apache-2.0",
        vendor="Alibaba (Qwen)",
        region="cn",
        languages=list(STUDIO_LANGUAGES),
        memory_bytes=26 * _GB,
        rank=3,
        notes="Demanding work; a mixture of experts (3B active), fast enough on a CPU with enough memory",
    ),
    LocalModelInfo(
        name="qwen3.8:27b",
        kind="chat",
        family="qwen3.8",
        download_bytes=17_700_000_000,
        digest="aaee06c39dcf2437cde036998d960e1fc1494b8191be7cc9657d01e509097813",
        listed_capabilities=["completion", "vision", "tools", "thinking"],
        min_ollama="0.32.12",
        license="Apache-2.0",
        vendor="Alibaba (Qwen)",
        region="cn",
        languages=list(STUDIO_LANGUAGES),
        memory_bytes=22 * _GB,
        rank=3,
        cpu_friendly=False,
        notes="Demanding work on a GPU (a dense model: slow on a CPU)",
    ),
    # European alternatives (Peter's approval of 2026-11-03; facts from the Ollama registry and Mistral AI's model
    # cards, checked 2026-11-03). Ministral follows instructions reliably only with a system prompt, which Nodestro
    # always sends.
    LocalModelInfo(
        name="ministral-3:3b",
        kind="chat",
        family="ministral-3",
        download_bytes=2_953_840_294,
        digest="f04aa1c738f64e13c625b82ae92504fc0260fa6723b509ed1ece0fa188179b1d",
        listed_capabilities=["completion", "vision", "tools"],
        min_ollama="0.13.1",
        license="Apache-2.0",
        vendor="Mistral AI",
        region="eu",
        languages=["en", "fr", "es", "de", "it", "pt", "nl", "zh", "ja", "ko", "ar"],
        memory_bytes=5 * _GB,
        rank=1,
        context=262_144,
        alternative=True,
        notes="European alternative for small tasks: classifying, extracting, short texts, simple tools",
    ),
    LocalModelInfo(
        name="ministral-3:8b",
        kind="chat",
        family="ministral-3",
        download_bytes=6_022_236_102,
        digest="1922accd5827ebe6829e536369195db25eaf664528dc66206d646ea3bb386b71",
        listed_capabilities=["completion", "vision", "tools"],
        min_ollama="0.13.1",
        license="Apache-2.0",
        vendor="Mistral AI",
        region="eu",
        languages=["en", "fr", "es", "de", "it", "pt", "nl", "zh", "ja", "ko", "ar"],
        memory_bytes=9 * _GB,
        rank=2,
        context=262_144,
        alternative=True,
        notes="European alternative for everyday agent work: writing, tools, documents and images",
    ),
    LocalModelInfo(
        name="ministral-3:14b",
        kind="chat",
        family="ministral-3",
        download_bytes=9_082_537_031,
        digest="4760c35aeb9d9e9c6174c2492562c0b999e80a222804fd96b1915ab72bbcdcf7",
        listed_capabilities=["completion", "vision", "tools"],
        min_ollama="0.13.1",
        license="Apache-2.0",
        vendor="Mistral AI",
        region="eu",
        languages=["en", "fr", "es", "de", "it", "pt", "nl", "zh", "ja", "ko", "ar"],
        memory_bytes=12 * _GB,
        rank=2,
        context=262_144,
        alternative=True,
        notes="European alternative for more demanding everyday work; slower on a CPU (not yet tested by Nodestro)",
    ),
    LocalModelInfo(
        name="devstral-small-2:24b",
        kind="chat",
        family="devstral-small-2",
        download_bytes=15_177_373_679,
        digest="24277f07f62db8f9cb68e9dfc679ea1818a7fbac47a50eff0a701d3f645b63c8",
        listed_capabilities=["completion", "vision", "tools"],
        min_ollama="0.13.3",
        license="Apache-2.0 (Mistral AI's model card; the Ollama manifest has no license layer)",
        vendor="Mistral AI",
        region="eu",
        languages=["en", "fr", "es", "de", "it", "pt", "nl", "zh", "ja", "ko", "ar"],
        memory_bytes=20 * _GB,
        rank=3,
        cpu_friendly=False,
        tasks=["coding"],
        alternative=True,
        notes="European coding agent model for a GPU; context length unconfirmed (the sources differ; not yet tested)",
    ),
    LocalModelInfo(
        name="qwen3-embedding:0.6b",
        kind="embedding",
        family="qwen3-embedding",
        download_bytes=640_000_000,
        digest="ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d",
        listed_capabilities=["embedding"],
        min_ollama="0.30.0",
        license="Apache-2.0 (upstream; the Ollama manifest has no license layer)",
        vendor="Alibaba (Qwen)",
        region="cn",
        languages=list(STUDIO_LANGUAGES),
        memory_bytes=2 * _GB,
        context=32_768,
        dimensions=1024,
        notes="Semantic search in many languages; small and fast",
    ),
    LocalModelInfo(
        name="qwen3-embedding:4b",
        kind="embedding",
        family="qwen3-embedding",
        download_bytes=2_500_000_000,
        digest="df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907",
        listed_capabilities=["embedding"],
        min_ollama="0.30.0",
        license="Apache-2.0 (upstream; the Ollama manifest has no license layer)",
        vendor="Alibaba (Qwen)",
        region="cn",
        languages=list(STUDIO_LANGUAGES),
        memory_bytes=5 * _GB,
        notes="More precise semantic search; slower indexing",
    ),
    LocalModelInfo(
        name="qwen3-embedding:8b",
        kind="embedding",
        family="qwen3-embedding",
        download_bytes=4_700_000_000,
        digest="64b933495768fbd3b87c20583d379728a07471e0c66733a9df87cd1901b3c44b",
        listed_capabilities=["embedding"],
        min_ollama="0.30.0",
        license="Apache-2.0 (upstream; the Ollama manifest has no license layer)",
        vendor="Alibaba (Qwen)",
        region="cn",
        languages=list(STUDIO_LANGUAGES),
        memory_bytes=8 * _GB,
        notes="The most precise semantic search; needs a GPU for large knowledge bases",
    ),
)

_BY_NAME = {model.name: model for model in CATALOG}


def catalog_model(name: str) -> LocalModelInfo | None:
    """The catalog entry of an Ollama tag (``ollama:`` prefix allowed); ``None`` for other models."""
    return _BY_NAME.get(name.removeprefix("ollama:"))


# ---- tasks -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskProfile:
    """What a kind of work needs: capabilities and the smallest and the ideal model rank."""

    id: str
    needs: tuple[str, ...]
    minimum: int
    ideal: int
    description: str


TASKS: Mapping[str, TaskProfile] = {
    task.id: task
    for task in (
        TaskProfile("classify", ("completion",), 1, 1, "Sort, route or label short texts"),
        TaskProfile("extract", ("completion",), 1, 2, "Pull fields out of texts into a structured result"),
        TaskProfile("write", ("completion",), 1, 2, "Write, rewrite or summarize texts"),
        TaskProfile("document_vision", ("completion", "vision"), 1, 2, "Read images and scanned documents"),
        TaskProfile("tool_agent", ("completion", "tools"), 1, 2, "Work with tools and knowledge search"),
        TaskProfile("analysis", ("completion",), 2, 3, "Demanding analysis and reasoning"),
        TaskProfile("coding", ("completion", "tools"), 2, 3, "Write and fix code"),
        TaskProfile("judge", ("completion",), 2, 2, "Check results against quality criteria"),
        TaskProfile("orchestrator", ("completion", "tools"), 2, 3, "Plan work and steer a team of agents"),
        TaskProfile("architect", ("completion", "tools"), 2, 3, "Design workflows (the Workflow Architect)"),
        TaskProfile("assistant", ("completion", "tools"), 2, 2, "The assistants of Nodestro Studio"),
        TaskProfile("embedding", ("embedding",), 0, 0, "Semantic knowledge search (embeddings)"),
    )
}

#: The model tiers of the examples (``fast``, ``standard``, ``strong``) and the task each stands for.
TIER_TASKS: Mapping[str, str] = {"fast": "classify", "standard": "write", "strong": "analysis", "judge": "judge"}


# ---- hardware ----------------------------------------------------------------------------------------------------


class Hardware(BaseModel):
    """What this computer offers to local models (memory sizes in bytes; ``None``: not known)."""

    system: str
    machine: str
    cpus: int
    ram_bytes: int | None = None
    #: ``nvidia`` (memory from nvidia-smi), ``apple`` (Apple silicon: memory shared with the GPU) or ``none``.
    gpu: Literal["nvidia", "apple", "none"] = "none"
    gpu_memory_bytes: int | None = None


def _ram_bytes() -> int | None:
    if sys.platform == "win32":
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("load", ctypes.c_ulong),
                ("total", ctypes.c_ulonglong),
                ("available", ctypes.c_ulonglong),
                ("total_page", ctypes.c_ulonglong),
                ("available_page", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended", ctypes.c_ulonglong),
            ]

        status = _Status()
        status.length = ctypes.sizeof(_Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return int(status.total)
        return None
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3, check=True)
            return int(out.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None


def _nvidia_memory() -> int | None:
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return None
    try:
        out = subprocess.run(
            [binary, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sizes = [int(line.strip()) for line in out.stdout.splitlines() if line.strip().isdigit()]
    return max(sizes) * 1024 * 1024 if sizes else None


def detect_hardware() -> Hardware:
    """Memory and GPU of this computer (runs ``nvidia-smi`` when it is installed; never anything else)."""
    ram = _ram_bytes()
    machine = platform.machine().lower()
    hardware = Hardware(system=sys.platform, machine=machine, cpus=os.cpu_count() or 1, ram_bytes=ram)
    if sys.platform == "darwin" and machine in ("arm64", "aarch64"):
        # Apple silicon: Ollama runs models on the GPU in the shared memory (about three quarters of it).
        hardware.gpu = "apple"
        hardware.gpu_memory_bytes = int(ram * 0.75) if ram else None
        return hardware
    memory = _nvidia_memory()
    if memory is not None:
        hardware.gpu = "nvidia"
        hardware.gpu_memory_bytes = memory
    return hardware


def _in_gpu(model: LocalModelInfo, hardware: Hardware) -> bool:
    return hardware.gpu_memory_bytes is not None and hardware.gpu_memory_bytes >= model.memory_bytes


def fits(model: LocalModelInfo, hardware: Hardware) -> bool:
    """Whether the model can run here: in GPU memory, or in RAM (CPU, if the model is fast enough there)."""
    if _in_gpu(model, hardware):
        return True
    if not model.cpu_friendly:
        return False
    # Unknown RAM: assume a common 16 GB computer rather than recommending nothing.
    ram = hardware.ram_bytes if hardware.ram_bytes is not None else 16 * _GB
    return ram >= model.memory_bytes


# ---- recommendations ---------------------------------------------------------------------------------------------


class Recommendation(BaseModel):
    """The model suggested for a task, and why."""

    task: str
    #: The qualified name to store (``ollama:<tag>``); ``None`` when no local model fits.
    model: str | None
    quality: Quality
    installed: bool = False
    #: Bytes to download before it can be used (0 when installed).
    download_bytes: int = 0
    #: Machine-readable reasons, e.g. ``installed``, ``below_ideal``, ``hardware_limit``, ``needs_download``,
    #: ``no_gpu`` and ``no_fit``; the interface shows its own texts for them.
    reasons: list[str] = Field(default_factory=list)
    #: The model that would suit the task better (not installed, or too large for this computer).
    better: str | None = None
    #: Every catalog model of the right kind that would work here, best first.
    alternatives: list[str] = Field(default_factory=list)


def _installed_names(installed: Iterable[str]) -> set[str]:
    return {name.removeprefix("ollama:") for name in installed}


def recommend(
    task: str,
    hardware: Hardware,
    installed: Iterable[str] = (),
    *,
    capabilities: Mapping[str, Iterable[str]] | None = None,
    preferences: SelectionPreferences | None = None,
) -> Recommendation:
    """The local model for ``task`` (a key of :data:`TASKS`) on ``hardware``.

    An installed model that is good enough wins over a better one that needs a download; among the models that fit,
    the largest up to the task's ideal is chosen. ``capabilities`` (from ``/api/show`` of installed models, by name)
    replace the listed ones where known: a model that does not report a needed capability is not suggested.
    ``preferences``: models that document all the work's languages come first where any do; a preferred region
    decides only between models of the same suitability.
    """
    profile = TASKS.get(task)
    if profile is None:
        raise ValueError(f"Unknown task {task!r}")
    present = _installed_names(installed)
    reported = {name.removeprefix("ollama:"): set(values) for name, values in (capabilities or {}).items()}

    wishes = preferences or SelectionPreferences()
    notes: list[str] = []

    def capable(model: LocalModelInfo) -> bool:
        known = reported.get(model.name, set(model.listed_capabilities))
        return all(need in known for need in profile.needs)

    def speaks(models: list[LocalModelInfo]) -> list[LocalModelInfo]:
        if not wishes.languages:
            return models
        wanted = {code.split("-")[0].lower() for code in wishes.languages}
        listed = [m for m in models if wanted <= set(m.languages)]
        if listed:
            return listed
        notes.append("languages_unverified")
        return models

    def other_region(model: LocalModelInfo) -> bool:
        return wishes.region is not None and model.region != wishes.region

    def unrequested(model: LocalModelInfo) -> bool:
        """An alternative model nobody asked for: without a region preference the standard models come first."""
        return wishes.region is None and model.alternative

    if profile.id == "embedding":
        candidates = speaks([m for m in CATALOG if m.kind == "embedding" and capable(m) and fits(m, hardware)])
        # The small model stays the default: another embedding model means reindexing everything.
        ordered = sorted(candidates, key=lambda m: (m.name not in present, other_region(m), m.memory_bytes))
        return _result(
            profile, ordered, present, hardware, ideal_name=LOCAL_EMBEDDING_MODEL, wishes=wishes, notes=notes
        )
    chat = speaks(
        [
            m
            for m in CATALOG
            if m.kind == "chat" and capable(m) and m.rank >= profile.minimum and (not m.tasks or profile.id in m.tasks)
        ]
    )
    usable = [m for m in chat if fits(m, hardware)]
    # Prefer the CPU-friendly model of a rank when no GPU holds the dense one.
    # Prefer a model that fits in GPU memory, then the CPU-friendly one of a rank, then the smaller one.
    usable.sort(
        key=lambda m: (
            unrequested(m),
            -min(m.rank, profile.ideal),
            other_region(m),
            not _in_gpu(m, hardware),
            not m.cpu_friendly,
            m.memory_bytes,
        )
    )
    ideal = next(
        (
            m
            for m in sorted(
                chat, key=lambda m: (unrequested(m), m.rank != profile.ideal, other_region(m), not m.cpu_friendly)
            )
        ),
        None,
    )
    installed_usable = [m for m in usable if m.name in present and not unrequested(m)]
    ordered = installed_usable + [m for m in usable if m not in installed_usable]
    if wishes.region is not None and usable:
        # A preferred region wins over an installed model of another region, but only among the best suited
        # models: the preference may suggest a download, never a weaker model.
        best = min(usable[0].rank, profile.ideal)
        preferred = [m for m in usable if m.region == wishes.region and min(m.rank, profile.ideal) == best]
        preferred.sort(key=lambda m: m.name not in present)
        if preferred:
            # Only the choice moves: an installed model stays the first alternative.
            ordered = [preferred[0]] + [m for m in ordered if m is not preferred[0]]
    return _result(
        profile, ordered, present, hardware, ideal_name=ideal.name if ideal else None, wishes=wishes, notes=notes
    )


def _result(
    profile: TaskProfile,
    ordered: list[LocalModelInfo],
    present: set[str],
    hardware: Hardware,
    *,
    ideal_name: str | None,
    wishes: SelectionPreferences,
    notes: list[str],
) -> Recommendation:
    if not ordered:
        return Recommendation(
            task=profile.id, model=None, quality="insufficient", reasons=["no_fit", *notes], better=None
        )
    chosen = ordered[0]
    reasons: list[str] = []
    installed = chosen.name in present
    reasons.append("installed" if installed else "needs_download")
    ideal = catalog_model(ideal_name) if ideal_name else None
    quality: Quality = "recommended"
    better: str | None = None
    if profile.id != "embedding" and chosen.rank < profile.ideal:
        quality = "limited" if chosen.rank == 1 else "acceptable"
        reasons.append("below_ideal")
        if ideal is not None and ideal.name != chosen.name:
            better = f"ollama:{ideal.name}"
            if not fits(ideal, hardware):
                reasons.append("hardware_limit")
    if hardware.gpu == "none" and profile.ideal >= 3:
        reasons.append("no_gpu")  # the dense large model is left out: too slow on a CPU
    if wishes.region is not None:
        # Named either way, so the choice stays explainable: the preference was met, or no equal model had it.
        reasons.append("preferred_region" if chosen.region == wishes.region else "preferred_region_unavailable")
    reasons.extend(notes)
    return Recommendation(
        task=profile.id,
        model=f"ollama:{chosen.name}",
        quality=quality,
        installed=installed,
        download_bytes=0 if installed else chosen.download_bytes,
        reasons=reasons,
        better=better,
        alternatives=[f"ollama:{m.name}" for m in ordered[1:]],
    )


def recommend_all(
    hardware: Hardware,
    installed: Iterable[str] = (),
    *,
    capabilities: Mapping[str, Iterable[str]] | None = None,
    preferences: SelectionPreferences | None = None,
) -> list[Recommendation]:
    """A recommendation for every task of :data:`TASKS`."""
    names = list(installed)
    return [recommend(task, hardware, names, capabilities=capabilities, preferences=preferences) for task in TASKS]


__all__ = [
    "CATALOG",
    "LOCAL_ASSISTANT_MODEL",
    "LOCAL_DEFAULT_MODEL",
    "LOCAL_EMBEDDING_MODEL",
    "OLLAMA_VERSION",
    "STUDIO_LANGUAGES",
    "TASKS",
    "TIER_TASKS",
    "Hardware",
    "LocalModelInfo",
    "Recommendation",
    "Region",
    "SelectionPreferences",
    "TaskProfile",
    "catalog_model",
    "detect_hardware",
    "fits",
    "recommend",
    "recommend_all",
]
