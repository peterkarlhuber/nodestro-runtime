"""A run start never waits for a local model server on the event loop, and asks a silent one once (RT3-04)."""

from __future__ import annotations

from collections import Counter
import contextlib
from pathlib import Path
import threading
from typing import Any

import pytest

from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.workspace import Workspace


async def test_a_silent_ollama_server_is_asked_off_the_event_loop_and_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orkexa.local import _http

    loop_thread = threading.current_thread()
    calls: Counter[tuple[str, bool]] = Counter()

    def silent(self: Any, method: str, path: str, body: Any = None, **_kwargs: Any) -> Any:
        calls[(path, threading.current_thread() is loop_thread)] += 1
        raise _http.OllamaError("timeout", "did not answer in time")  # each costs the client's timeout (30 s)

    monkeypatch.setattr(_http.OllamaHTTP, "request", silent)
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        workspace.store.put_raw("settings", "app", {"providers": {"ollama": {"base_url": "http://10.1.2.3:11434/v1"}}})
        agents = [
            {"id": f"a{i}", "type": "agent", "agent": {"name": f"A{i}", "system_prompt": "x", "model": "ollama:m:1b"}}
            for i in range(3)
        ]
        flow = WorkflowIn.model_validate(
            {
                "name": "Local",
                "nodes": [{"id": "start", "type": "start"}, *agents],
                "edges": [{"id": f"e{i}", "source": "start", "target": f"a{i}"} for i in range(3)],
            }
        )
        saved = workspace.save_workflow(flow)
        calls.clear()
        with contextlib.suppress(Exception):  # whether the start is refused does not matter here
            await workspace.run(saved, RunIn(input="hi"))
        assert not [path for (path, on_loop) in calls if on_loop], calls
        assert sum(count for (path, _), count in calls.items() if path == "/api/show") <= 1, calls


async def test_resuming_asks_a_silent_ollama_server_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RT3-11: ``resume`` built its plan (with the pre-check) on the event loop, like ``launch`` before RT3-04."""
    from orkexa.local import _http

    from nodestro_runtime.schemas import Workflow
    from nodestro_runtime.testing import ScriptedClient, text

    loop_thread = threading.current_thread()
    calls: Counter[tuple[str, bool]] = Counter()

    def silent(self: Any, method: str, path: str, body: Any = None, **_kwargs: Any) -> Any:
        calls[(path, threading.current_thread() is loop_thread)] += 1
        raise _http.OllamaError("timeout", "did not answer in time")

    fake = ScriptedClient({"ACT": [text("x", stop_reason="max_tokens")]})
    with Workspace(tmp_path / "data", client_factory=lambda _p: fake, integrations=[]) as workspace:
        flow = WorkflowIn.model_validate(
            {
                "name": "Local",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "act", "type": "agent", "agent": {"name": "Act", "system_prompt": "ACT"}},
                ],
                "edges": [{"id": "e", "source": "start", "target": "act"}],
            }
        )
        saved = workspace.save_workflow(flow)
        failed = await workspace.wait((await workspace.run(saved, RunIn(input="go"))).id, timeout=30)
        assert failed.status == "failed", failed.status
        # The workflow now uses a local model on a server that accepts connections but does not answer.
        current = workspace.store.get("workflows", saved.id, Workflow)
        assert current is not None
        current.nodes[1].agent.model = "ollama:m:1b"  # type: ignore[union-attr]
        workspace.store.put("workflows", saved.id, current)
        workspace.store.put_raw("settings", "app", {"providers": {"ollama": {"base_url": "http://10.1.2.3:11434/v1"}}})
        monkeypatch.setattr(_http.OllamaHTTP, "request", silent)
        calls.clear()
        resumed = None
        with contextlib.suppress(Exception):  # whether the resume is refused does not matter here
            resumed = await workspace.resume(failed.id, mode="continue", confirm=True)
        if resumed is not None:
            await workspace.wait(resumed.id, timeout=30)
        assert calls, "the resume never asked the local server"
        assert not [path for (path, on_loop) in calls if on_loop], calls
