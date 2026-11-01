"""Cancelling a run stops its running sandbox call, and only that call (audit B-03)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time

from orkexa.dynamic import SubprocessSandbox
from orkexa.dynamic.errors import SandboxError
import pytest

from nodestro_runtime import sandbox_cancel
from nodestro_runtime.builtin_sandbox import LinuxSandbox
from nodestro_runtime.container_images import StreamedSandbox
from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.workspace import Workspace

TOOL = "celsius_to_fahrenheit"


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _cancel_after(seconds: float) -> tuple[threading.Event, sandbox_cancel.Check]:
    flag = threading.Event()
    threading.Timer(seconds, flag.set).start()
    return flag, flag.is_set


def test_the_built_in_sandbox_kills_the_call_of_a_cancelled_run(tmp_path: Path) -> None:
    # A stand-in interpreter that never answers, so only the cancel (or the timeout) can end the call.
    pid_file = tmp_path / "pid"
    python = _script(tmp_path / "python", f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                     "time.sleep(60)\n")  # fmt: skip
    sandbox = LinuxSandbox(timeout=20.0, python=str(python))
    workdir = tmp_path / "call"
    workdir.mkdir()
    _, check = _cancel_after(0.5)
    token = sandbox_cancel.bind(check)
    began = time.monotonic()
    try:
        with pytest.raises(SandboxError, match="cancelled"):
            sandbox._execute(workdir, "{}")
    finally:
        sandbox_cancel.reset(token)
    assert time.monotonic() - began < 5
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_the_built_in_sandbox_ignores_other_runs(tmp_path: Path) -> None:
    python = _script(tmp_path / "python", "import sys\nsys.stdout.write('{\"ok\": true}')\n")
    workdir = tmp_path / "call"
    workdir.mkdir()
    assert json.loads(LinuxSandbox(timeout=20.0, python=str(python))._execute(workdir, "{}")) == {"ok": True}


def test_a_container_call_of_a_cancelled_run_has_its_container_removed(tmp_path: Path) -> None:
    # A stand-in docker CLI: ``run`` waits like a container; ``rm -f <name>`` ends the ``run`` of that name.
    log = tmp_path / "docker.log"
    docker = _script(
        tmp_path / "docker",
        "import json, os, signal, sys, time\n"
        f"log, root = {str(log)!r}, {str(tmp_path)!r}\n"
        "args = sys.argv[1:]\n"
        "open(log, 'a').write(json.dumps(args) + '\\n')\n"
        "name = args[args.index('--name') + 1] if '--name' in args else args[-1]\n"
        "if args[0] == 'rm':\n"
        "    os.kill(int(open(os.path.join(root, name)).read()), signal.SIGTERM)\n"
        "    sys.exit(0)\n"
        "open(os.path.join(root, name), 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
    )
    sandbox = StreamedSandbox(image="nodestro-env:test", docker=str(docker), timeout=20.0)
    workdir = tmp_path / "call"
    workdir.mkdir()
    _, check = _cancel_after(0.5)
    token = sandbox_cancel.bind(check)
    began = time.monotonic()
    try:
        with pytest.raises(SandboxError, match="cancelled"):
            sandbox._execute(workdir, "{}")
    finally:
        sandbox_cancel.reset(token)
    assert time.monotonic() - began < 5
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0][0] == "run" and calls[1][:2] == ["rm", "-f"]
    assert calls[1][2] == calls[0][calls[0].index("--name") + 1]


class _WaitingSandbox(SubprocessSandbox):
    """Stands in for a sandbox that supports cancel: the call waits until its run is cancelled (at most 8 s)."""

    def __init__(self) -> None:
        super().__init__(timeout=30.0, acknowledge_unsafe=True)
        self.running = threading.Event()
        self.saw_cancel = threading.Event()

    def _execute(self, workdir: Path, request: str) -> str:
        self.running.set()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if sandbox_cancel.requested():
                self.saw_cancel.set()
                raise SandboxError("The run was cancelled; the call was stopped")
            time.sleep(0.02)
        raise SandboxError("Timed out")


async def test_cancelling_a_run_reaches_its_running_sandbox_call(tmp_path: Path) -> None:
    fake = ScriptedClient({"AGENT": [tool_use(TOOL, {"celsius": 100}), text("done")]})
    workspace = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    try:
        result, _ = workspace.tools.save_custom(
            CUSTOM_TOOL_TEMPLATE,
            CUSTOM_TESTS_TEMPLATE,
            TOOL,
            trust=workspace.tools.editor_trust(TOOL, CUSTOM_TOOL_TEMPLATE),
        )
        assert result.ok, result
        sandbox = _WaitingSandbox()
        workspace.runs.tool_sandbox = lambda _workflow_id: sandbox
        flow = WorkflowIn.model_validate(
            {
                "name": "Cancel",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "a", "type": "agent", "agent": {"name": "A", "system_prompt": "AGENT", "tools": [TOOL]}},
                ],
                "edges": [{"id": "e", "source": "start", "target": "a"}],
            }
        )
        started = await workspace.run(workspace.save_workflow(flow), RunIn(input="x"))
        assert await asyncio.to_thread(sandbox.running.wait, 20)
        began = time.monotonic()
        assert workspace.runs.cancel(started.id)
        done = await workspace.wait(started.id, timeout=20)
        assert sandbox.saw_cancel.is_set()
        assert time.monotonic() - began < 5
        assert done.status == "cancelled"
    finally:
        workspace.close()


def test_outside_a_run_nothing_is_cancelled() -> None:
    assert sandbox_cancel.requested() is False
    token = sandbox_cancel.bind(lambda: (_ for _ in ()).throw(RuntimeError("broken")))
    try:
        assert sandbox_cancel.requested() is False  # a broken check never stops a call
    finally:
        sandbox_cancel.reset(token)
