"""Stopping tool generation jobs (``ToolLibrary.cancel_jobs``; security review NF-1: a suspended tenant's jobs).

The model is a scripted client and the sandbox a stand-in: a host subprocess that counts as a container
(``ContainerDouble``), or Nodestro's container sandbox with a stand-in docker CLI (no Docker needed).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time
from typing import Any

from orkexa import UsageTracker
from orkexa.dynamic import SubprocessSandbox
import pytest
from test_docker_first import CODE as LIBRARY_CODE
from test_docker_first import INDEX, _Images
from test_docker_first import TESTS as LIBRARY_TESTS

from nodestro_runtime import docker_status, sandbox_cancel
from nodestro_runtime.container_images import StreamedSandbox
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import Connection
from nodestro_runtime.python_tools import EnvironmentGate
from nodestro_runtime.testing import ContainerDouble, FakeUv, ScriptedClient, StaticDockerMonitor, parsed
from nodestro_runtime.tool_library import DependencyGate, DependencyOutcome, ToolJob, ToolLibrary
from nodestro_runtime.workspace import Workspace

CODE = "suspended"
CODER = "expert Python developer"  # in the Coding Agent's system prompt
MODEL = "claude-haiku-5-5"
NAME = "add_numbers"
TOOL = '''def add_numbers(a: int, b: int) -> int:
    """Add two numbers.

    Args:
        a: The first number.
        b: The second number.

    Returns:
        The sum.
    """
    return a + b
'''
TESTS = "from tool import add_numbers\n\n\ndef test_add():\n    assert add_numbers(2, 3) == 5\n"


def _proposal(code: str = TOOL, name: str = NAME, tests: str | None = None) -> Any:
    tests = TESTS.replace(NAME, name) if tests is None else tests
    return parsed(lambda model: model(function_name=name, description="Adds numbers.", code=code, tests=tests))


class _Finished:
    """``on_finished`` of a job: when it ran and how often."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.event = threading.Event()

    def __call__(self, job: ToolJob, _tracker: UsageTracker) -> None:
        self.calls.append(job.status)
        self.event.set()


@pytest.fixture
def container() -> ContainerDouble:
    return ContainerDouble()


@pytest.fixture
def library(tmp_path: Path, container: ContainerDouble) -> Iterator[ToolLibrary]:
    yield ToolLibrary(tmp_path / "tools", sandbox=container)


def _start(library: ToolLibrary, script: list[Any]) -> tuple[ToolJob, ScriptedClient, _Finished]:
    client = ScriptedClient({CODER: script})
    finished = _Finished()
    job = library.start_job(
        "Add two numbers", connection=Connection(provider="anthropic", client=client), model=MODEL, on_finished=finished
    )
    return job, client, finished


def _until(condition: Callable[[], bool], seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.02)


def _assert_stopped(job: ToolJob) -> None:
    assert job.status == "failed" and job.message_code == CODE and job.message == "The job was stopped"
    assert job.error == "The job was stopped" and job.finished_at is not None
    assert job.approval is None and job.dependency_plan is None and job.tool_name is None


def test_a_job_waiting_for_its_approval_is_stopped_at_once_and_its_tool_never_registered(
    library: ToolLibrary,
) -> None:
    job, client, finished = _start(library, [_proposal()])
    _until(lambda: job.status == "awaiting_approval")
    began = time.monotonic()
    assert library.cancel_jobs(CODE) == [job.id]
    _assert_stopped(job)  # final at once, not after the hour the approval may wait
    assert finished.event.wait(5) and time.monotonic() - began < 5
    _assert_stopped(job)  # the thread's end changed nothing
    assert finished.calls == ["failed"]
    assert NAME not in library and NAME not in library.registry
    assert [record.status for record in library.registry.versions(NAME)] == ["rejected"]
    assert len(client.messages.calls) == 1
    with pytest.raises(NodestroError) as error:
        library.decide(job.id, True)  # a late approval is refused, never registers the tool
    assert error.value.code == "job.not_awaiting"
    _assert_stopped(job)
    assert library.cancel_jobs(CODE) == []  # idempotent


