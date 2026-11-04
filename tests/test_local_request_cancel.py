"""Cancel and the time limit end a run whose model request waits for a local model server (RT3-05).

The server here accepts the connection and never answers, like an Ollama that hangs or queues the request: without
closing the request, the run kept its thread until the client's timeout (an hour for local servers)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import socket
import threading
import time
from typing import Any

import pytest

from nodestro_runtime.schemas import Run, RunIn, WorkflowIn
from nodestro_runtime.workspace import Workspace


@pytest.fixture
def silent_server() -> Iterator[int]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                return
            held.append(connection)  # read nothing, answer nothing

    threading.Thread(target=accept, daemon=True).start()
    yield server.getsockname()[1]
    stop.set()
    server.close()
    for connection in held:
        connection.close()


@pytest.fixture
def workspace(tmp_path: Path, silent_server: int, monkeypatch: pytest.MonkeyPatch) -> Iterator[Workspace]:
    from orkexa.local import _http

    def no_answer(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise _http.OllamaError("unreachable", "no answer")  # model metadata: unknown, quickly

    monkeypatch.setattr(_http.OllamaHTTP, "request", no_answer)
    opened = Workspace(tmp_path / "data", integrations=[])
    base_url = f"http://127.0.0.1:{silent_server}/v1"
    opened.store.put_raw("settings", "app", {"providers": {"ollama": {"base_url": base_url}}})
    yield opened
    opened.close()


def _flow(**limits: Any) -> WorkflowIn:
    agent = {"name": "A", "system_prompt": "x", "model": "ollama:qwen3.5:4b"}
    return WorkflowIn.model_validate(
        {
            "name": "Local",
            "limits": limits,
            "nodes": [{"id": "start", "type": "start"}, {"id": "a", "type": "agent", "agent": agent}],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


async def _until_stopped(workspace: Workspace, run_id: str, seconds: float) -> Run:
    import asyncio

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        run = workspace.runs.get(run_id)
        assert run is not None
        if run.status not in ("queued", "running"):
            return run
        await asyncio.sleep(0.1)
    raise AssertionError(f"the run still runs after {seconds} s")


async def test_cancel_ends_a_run_waiting_for_a_silent_local_server(workspace: Workspace) -> None:
    import asyncio

    run = await workspace.run(workspace.save_workflow(_flow()), RunIn(input="hi"))
    await asyncio.sleep(1.0)  # the request is sent and waits
    assert workspace.cancel(run.id)
    stopped = await _until_stopped(workspace, run.id, 10)
    assert stopped.status == "cancelled"
    assert stopped.nodes["a"].status == "cancelled"


async def test_the_time_limit_ends_a_run_waiting_for_a_silent_local_server(workspace: Workspace) -> None:
    run = await workspace.run(workspace.save_workflow(_flow(max_duration_s=1.5)), RunIn(input="hi"))
    stopped = await _until_stopped(workspace, run.id, 12)
    assert stopped.status == "failed"
    assert stopped.error_code == "run.time_limit"
