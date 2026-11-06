"""Steps and measured progress of the local model server's jobs: setting up the server, downloading models.

Fakes stand in for Ollama (a client whose pull yields Ollama's real status sequence) and for the runtime download;
the real runs (a managed install from the release page, real pulls, a full disk, a broken network) are described in
docs/local-models.md ("Setup and download progress")."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any

from orkexa.local import OllamaError
from orkexa.local.client import PullProgress as Line
import pytest

from nodestro_runtime import local_service
from nodestro_runtime.local_models import Hardware
from nodestro_runtime.local_progress import (
    PULL_STEPS,
    SETUP_STEPS,
    JobLayer,
    LocalJob,
    Meter,
    PullProgress,
    Tracker,
    now,
)
from nodestro_runtime.local_service import LocalModelService
from nodestro_runtime.problems import NodestroError

MB = 1_000_000


class Clock:
    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at


class Store:
    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], Any] = {}

    def get_raw(self, collection: str, key: str) -> Any:
        value = self.docs.get((collection, key))
        return json.loads(json.dumps(value)) if value is not None else None

    def put_raw(self, collection: str, key: str, value: Any) -> None:
        self.docs[(collection, key)] = json.loads(json.dumps(value))


def _job(kind: str = "pull") -> LocalJob:
    return LocalJob(id="j1", kind=kind, target="m", started_at=now())  # type: ignore[arg-type]


# ---- the measurement ---------------------------------------------------------------------------------------------


def test_the_rate_is_given_only_after_a_few_seconds_and_the_eta_only_when_steady() -> None:
    clock = Clock()
    job = _job("setup")
    tracker = Tracker(job, SETUP_STEPS, clock=clock)
    tracker.enter("download")
    for second in range(3):
        tracker.progress(second * 10 * MB, 100 * MB)
        assert job.rate is None and job.eta_seconds is None  # less than three seconds measured
        clock.at += 1
    tracker.progress(30 * MB, 100 * MB)
    assert job.rate == pytest.approx(10 * MB)
    assert job.eta_seconds is None  # the rate is known, the remaining time not yet (five seconds)
    for second in range(4, 7):
        clock.at += 1
        tracker.progress(second * 10 * MB, 100 * MB)
    assert job.eta_seconds == 4 and job.eta_scope == "job"
    # A stall: no new bytes. The rate falls, no remaining time is given, and the stall is named after 5 s.
    for _ in range(6):
        clock.at += 1
        tracker.tick()
    assert job.eta_seconds is None
    assert job.stalled_seconds is not None and job.stalled_seconds >= 5
    assert job.rate is not None and job.rate < 10 * MB
    tracker.progress(61 * MB, 100 * MB)
    assert job.stalled_seconds is None


def test_a_checksum_has_a_percentage_but_no_transfer_rate() -> None:
    clock = Clock()
    job = _job("setup")
    tracker = Tracker(job, SETUP_STEPS, clock=clock)
    tracker.enter("download")
    tracker.progress(100, 100)
    tracker.enter("verify")
    assert job.progress_step == "download"  # until the checksum reports bytes
    for second in range(10):
        clock.at += 1
        tracker.progress(second, 10, rate=False)
    assert job.progress_step == "verify" and job.rate is None and job.eta_seconds is None
    assert [step.state for step in job.steps[:4]] == ["skipped", "skipped", "done", "running"]


def test_meter_windows() -> None:
    clock = Clock()
    meter = Meter(clock)
    assert meter.rate() is None and meter.still() == 0
    for second in range(20):
        meter.add(second * 100)
        clock.at += 1
    assert meter.rate() == pytest.approx(100)
    assert meter.rate(3) == pytest.approx(100)


# ---- a model download: Ollama's statuses ------------------------------------------------------------------------


def _ollama_pull(first: int = 0) -> list[Line]:
    """Ollama 0.40's sequence for a model with two files (seen in a real pull of qwen3.5:4b): one file at a time."""
    big, small = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    lines = [Line("pulling manifest")]
    lines += [Line(f"pulling {big[7:19]}", big, 600 * MB, done) for done in (first, 200 * MB, 590 * MB)]
    lines += [Line(f"pulling {small[7:19]}", small, 2 * MB, 2 * MB)]
    lines += [Line("verifying sha256 digest"), Line("writing manifest"), Line("success")]
    return lines


