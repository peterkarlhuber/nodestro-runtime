"""Progress of the local model server's jobs: steps, measured bytes, transfer rate and remaining time.

A job (:class:`LocalJob`) runs in a thread of :class:`~nodestro_runtime.local_service.LocalModelService`. Long jobs
report **steps** that match what the code does at that moment (e.g. setting up the server: check, prerequisites,
download, verify, install, configure, start, reachability, hardware, ready). A step with a byte count names itself in
``progress_step``; every other step has no percentage, and the interface shows it as running without one.

Nothing here is estimated from assumptions: ``completed``/``total`` are the bytes the code counted (or Ollama
reported), ``rate`` is measured over the last seconds and given only once the measurement spans a few seconds, and
``eta_seconds`` only when the total is final and the rate measured long enough.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
import time
from typing import Literal

from pydantic import BaseModel, Field

JobKind = Literal[
    "pull", "build", "setup", "install_runtime", "import_package", "import_gguf", "move_models", "benchmark"
]
JobState = Literal["running", "done", "failed", "cancelled"]
StepState = Literal["pending", "running", "done", "skipped", "failed"]

#: The steps of setting up the local model server (``setup``), in order.
SETUP_STEPS = (
    "check",
    "prerequisites",
    "download",
    "verify",
    "install",
    "configure",
    "start",
    "reachable",
    "hardware",
    "ready",
)
#: The steps of a model download through Ollama (``pull``): its own statuses, in order.
PULL_STEPS = ("check", "manifest", "download", "verify", "finalize")
#: The steps of building a catalog model from upstream files (``build``); ``progress`` texts of orkexa's build_model.
BUILD_STEPS = ("template", "weights", "convert", "create", "check", "install")
_BUILD_TEXTS = {
    "verify template": "template",
    "verify weights": "weights",
    "write gguf": "convert",
    "create model": "create",
    "check reference vectors": "check",
    "install model": "install",
}
#: Error codes after which trying again does not help without a change first (other settings, another file).
_FINAL_ERRORS = frozenset(
    {
        "local.offline_download",
        "local.unsupported_platform",
        "local.not_managed",
        "local.archive_or_download",
        "local.file_missing",
        "local.unavailable",
        "local.not_buildable",
    }
)
_RETRYABLE_KINDS = frozenset({"pull", "build", "setup", "install_runtime", "benchmark", "move_models"})

#: The measurement window of the transfer rate, how long it must span before a rate is given, and before a
#: remaining time is given.
RATE_WINDOW = 10.0
RATE_MIN_SPAN = 3.0
ETA_MIN_SPAN = 5.0
#: After this many seconds without new bytes a download counts as stalled (shown, so a wait is not taken for a hang).
STALL_AFTER = 5.0
_MAX_LOG = 60


class JobStep(BaseModel):
    """One step of a job, as the code runs it."""

    id: str
    state: StepState = "pending"
    #: A code the interface turns into its own text, e.g. ``bundled``, ``already_running``, ``gpu_nvidia``.
    note: str | None = None
    #: Technical detail in English (paths, versions, sizes, addresses), shown on request.
    detail: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobLayer(BaseModel):
    """One file (layer) of a model download, as Ollama reports it."""

    digest: str
    total: int | None = None
    completed: int = 0


class LocalJob(BaseModel):
    id: str
    kind: JobKind
    target: str
    state: JobState = "running"
    #: Bytes (or items) of the step named by ``progress_step``; ``total`` ``None``: not known.
    completed: int = 0
    total: int | None = None
    message: str = ""
    error_code: str | None = None
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    # --- steps and measured progress (2026-11) ---
    #: The step that runs now (``None`` before the first and after the last).
    phase: str | None = None
    steps: list[JobStep] = Field(default_factory=list)
    #: The step ``completed``/``total`` belong to; any other running step has no measurable progress.
    progress_step: str | None = None
    #: Whether ``total`` is final (a model download learns the size of each file only when Ollama starts it).
    total_final: bool = True
    #: Bytes per second, measured over the last seconds of the download; ``None`` while not measured reliably.
    rate: float | None = None
    #: Remaining seconds from ``rate``; ``None`` when that is not reliable. ``eta_scope`` says what remains: the whole
    #: job (``total`` is final) or, in a model download, the file that downloads now (the last of ``layers``; Ollama
    #: names the next file only when it starts it).
    eta_seconds: int | None = None
    eta_scope: Literal["job", "file"] | None = None
    #: Seconds without new bytes in a running download (``None``: data arrives); Ollama retries on its own meanwhile.
    stalled_seconds: int | None = None
    #: Bytes that were already there when the job started (a partial download that continues, layers present).
    resumed_bytes: int = 0
    #: A model download: its files with their sizes (technical detail).
    layers: list[JobLayer] = Field(default_factory=list)
    #: Technical lines (English), newest last.
    log: list[str] = Field(default_factory=list)
    #: Whether starting the same job again makes sense now (a download continues where it stopped).
    retryable: bool = False
    updated_at: datetime | None = None


def now() -> datetime:
    return datetime.now(UTC)


class Meter:
    """The transfer rate over the last :data:`RATE_WINDOW` seconds (monotonic clock)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._samples: deque[tuple[float, int]] = deque()

    def reset(self) -> None:
        self._samples.clear()

    def add(self, done: int) -> None:
        at = self._clock()
        self._samples.append((at, done))
        while len(self._samples) > 2 and at - self._samples[1][0] >= RATE_WINDOW:
            self._samples.popleft()

    def span(self) -> float:
        return self._samples[-1][0] - self._samples[0][0] if len(self._samples) > 1 else 0.0

    def idle(self) -> float:
        """Seconds since the last sample (``0`` without one)."""
        return self._clock() - self._samples[-1][0] if self._samples else 0.0

    def rate(self, window: float | None = None) -> float | None:
        """Bytes per second over all samples, or over the last ``window`` seconds; ``None`` while the samples span
        less than :data:`RATE_MIN_SPAN`."""
        if len(self._samples) < 2:
            return None
        end, last = self._samples[-1]
        start, first = self._samples[0]
        if window is not None:
            for at, done in self._samples:
                if end - at < window:
                    break
                start, first = at, done
        if end - start < RATE_MIN_SPAN:
            return None
        return max(0.0, (last - first) / (end - start))

    def still(self) -> float:
        """Seconds since the count last grew (``0`` without samples)."""
        if not self._samples:
            return 0.0
        end, last = self._samples[-1]
        since = end
        for at, done in reversed(self._samples):
            if done < last:
                break
            since = at
        return self._clock() - since

    def last(self) -> int | None:
        return self._samples[-1][1] if self._samples else None


