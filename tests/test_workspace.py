"""The runtime used directly from Python: no HTTP server, no interface, scripted model answers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import itertools
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any

from orkexa import Agent
import pytest

from nodestro_runtime.preflight import preflight
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import MissingApiKeyError
from nodestro_runtime.schemas import RunIn, SettingsIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.workspace import Workspace

Make = Callable[..., Workspace]


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Make]:
    opened: list[Workspace] = []

    def factory(script: dict[str, list[Any]] | None = None, *, client: Any = None, **options: Any) -> Workspace:
        fake = client or ScriptedClient(script or {})
        workspace = Workspace(
            tmp_path / f"data{len(opened)}", client_factory=lambda _provider: fake, integrations=[], **options
        )
        workspace.fake = fake  # type: ignore[attr-defined]
        opened.append(workspace)
        return workspace

    yield factory
    for workspace in opened:
        workspace.close()


def agent(node_id: str, prompt: str, **config: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "agent",
        "label": node_id.title(),
        "agent": {"name": node_id.title(), "system_prompt": prompt, **config},
    }


def edge(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    return {"id": f"{source}-{target}", "source": source, "target": target, "source_handle": handle}


def workflow(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], **extra: Any) -> WorkflowIn:
    return WorkflowIn.model_validate({"name": "Test", "nodes": nodes, "edges": edges, **extra})


async def run(workspace: Workspace, flow: WorkflowIn, value: str = "Eingabe") -> Any:
    saved = workspace.save_workflow(flow)
    started = await workspace.run(saved, RunIn(input=value))
    return await workspace.wait(started.id, timeout=20)


async def test_a_linear_workflow_runs_without_server(make: Make) -> None:
    workspace = make({"RESEARCH": [text("Fakten A")], "WRITER": [text("Artikel B")]})
    flow = workflow(
        [
            {"id": "start", "type": "start"},
            agent("research", "RESEARCH"),
            agent("writer", "WRITER", task_template="Schreibe aus:\n{{Research}}"),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "research"), edge("research", "writer"), edge("writer", "out")],
    )
    done = await run(workspace, flow, "Thema X")
    assert done.status == "succeeded", done.error
    assert done.output == "Artikel B"
    assert done.nodes["writer"].prompt == "Schreibe aus:\nFakten A"
    assert done.usage.requests == 2 and (done.usage.estimated_cost_usd or 0) > 0
    # The run is stored where the studio finds it.
    assert workspace.runs.get(done.id) is not None


async def test_parallel_branches_run_and_merge(make: Make) -> None:
    started: list[str] = []
    both = threading.Barrier(2, timeout=5)

    def parallel(answer: str) -> Callable[[dict[str, Any]], Any]:
        def respond(request: dict[str, Any]) -> Any:
            started.append(answer)
            both.wait()  # only returns when the other branch runs at the same time
            return text(answer)

        return respond

    workspace = make({"LEFT": [parallel("links")], "RIGHT": [parallel("rechts")], "MERGE": [text("beides")]})
    flow = workflow(
        [
            {"id": "start", "type": "start"},
            agent("left", "LEFT"),
            agent("right", "RIGHT"),
            agent("merge", "MERGE"),
        ],
        [edge("start", "left"), edge("start", "right"), edge("left", "merge"), edge("right", "merge")],
    )
    done = await run(workspace, flow)
    assert done.status == "succeeded", done.error
    assert sorted(started) == ["links", "rechts"]
    assert "links" in done.nodes["merge"].prompt and "rechts" in done.nodes["merge"].prompt


async def test_the_pre_check_refuses_an_invalid_workflow(make: Make) -> None:
    workspace = make()
    saved = workspace.save_workflow(workflow([agent("a", "A"), agent("b", "B")], [edge("a", "b"), edge("b", "a")]))
    codes = {problem.code for problem in preflight(workspace, saved).problems}
    assert "workflow.cycle" in codes
    with pytest.raises(NodestroError):
        await workspace.run(saved, RunIn(input="x"))
    assert workspace.runs.live_runs() == []


async def test_a_failed_step_blocks_its_successors(make: Make) -> None:
    workspace = make({"BROKEN": [text("x", stop_reason="max_tokens")], "OTHER": [text("y")]})
    flow = workflow(
        [agent("broken", "BROKEN"), agent("after", "AFTER"), agent("other", "OTHER")], [edge("broken", "after")]
    )
    done = await run(workspace, flow)
    assert done.status == "failed"
    assert done.nodes["after"].status == "blocked"
    assert done.nodes["other"].status == "succeeded"


async def test_budget_limits_stop_the_run(make: Make) -> None:
    huge = {"input_tokens": 1_000_000, "output_tokens": 0}
    workspace = make({"EXPENSIVE": [text("a", usage=huge)], "NEXT": [text("b")]})
    flow = workflow(
        [agent("expensive", "EXPENSIVE"), agent("next", "NEXT")], [edge("expensive", "next")], budget_usd=0.5
    )
    done = await run(workspace, flow)
    assert done.status == "failed"
    assert done.nodes["expensive"].error_code == "run.budget_exceeded"


HUGE = {"input_tokens": 1_000_000, "output_tokens": 0}


def subflow(node_id: str, workflow_id: str) -> dict[str, Any]:
    return {"id": node_id, "type": "subworkflow", "label": node_id.title(), "subworkflow": {"workflow_id": workflow_id}}


async def test_the_budget_of_a_sub_workflow_is_enforced(make: Make) -> None:
    workspace = make({"EXPENSIVE": [text("a", usage=HUGE)], "NEXT": [text("b")]})
    child = workspace.save_workflow(
        workflow(
            [{"id": "s", "type": "start"}, agent("expensive", "EXPENSIVE"), agent("next", "NEXT")],
            [edge("s", "expensive"), edge("expensive", "next")],
            budget_usd=0.5,
        )
    )
    parent = workflow(
        [{"id": "start", "type": "start"}, subflow("child", child.id)], [edge("start", "child")], budget_usd=1000.0
    )
    done = await run(workspace, parent)
    assert done.status == "failed"
    # The child stopped at its own limit, although the parent still had money left.
    inner = workspace.runs.get(done.nodes["child"].child_run_id)
    assert inner.nodes["expensive"].error_code == "run.budget_exceeded"
    assert inner.nodes["next"].status != "succeeded"


async def test_the_parent_budget_still_covers_the_sub_workflow(make: Make) -> None:
    workspace = make({"EXPENSIVE": [text("a", usage=HUGE)], "NEXT": [text("b")]})
    child = workspace.save_workflow(
        workflow(
            [{"id": "s", "type": "start"}, agent("expensive", "EXPENSIVE"), agent("next", "NEXT")],
            [edge("s", "expensive"), edge("expensive", "next")],
        )
    )
    parent = workflow(
        [{"id": "start", "type": "start"}, subflow("child", child.id)], [edge("start", "child")], budget_usd=0.5
    )
    done = await run(workspace, parent)
    assert done.status == "failed"
    inner = workspace.runs.get(done.nodes["child"].child_run_id)
    assert inner.nodes["expensive"].error_code == "run.budget_exceeded"


async def test_a_cheap_sub_workflow_within_its_budget_succeeds(make: Make) -> None:
    workspace = make({"CHEAP": [text("klein")]})
    child = workspace.save_workflow(
        workflow([{"id": "s", "type": "start"}, agent("cheap", "CHEAP")], [edge("s", "cheap")], budget_usd=0.5)
    )
    parent = workflow(
        [{"id": "start", "type": "start"}, subflow("child", child.id), {"id": "out", "type": "output"}],
        [edge("start", "child"), edge("child", "out")],
    )
    done = await run(workspace, parent)
    assert done.status == "succeeded", done.error


async def test_an_approval_waits_for_a_person(make: Make) -> None:
    workspace = make({"DRAFT": [text("Angebot 1000 EUR")], "SEND": [text("Final")]})
    flow = workflow(
        [
            {"id": "start", "type": "start"},
            agent("draft", "DRAFT"),
            {"id": "check", "type": "approval", "label": "Prüfen", "approval": {"instructions": "Preis?"}},
            agent("send", "SEND"),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "draft"), edge("draft", "check"), edge("check", "send", "approved"), edge("send", "out")],
    )
    paused = await run(workspace, flow)
    assert paused.status == "paused" and paused.nodes["check"].status == "awaiting_approval"
    # A paused run keeps only its event counter: the events continue numbering when it resumes.
    assert paused.id not in workspace.runs._events
    before = workspace.runs._seq[paused.id]
    workspace.decide(paused.id, "check", "approved", "ok")
    history, queue = workspace.runs.subscribe(paused.id)
    assert all(event.seq > before for event in history)
    done = await workspace.wait(paused.id, timeout=20)
    assert done.status == "succeeded" and done.output == "Final"
    workspace.runs.unsubscribe(paused.id, queue)


async def test_finished_runs_release_their_event_history(make: Make) -> None:
    workspace = make({"ONE": [text("eins")]})
    done = await run(
        workspace, workflow([{"id": "start", "type": "start"}, agent("one", "ONE")], [edge("start", "one")])
    )
    assert done.status == "succeeded"
    # A long-running server starts many runs: nothing of a finished run stays in memory.
    assert done.id not in workspace.runs._events and done.id not in workspace.runs._subscribers


async def test_a_running_run_can_be_cancelled(make: Make) -> None:
    release = threading.Event()

    def slow(_request: dict[str, Any]) -> Any:
        release.wait(5)
        return text("spät")

    workspace = make({"SLOW": [slow]})
    saved = workspace.save_workflow(workflow([agent("slow", "SLOW")], []))
    started = await workspace.run(saved, RunIn(input="x"))
    for _ in range(100):
        if workspace.runs.is_live(started.id):
            break
        await asyncio.sleep(0.02)
    assert workspace.cancel(started.id)
    release.set()
    done = await workspace.wait(started.id, timeout=20)
    assert done.status == "cancelled"


async def test_a_failed_run_continues_with_its_results(make: Make) -> None:
    script = {name: [text(f"{name} ok")] for name in ("ONE", "TWO")}
    script["THREE"] = [text("x", stop_reason="max_tokens"), text("THREE ok")]
    workspace = make(script)
    names = ["one", "two", "three"]
    nodes = [{"id": "start", "type": "start"}, *(agent(name, name.upper()) for name in names)]
    flow = workflow(nodes, [edge(a, b) for a, b in itertools.pairwise(["start", *names])])
    failed = await run(workspace, flow)
    assert failed.status == "failed"
    continued = await workspace.resume(failed.id)
    done = await workspace.wait(continued.id, timeout=20)
    assert done.status == "succeeded", done.error
    assert done.resumed_from == failed.id and done.nodes["two"].reused_from == failed.id
    assert done.usage.requests == 1


async def test_persistent_memory_survives_runs(make: Make) -> None:
    create_call = tool_use("memory", {"command": "create", "path": "/memories/p.md", "file_text": "Peter"})
    workspace = make({"MEM": [create_call, text("ok")]})
    flow = workflow([agent("mem", "MEM", memory={"mode": "persistent", "space": "Team"})], [])
    assert (await run(workspace, flow)).status == "succeeded"
    assert workspace.memory.open("Team").read("/memories/p.md") == "Peter"


def test_knowledge_files_are_indexed_and_found(make: Make) -> None:
    workspace = make()
    entry = workspace.knowledge.add(b"# Vertrag\n\nDie Haftung ist auf 10.000 EUR begrenzt.", name="vertrag.md")
    hits = workspace.knowledge.search("Haftung")
    assert hits and entry.id in {hit.source for hit in hits}


def test_providers_must_be_configured_before_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        with pytest.raises(MissingApiKeyError):
            workspace.connection("openai")
        workspace.update_settings(SettingsIn.model_validate({"providers": {"openai": {"api_key": "sk-test"}}}))
        connection = workspace.connection("openai")
        assert connection.api_key == "sk-test"
        # Keys are never part of what is shown.
        assert "sk-test" not in workspace.settings().model_dump_json()
        with pytest.raises(NodestroError):
            workspace.connection("unknown-provider")
    finally:
        workspace.close()


def test_agents_are_orkexa_agents(make: Make) -> None:
    workspace = make()
    flow = workflow([agent("a", "A")], [])
    node = flow.nodes[0]
    assert node.agent is not None
    built = workspace.builder.build(node.agent, connection=workspace.connection("anthropic"))
    assert isinstance(built.agent, Agent)


def test_the_runtime_needs_no_http_server_or_interface() -> None:
    code = (
        "import sys, nodestro_runtime.workspace, nodestro_runtime.testing\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "('fastapi', 'starlette', 'uvicorn', 'nodestro_api', 'nodestro_studio', 'nodestro_examples')]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