def test_ollama_statuses_become_steps_and_the_bytes_of_all_files_add_up() -> None:
    clock = Clock()
    job = _job()
    tracker = Tracker(job, PULL_STEPS, clock=clock)
    progress = PullProgress(tracker, {"sha256:" + "a" * 64: 150 * MB})
    seen: list[tuple[str | None, int, int | None, bool]] = []
    for line in _ollama_pull(first=150 * MB):
        clock.at += 1
        progress.update(line.status, line.digest, line.total, line.completed)
        seen.append((job.phase, job.completed, job.total, job.total_final))
    assert seen[0][0] == "manifest"
    assert seen[1] == ("download", 150 * MB, 600 * MB, False)  # the next file is not known yet
    assert seen[4] == ("download", 592 * MB, 602 * MB, False)
    # Verified: every file is complete (Ollama's last line of a file can be a little short), the total is final.
    assert seen[5] == ("verify", 602 * MB, 602 * MB, True)
    assert seen[6][0] == "finalize"
    assert job.resumed_bytes == 150 * MB  # the part an earlier, interrupted download left
    assert [layer.total for layer in job.layers] == [600 * MB, 2 * MB]
    assert job.phase is None and [step.state for step in job.steps] == ["skipped"] + ["done"] * 4


def test_a_model_download_gives_the_remaining_time_of_the_current_file() -> None:
    clock = Clock()
    job = _job()
    tracker = Tracker(job, PULL_STEPS, clock=clock)
    progress = PullProgress(tracker)
    digest = "sha256:" + "c" * 64
    for second in range(8):
        progress.update("pulling cccc", digest, 100 * MB, second * 10 * MB)
        clock.at += 1
    assert job.total_final is False
    assert job.eta_scope == "file" and job.eta_seconds == 3  # 30 MB left at 10 MB/s
    assert job.layers == [JobLayer(digest=digest, total=100 * MB, completed=70 * MB)]


# ---- the service ------------------------------------------------------------------------------------------------


class FakeClient:
    """An Ollama: ``pull`` yields the given lines, waits on ``gate`` (to keep a job running) or raises ``error``."""

    pulls: list[str]
    gate: threading.Event | None = None
    error: OllamaError | None = None
    lines: list[Line]
    answering = True

    def __init__(self, url: str) -> None:
        self.url = url

    def version(self, timeout: float = 3.0) -> str:
        if not FakeClient.answering:
            raise OllamaError("unreachable", "not reachable")
        return "0.40.14"

    def list_models(self) -> list[Any]:
        return []

    def running(self) -> list[Any]:
        return []

    def pull(self, name: str, *, cancel: threading.Event | None = None, timeout: float = 60.0) -> Iterator[Line]:
        FakeClient.pulls.append(name)
        for line in FakeClient.lines:
            yield line
            if FakeClient.gate is not None and line.digest:
                while not FakeClient.gate.wait(0.02):
                    if cancel is not None and cancel.is_set():
                        raise OllamaError("cancelled", f"The download of {name} was cancelled")
            if FakeClient.error is not None and line.digest:
                raise FakeClient.error


@pytest.fixture(autouse=True)
def _fake_client() -> Iterator[None]:
    FakeClient.pulls, FakeClient.gate, FakeClient.error = [], None, None
    FakeClient.lines, FakeClient.answering = _ollama_pull(), True
    yield
    if FakeClient.gate is not None:
        FakeClient.gate.set()


def _service(
    tmp_path: Path, *, store: Store | None = None, external: str | None = None, **env: str
) -> LocalModelService:
    service = LocalModelService(
        tmp_path / "data",
        "local-container" if "NODESTRO_OLLAMA_URL" in env else "desktop",
        store=store or Store(),
        offline=lambda: False,
        external_url=lambda: external,
        environ=env,
        hardware=lambda: Hardware(system="linux", machine="x86_64", cpus=4, ram_bytes=16 * 1024**3),
        client_factory=FakeClient,  # type: ignore[arg-type]
    )
    service.detect_external = lambda: None
    return service


def _wait(service: LocalModelService, job: LocalJob) -> LocalJob:
    deadline = time.monotonic() + 10
    while service.job(job.id).state == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    return service.job(job.id)


def test_downloads_of_several_models_are_separate_and_one_model_downloads_once(tmp_path: Path) -> None:
    service = _service(tmp_path, NODESTRO_OLLAMA_URL="http://127.0.0.1:9", NODESTRO_OLLAMA_MODELS_DIR=str(tmp_path))
    FakeClient.gate = threading.Event()
    first = service.pull("qwen3.5:4b")
    second = service.pull("qwen3-embedding:0.6b")
    with pytest.raises(NodestroError) as error:
        service.pull("qwen3.5:4b")  # the same model is already downloading
    assert error.value.code == "local.job_running" and error.value.params["job"] == first.id
    FakeClient.gate.set()
    assert _wait(service, first).state == "done" and _wait(service, second).state == "done"
    assert {job.target for job in service.jobs()} == {"qwen3.5:4b", "qwen3-embedding:0.6b"}
    assert all(job.completed == job.total == 602 * MB for job in service.jobs())