def test_a_job_inside_the_model_loop_sends_no_further_request_and_ignores_the_late_answer(
    library: ToolLibrary, container: ContainerDouble
) -> None:
    sent, release = threading.Event(), threading.Event()
    invalid = _proposal(code="import socket\n" + TOOL)  # would be fed back for a second attempt

    def slow(request: dict[str, Any]) -> Any:
        sent.set()
        release.wait(20)
        return invalid(request)

    job, client, finished = _start(library, [slow, _proposal()])
    assert sent.wait(10)
    assert library.cancel_jobs(CODE) == [job.id]
    _assert_stopped(job)  # while the request is still open
    release.set()
    assert finished.event.wait(10)
    _assert_stopped(job)
    assert len(client.messages.calls) == 1  # no correction request after the late answer
    assert container.calls == []  # the late proposal was never tested
    assert job.attempts == []  # nor recorded
    assert NAME not in library.registry


def test_a_job_whose_tests_run_has_its_sandbox_call_killed(
    library: ToolLibrary, container: ContainerDouble, tmp_path: Path
) -> None:
    # A stand-in interpreter for the tests that never answers, so only the stop (or the timeout) ends the call.
    pid_file = tmp_path / "tests.pid"
    python = _script(
        tmp_path / "python", f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    )
    container.host = SubprocessSandbox(timeout=30.0, python=str(python), acknowledge_unsafe=True)
    job, client, finished = _start(library, [_proposal()])
    _until(lambda: pid_file.exists() and pid_file.read_text() != "")
    began = time.monotonic()
    assert library.cancel_jobs(CODE) == [job.id]
    _assert_stopped(job)
    assert finished.event.wait(10) and time.monotonic() - began < 10  # not the sandbox's 30 s timeout
    _assert_stopped(job)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)  # the test process is gone
    assert container.calls == [("validate", NAME)]
    assert len(client.messages.calls) == 1  # the killed test is not fed back as a failure
    assert NAME not in library.registry


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class _Container(StreamedSandbox):
    """Nodestro's container sandbox, ready without a real Docker (preflight and self-test are Orkexa's)."""

    def self_test_passed(self) -> bool:
        return True

    def _require_ready(self) -> str:
        return "sha256:" + "ab" * 32


def test_a_job_whose_tests_run_in_a_container_leaves_no_container(tmp_path: Path) -> None:
    # A stand-in docker CLI: ``run`` waits like a container; ``kill``/``rm -f <name>`` end the ``run`` of that name.
    log = tmp_path / "docker.log"
    docker = _script(
        tmp_path / "docker",
        "import json, os, signal, sys, time\n"
        f"log, root = {str(log)!r}, {str(tmp_path)!r}\n"
        "args = sys.argv[1:]\n"
        "open(log, 'a').write(json.dumps(args) + '\\n')\n"
        "if args[0] in ('kill', 'rm'):\n"
        "    path = os.path.join(root, args[-1])\n"
        "    if os.path.exists(path):\n"
        "        os.kill(int(open(path).read()), signal.SIGTERM)\n"
        "        os.unlink(path)\n"
        "    sys.exit(0)\n"
        "if args[0] == 'ps':\n"
        "    sys.exit(0)\n"
        "name = args[args.index('--name') + 1]\n"
        "open(os.path.join(root, name), 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
    )
    library = ToolLibrary(
        tmp_path / "tools", sandbox=_Container(image="nodestro-env:test", docker=str(docker), timeout=30.0)
    )
    job, _client, finished = _start(library, [_proposal()])
    # The stand-in container runs (it wrote its pid).
    _until(lambda: any(path.read_text() for path in tmp_path.glob("orkexa-sandbox-*")))
    assert library.cancel_jobs(CODE) == [job.id]
    assert finished.event.wait(10)
    _assert_stopped(job)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[0][0] == "run" and ["kill", name] in calls and ["rm", "-f", name] in calls
    with pytest.raises(ProcessLookupError):  # the stand-in container ended
        os.kill(int((tmp_path / name).read_text()), 0)