class Tracker:
    """Moves a job through its steps and keeps its measured progress (used by the job's thread)."""

    def __init__(
        self,
        job: LocalJob,
        steps: Iterable[str],
        *,
        changed: Callable[[LocalJob, bool], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.job = job
        self.meter = Meter(clock)
        self._changed = changed
        if not job.steps:
            job.steps = [JobStep(id=step) for step in steps]

    def step(self, step_id: str) -> JobStep:
        for step in self.job.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def _touch(self, important: bool = False) -> None:
        self.job.updated_at = now()
        if self._changed is not None:
            self._changed(self.job, important)

    def enter(self, step_id: str, detail: str | None = None) -> None:
        """Start a step; earlier steps still running are done, pending ones before it are skipped."""
        seen = False
        for step in self.job.steps:
            if step.id == step_id:
                seen = True
                break
            if step.state == "running":
                step.state, step.finished_at = "done", now()
            elif step.state == "pending":
                step.state = "skipped"
        if not seen:
            raise KeyError(step_id)
        step = self.step(step_id)
        if step.state == "running":
            return
        step.state, step.started_at = "running", now()
        if detail:
            step.detail = detail
        self.job.phase = step_id
        self.job.rate = self.job.eta_seconds = None
        self.job.eta_scope = self.job.stalled_seconds = None
        self.meter.reset()
        self.log(f"{step_id}: started" + (f" ({detail})" if detail else ""))
        self._touch(True)

    def finish(self, step_id: str, note: str | None = None, detail: str | None = None) -> None:
        step = self.step(step_id)
        if step.state not in ("running", "pending"):
            return
        step.state, step.finished_at = "done", now()
        if step.started_at is None:
            step.started_at = step.finished_at
        if note:
            step.note = note
        if detail:
            step.detail = detail
        if self.job.phase == step_id:
            self.job.phase = None
        self.log(f"{step_id}: done" + (f" ({note})" if note else ""))
        self._touch(True)

    def skip(self, step_id: str, note: str | None = None, detail: str | None = None) -> None:
        step = self.step(step_id)
        if step.state != "pending":
            return
        step.state = "skipped"
        step.note = note
        if detail:
            step.detail = detail
        self.log(f"{step_id}: skipped" + (f" ({note})" if note else ""))
        self._touch(True)

    def progress(self, done: int, total: int | None, *, final: bool = True, rate: bool = True) -> None:
        """Bytes of the running step; ``rate``: measure the transfer rate (a download, not a checksum)."""
        job = self.job
        if job.progress_step != job.phase:
            self.meter.reset()
        job.progress_step = job.phase
        job.completed, job.total, job.total_final = done, total, final
        if rate:
            self.meter.add(done)
            self._measure()
        else:
            job.rate = job.eta_seconds = None
            job.eta_scope = job.stalled_seconds = None
        self._touch()

    def _measure(self) -> None:
        job = self.job
        measured = self.meter.rate()
        job.rate = round(measured, 1) if measured is not None else None
        job.eta_seconds, job.eta_scope = None, None
        still = self.meter.still()
        job.stalled_seconds = int(still) if still >= STALL_AFTER else None
        if not measured or self.meter.span() < ETA_MIN_SPAN:
            return
        # Only a steady rate gives a remaining time: the last seconds must match the whole window (no stall, no
        # burst), else the time would jump around.
        recent = self.meter.rate(RATE_MIN_SPAN)
        if not recent or not 0.5 <= recent / measured <= 2.0:
            return
        if job.total is not None and job.total_final and job.total >= job.completed:
            job.eta_seconds, job.eta_scope = int((job.total - job.completed) / measured + 0.5), "job"
        elif job.layers and job.layers[-1].total is not None and job.layers[-1].total >= job.layers[-1].completed:
            left = (job.layers[-1].total or 0) - job.layers[-1].completed
            job.eta_seconds, job.eta_scope = int(left / measured + 0.5), "file"

    def tick(self) -> None:
        """Called when the job is read: a download that sends nothing slows its rate down (no stale rate)."""
        last = self.meter.last()
        if self.job.state == "running" and last is not None and self.meter.idle() >= 1.0:
            self.meter.add(last)
            self._measure()

    def log(self, line: str) -> None:
        stamp = now().strftime("%H:%M:%S")
        self.job.log.append(f"{stamp} {line}")
        if len(self.job.log) > _MAX_LOG:
            del self.job.log[: len(self.job.log) - _MAX_LOG]

    def fail(self) -> None:
        """The job ended with an error or was cancelled: its running step failed (cancelled: stays as it is)."""
        for step in self.job.steps:
            if step.state == "running":
                step.state = "failed" if self.job.state == "failed" else "pending"
                step.finished_at = now()
        self._still()

    def complete(self) -> None:
        for step in self.job.steps:
            if step.state == "running":
                step.state, step.finished_at = "done", now()
        self._still()

    def _still(self) -> None:
        self.job.stalled_seconds = None
        self.job.phase = None
        self.job.rate = self.job.eta_seconds = None
        self.job.eta_scope = None


class PullProgress:
    """Ollama's ``/api/pull`` statuses as steps, with the bytes of all files of the model added up.

    Ollama reports one line per file (layer) with that file's ``total`` and ``completed``; the sum over the files
    seen so far is the job's progress. Its total is final once Ollama verifies the download (it does so after the
    last file), so until then ``total_final`` is false and no remaining time is given. A file that is already there
    (complete, or partly from an interrupted download that Ollama continues) counts as ``resumed_bytes``."""

    def __init__(self, tracker: Tracker, present: dict[str, int] | None = None) -> None:
        """``present``: bytes already in the model folder before the download started, by digest
        (``sha256:<hex>``): complete files with their size, partial ones with the bytes Ollama recorded for them."""
        self.tracker = tracker
        self.layers: dict[str, JobLayer] = {}
        self.present = dict(present or {})
        self.statuses: list[str] = []

    def update(self, status: str, digest: str = "", total: int | None = None, completed: int | None = None) -> None:
        tracker, job = self.tracker, self.tracker.job
        job.message = status
        if status not in self.statuses:
            self.statuses.append(status)
            tracker.log(f"ollama: {status}")
        if status == "pulling manifest":
            tracker.enter("manifest")
        elif status.startswith("pulling ") and digest:
            if job.phase != "download":
                tracker.enter("download")
            layer = self.layers.get(digest)
            if layer is None:
                layer = self.layers[digest] = JobLayer(digest=digest)
                job.layers.append(layer)
                before = self.present.get(digest, 0)
                if before:
                    job.resumed_bytes += before
                    tracker.log(f"{digest[:19]}: {before} bytes were already there")
            if total:
                layer.total = total
            if completed is not None:
                layer.completed = max(layer.completed, completed)
            known = [item.total for item in self.layers.values()]
            done = sum(item.completed for item in self.layers.values())
            tracker.progress(done, sum(item or 0 for item in known) or None, final=False)
        elif status.startswith("verifying"):
            self._final()
            tracker.enter("verify")
        elif status.startswith("writing manifest") or status.startswith("removing"):
            self._final()
            tracker.enter("finalize")
        elif status == "success":
            self._final()
            tracker.complete()

    def _final(self) -> None:
        """Ollama verifies only after every file is complete: its last progress line of a file may be a little short
        of the file's size, so the files count as complete now (their sizes as Ollama reported them)."""
        job = self.tracker.job
        if self.layers and job.progress_step == "download":
            for layer in self.layers.values():
                if layer.total is not None:
                    layer.completed = layer.total
            job.total_final = all(layer.total is not None for layer in self.layers.values())
            if job.total_final:
                job.completed = job.total = sum(layer.total or 0 for layer in self.layers.values())
            job.rate = job.eta_seconds = None
            job.eta_scope = None


def build_step(text: str) -> str | None:
    """The step of a ``build_model`` progress text (``None``: not a step, e.g. ``done``)."""
    return _BUILD_TEXTS.get(text)


def retryable(job: LocalJob) -> bool:
    return job.kind in _RETRYABLE_KINDS and job.state in ("failed", "cancelled") and job.error_code not in _FINAL_ERRORS


__all__ = [
    "BUILD_STEPS",
    "PULL_STEPS",
    "SETUP_STEPS",
    "JobKind",
    "JobLayer",
    "JobState",
    "JobStep",
    "LocalJob",
    "Meter",
    "PullProgress",
    "StepState",
    "Tracker",
    "build_step",
    "retryable",
]