def test_a_cancelled_download_can_be_started_again(tmp_path: Path) -> None:
    service = _service(tmp_path, NODESTRO_OLLAMA_URL="http://127.0.0.1:9", NODESTRO_OLLAMA_MODELS_DIR=str(tmp_path))
    FakeClient.gate = threading.Event()
    job = service.pull("qwen3.5:4b")
    deadline = time.monotonic() + 5
    while service.job(job.id).phase != "download" and time.monotonic() < deadline:
        time.sleep(0.02)
    service.cancel(job.id)
    done = _wait(service, job)
    assert done.state == "cancelled" and done.retryable and done.error_code == "local.cancelled"
    assert done.phase is None and next(step for step in done.steps if step.id == "download").state == "pending"
    FakeClient.gate = None
    # Ollama keeps the partial file: the next download counts it as already there.
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    (blobs / ("sha256-" + "a" * 64 + "-partial")).write_bytes(b"")  # preallocated, says nothing
    (blobs / ("sha256-" + "a" * 64 + "-partial-0")).write_text(json.dumps({"N": 0, "Completed": 123}))
    again = _wait(service, service.pull("qwen3.5:4b"))
    assert again.state == "done" and again.resumed_bytes == 123


@pytest.mark.parametrize(
    ("error", "code", "retry"),
    [
        (OllamaError("network", "max retries exceeded: dial tcp: i/o timeout"), "local.network", True),
        (OllamaError("disk_full", "write ...-partial: no space left on device"), "local.disk_full", True),
        (OllamaError("model_missing", "pull model manifest: file does not exist"), "local.model_missing", True),
    ],
)
def test_download_errors_keep_their_codes(tmp_path: Path, error: OllamaError, code: str, retry: bool) -> None:
    service = _service(tmp_path, NODESTRO_OLLAMA_URL="http://127.0.0.1:9", NODESTRO_OLLAMA_MODELS_DIR=str(tmp_path))
    FakeClient.error = error
    job = _wait(service, service.pull("qwen3.5:4b"))
    assert (job.state, job.error_code, job.retryable) == ("failed", code, retry)
    assert next(step for step in job.steps if step.id == "download").state == "failed"
    assert any(code in line for line in job.log)


def test_too_little_space_is_named_before_the_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, NODESTRO_OLLAMA_URL="http://127.0.0.1:9", NODESTRO_OLLAMA_MODELS_DIR=str(tmp_path))
    monkeypatch.setattr(local_service, "_free_bytes", lambda folder: 1_000_000)
    job = _wait(service, service.pull("qwen3.5:4b"))
    assert job.error_code == "local.disk_full" and FakeClient.pulls == []
    assert job.steps[0].id == "check" and job.steps[0].state == "failed"


def test_jobs_survive_a_restart_and_a_running_one_is_named_interrupted(tmp_path: Path) -> None:
    store = Store()
    service = _service(
        tmp_path, store=store, NODESTRO_OLLAMA_URL="http://127.0.0.1:9", NODESTRO_OLLAMA_MODELS_DIR=str(tmp_path)
    )
    done = _wait(service, service.pull("qwen3-embedding:0.6b"))
    FakeClient.gate = threading.Event()
    running = service.pull("qwen3.5:4b")
    deadline = time.monotonic() + 5
    while service.job(running.id).phase != "download" and time.monotonic() < deadline:
        time.sleep(0.02)
    # The application stops (its threads end with it); a new one reads the stored jobs.
    restarted = _service(
        tmp_path, store=store, NODESTRO_OLLAMA_URL="http://127.0.0.1:9", NODESTRO_OLLAMA_MODELS_DIR=str(tmp_path)
    )
    assert restarted.job(done.id).state == "done"
    interrupted = restarted.job(running.id)
    assert (interrupted.state, interrupted.error_code, interrupted.retryable) == ("failed", "local.interrupted", True)
    assert interrupted.steps[2].state == "failed" and interrupted.phase is None
    FakeClient.gate.set()


def test_an_orderly_shutdown_records_the_running_job_as_interrupted_not_canceled(tmp_path: Path) -> None:
    store = Store()
    env = {"NODESTRO_OLLAMA_URL": "http://127.0.0.1:9", "NODESTRO_OLLAMA_MODELS_DIR": str(tmp_path)}
    service = _service(tmp_path, store=store, **env)
    FakeClient.gate, FakeClient.lines = threading.Event(), _ollama_pull(first=200 * MB)
    running = service.pull("qwen3.5:4b")
    deadline = time.monotonic() + 5
    while service.job(running.id).completed == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    reached = service.job(running.id).completed
    service.close()  # the application ends: its job thread sees the cancel and ends too
    ended = _wait(service, running)
    assert (ended.state, ended.error_code, ended.retryable) == ("failed", "local.interrupted", True)
    assert next(step for step in ended.steps if step.id == "download").state == "failed"
    restored = _service(tmp_path, store=store, **env).job(running.id)
    assert (restored.state, restored.error_code) == ("failed", "local.interrupted")
    assert restored.completed == reached > 0  # stored at the shutdown, not only every few seconds