class _Gate(DependencyGate):
    """Asks for one library; counts installations."""

    def __init__(self) -> None:
        self.installs: list[str] = []

    def prepare(self, name: str, code: str, tests: str) -> DependencyOutcome:
        return DependencyOutcome(plan={"id": "plan_1", "requirements": [{"name": "tabulate"}]})

    def install(self, plan_id: str, log: Callable[[str], None]) -> None:
        self.installs.append(plan_id)


def test_a_job_waiting_for_its_libraries_is_stopped_and_installs_nothing(
    library: ToolLibrary, container: ContainerDouble
) -> None:
    gate = _Gate()
    library.dependencies = gate
    job, client, finished = _start(library, [_proposal()])
    _until(lambda: job.status == "awaiting_dependencies")
    assert library.cancel_jobs(CODE) == [job.id]
    _assert_stopped(job)
    assert finished.event.wait(5)
    _assert_stopped(job)
    with pytest.raises(NodestroError) as error:
        library.decide_dependencies(job.id, "approve")
    assert error.value.code == "job.not_awaiting"
    assert gate.installs == [] and container.calls == [] and len(client.messages.calls) == 1


def test_only_unfinished_jobs_of_this_library_are_stopped(tmp_path: Path) -> None:
    mine = ToolLibrary(tmp_path / "mine", sandbox=ContainerDouble())
    other = ToolLibrary(tmp_path / "other", sandbox=ContainerDouble())
    done, _, done_finished = _start(mine, [_proposal()])
    _until(lambda: done.status == "awaiting_approval")
    mine.decide(done.id, True)
    assert done_finished.event.wait(10) and done.status == "approved"
    waiting, _, _ = _start(mine, [_proposal(TOOL.replace(NAME, "add_more"), "add_more")])
    theirs, _, theirs_finished = _start(other, [_proposal()])
    _until(lambda: waiting.status == "awaiting_approval" and theirs.status == "awaiting_approval")
    assert mine.cancel_jobs(CODE) == [waiting.id]
    _assert_stopped(waiting)
    assert done.status == "approved" and done.message_code == "job.approved" and NAME in mine
    assert theirs.status == "awaiting_approval" and theirs.message_code == "job.awaiting_approval"
    assert other.decide(theirs.id, True) is theirs and theirs_finished.event.wait(10)
    assert other.jobs[theirs.id].status == "approved" and NAME in other


def test_a_job_whose_approval_was_taken_registers_its_tool(library: ToolLibrary) -> None:
    # The decision and the stop race: once the approval is taken, the tool is registered and the job not stopped.
    job, _, finished = _start(library, [_proposal()])
    _until(lambda: job.status == "awaiting_approval")
    with library._lock:  # the job's thread takes the approval under this lock, as cancel_jobs decides
        library.decide(job.id, True)
        decision = library._decisions[job.id]
    _until(lambda: decision.committed or finished.event.is_set())
    assert library.cancel_jobs(CODE) == []
    assert finished.event.wait(10) and job.status == "approved" and NAME in library


# ---- R4-7: a stopped job never starts an installation ------------------------------------------------------------


@pytest.fixture
def python_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Workspace, _Images]]:
    """A workspace whose Python environments build container images (a stand-in) with a stand-in ``uv``."""
    monitor = StaticDockerMonitor(docker_status.approved_status())
    monkeypatch.setattr(docker_status, "monitor_for", lambda *a, **k: monitor)
    workspace = Workspace(tmp_path / "data", python_runner=FakeUv(INDEX), integrations=[])
    images = _Images()
    workspace.python.images = images  # type: ignore[assignment]
    sandbox = ContainerDouble()  # the job's tests run in a stand-in container (Docker's self-test is not run here)
    workspace.tools.container_fallback = lambda: sandbox
    try:
        yield workspace, images
    finally:
        workspace.close()