def test_setting_up_a_users_own_server_never_starts_or_stops_it(tmp_path: Path) -> None:
    service = _service(tmp_path, external="http://127.0.0.1:9210/v1")
    assert service.mode == "external"
    job = _wait(service, service.setup())
    assert job.state == "done"
    states = {step.id: (step.state, step.note) for step in job.steps}
    assert states["check"] == ("done", "external")
    assert states["download"] == ("skipped", "external")
    assert states["configure"] == states["start"] == ("skipped", "not_managed")
    assert states["reachable"][0] == "done" and states["hardware"] == ("done", "gpu_none")
    assert service._managed is None  # nothing was started
    service.stop()
    service.restart()
    assert service._managed is None


def test_a_server_that_does_not_answer_fails_its_step(tmp_path: Path) -> None:
    service = _service(tmp_path, NODESTRO_OLLAMA_URL="http://127.0.0.1:9")
    FakeClient.answering = False
    job = _wait(service, service.setup())
    assert job.state == "failed" and job.error_code == "local.unreachable" and job.retryable
    assert {step.id: step.state for step in job.steps}["reachable"] == "failed"


def test_setup_downloads_verifies_and_installs_with_phases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX fake")
    gate = threading.Event()
    seen: dict[str, Any] = {}

    def install(home: Path, **kwargs: Any) -> Path:
        phase, progress = kwargs["phase"], kwargs["progress"]
        phase("download")
        progress(40, 100)  # 40 bytes of an earlier attempt
        gate.wait(5)
        progress(100, 100)
        phase("verify")
        progress(100, 100)
        phase("unpack")
        binary = home / "runtime" / "bin" / "ollama"
        binary.parent.mkdir(parents=True)
        binary.write_text("")
        seen["download"] = kwargs["download"]
        return binary

    monkeypatch.setattr(local_service, "install_runtime", install)
    monkeypatch.setattr(local_service, "artifact_for", lambda: type("A", (), {"name": "ollama-linux-amd64.tar.zst"}))
    monkeypatch.setattr(LocalModelService, "start", lambda self: "http://127.0.0.1:9")
    monkeypatch.setattr(LocalModelService, "ollama_url", lambda self: "http://127.0.0.1:9")
    service = _service(tmp_path)
    with pytest.raises(NodestroError) as error:
        service.setup()  # no runtime and no download allowed: refused at once
    assert error.value.code == "local.archive_or_download"
    job = service.setup(download=True)
    deadline = time.monotonic() + 5
    while service.job(job.id).phase != "download" and time.monotonic() < deadline:
        time.sleep(0.02)
    running = service.job(job.id)
    assert running.progress_step == "download" and running.resumed_bytes == 40
    with pytest.raises(NodestroError) as error:
        service.setup(download=True)  # one setup at a time
    assert error.value.code == "local.job_running"
    gate.set()
    job = _wait(service, job)
    assert job.state == "done" and seen["download"] is True
    assert [(step.id, step.state) for step in job.steps] == [
        ("check", "done"),
        ("prerequisites", "done"),
        ("download", "done"),
        ("verify", "done"),
        ("install", "done"),
        ("configure", "done"),
        ("start", "done"),
        ("reachable", "done"),
        ("hardware", "done"),
        ("ready", "done"),
    ]
    configure = next(step for step in job.steps if step.id == "configure")
    assert configure.detail is not None and "OLLAMA_CONTEXT_LENGTH=32768" in configure.detail


def test_setup_skips_what_is_installed_and_names_an_ollama_it_leaves_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "ollama"
    binary.write_text("")
    monkeypatch.setattr(LocalModelService, "start", lambda self: "http://127.0.0.1:9")
    monkeypatch.setattr(LocalModelService, "ollama_url", lambda self: "http://127.0.0.1:9")
    service = _service(tmp_path, NODESTRO_OLLAMA_BINARY=str(binary))
    service.detect_external = lambda: "0.30.0"
    job = _wait(service, service.setup())
    states = {step.id: (step.state, step.note) for step in job.steps}
    assert states["check"] == ("done", "bundled")
    assert states["download"] == ("skipped", "bundled")
    assert states["start"][0] == "done"
    assert any("never stops or changes" in line for line in job.log)