class _StopFirst(EnvironmentGate):
    """The real gate; the stop lands right after the job's own last check, just before the installation starts
    (as the API stops a suspended tenant's work: ``cancel_jobs``, then ``cancel_installs``)."""

    def __init__(self, workspace: Workspace, *, before: str) -> None:
        super().__init__(workspace.python)
        self.workspace, self.before = workspace, before
        self.stopped: list[str] = []

    def _stop(self) -> None:
        self.stopped += self.workspace.tools.cancel_jobs(CODE)
        self.stopped += self.workspace.python.cancel_installs(CODE)

    def prepare(self, name: str, code: str, tests: str) -> DependencyOutcome:
        if self.before == "prepare":
            self._stop()
        return super().prepare(name, code, tests)

    def install(self, plan_id: str, log: Callable[[str], None]) -> None:
        if self.before == "install":
            self._stop()
        super().install(plan_id, log)


def _library_job(workspace: Workspace, gate: EnvironmentGate) -> tuple[ToolJob, _Finished]:
    workspace.tools.dependencies = gate
    job, _client, finished = _start(workspace.tools, [_proposal(LIBRARY_CODE, "show", LIBRARY_TESTS)])
    return job, finished


def test_a_job_stopped_between_the_library_approval_and_the_installation_installs_nothing(
    python_workspace: tuple[Workspace, _Images],
) -> None:
    workspace, images = python_workspace
    gate = _StopFirst(workspace, before="install")
    job, finished = _library_job(workspace, gate)
    _until(lambda: job.status == "awaiting_dependencies")
    plan = job.dependency_plan
    assert plan is not None
    workspace.tools.decide_dependencies(job.id, "approve")
    assert finished.event.wait(10)
    _assert_stopped(job)
    assert gate.stopped == [job.id]  # no installation ran yet when the work was stopped
    assert images.builds == [] and images.checked == []  # the installation never started
    env = workspace.python.get(plan["scope"])
    assert env is None or (env.install.status != "installing" and not env.approvals)
    assert workspace.python.cancel_installs(CODE) == []
    assert "show" not in workspace.tools.registry


def test_a_job_stopped_before_its_approved_environment_is_installed_installs_nothing(
    python_workspace: tuple[Workspace, _Images],
) -> None:
    workspace, images = python_workspace
    python = workspace.python
    # The libraries were approved and installed before; the image is gone, so preparing would install it again.
    needed = python.needed_for_sources([("show", LIBRARY_CODE, LIBRARY_TESTS)])
    plan = python.plan("tool", "show", needed, trust="untrusted")
    assert plan.error is None and plan.lock
    assert python.approve(plan.id, wait=True).install.status == "installed"
    built = list(images.builds)
    images.built.clear()
    python._images_ready.clear()
    assert python.status("tool", "show", needed, trust="untrusted").state == "install_needed"
    gate = _StopFirst(workspace, before="prepare")
    job, finished = _library_job(workspace, gate)
    assert finished.event.wait(10)
    _assert_stopped(job)
    assert gate.stopped == [job.id]
    assert images.builds == built  # no installation started for the stopped job
    env = python.get(plan.scope)
    assert env is not None and env.install.status != "installing"


def test_an_installation_is_refused_while_the_work_that_asks_for_it_is_stopped(
    python_workspace: tuple[Workspace, _Images],
) -> None:
    workspace, images = python_workspace
    python = workspace.python
    needed = python.needed_for_sources([("show", LIBRARY_CODE, LIBRARY_TESTS)])
    plan = python.plan("tool", "show", needed, trust="untrusted")
    token = sandbox_cancel.bind(lambda: True)
    try:
        with pytest.raises(NodestroError) as error:
            python.approve(plan.id, wait=True)
    finally:
        sandbox_cancel.reset(token)
    assert error.value.code == "deps.install_stopped"
    assert python.get(plan.scope) is None and images.builds == []  # nothing stored, nothing started
    assert python.cancel_installs(CODE) == []
    # Work that was not stopped installs as before.
    assert python.approve(plan.id, wait=True).install.status == "installed" and images.builds
