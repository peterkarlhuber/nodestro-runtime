"""Live run chat on a server (PostgreSQL, needs ``NODESTRO_TEST_POSTGRES``): the API process stores instructions,
the worker process that executes the run hands them to the orchestrator; several API processes number messages in
one order; tenants never see each other's conversations; a worker that lost its lease delivers nothing."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import concurrent.futures
from pathlib import Path
from typing import Any

import postgres_support
import pytest

from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, parsed, text
from nodestro_runtime.workspace import Workspace

pytest.importorskip("psycopg")

from nodestro_runtime.jobs import JobQueue, QueuedRuns, RunWorker
from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.run_chat import ChatLimits
from nodestro_runtime.run_views import approvals_of_tree
from nodestro_runtime.storage_postgres import PostgresDatabase

BOSS = "You coordinate teams"


@pytest.fixture
def connect() -> Iterator[Callable[[], PostgresDatabase]]:
    dsn = postgres_support.require_postgres()
    name = postgres_support.new_schema()
    first = PostgresDatabase(dsn, schema=name)
    first.migrate()
    opened = [first]

    def factory() -> PostgresDatabase:
        database = PostgresDatabase(dsn, schema=name)
        opened.append(database)
        return database

    yield factory
    for database in opened:
        database.close()
    postgres_support.drop_schema(name)


class Process:
    def __init__(self, database: PostgresDatabase, root: Path, script: dict[str, list[Any]]) -> None:
        self.database = database
        self.root = root
        self.client = ScriptedClient(script)
        self.opened: dict[str, Workspace] = {}

    def __call__(self, tenant_id: str) -> Workspace:
        if tenant_id not in self.opened:
            self.opened[tenant_id] = Workspace(
                self.root / tenant_id,
                client_factory=lambda _provider: self.client,
                integrations=[],
                storage=tenant_storage(self.database, tenant_id),
            )
        return self.opened[tenant_id]


@pytest.fixture
def processes(tmp_path: Path) -> Iterator[Callable[..., Process]]:
    made: list[Process] = []

    def factory(database: PostgresDatabase, script: dict[str, list[Any]] | None = None) -> Process:
        process = Process(database, tmp_path / "files", script or {})
        made.append(process)
        return process

    yield factory
    for process in made:
        for workspace in process.opened.values():
            workspace.close()


def edge(source: str, target: str) -> dict[str, Any]:
    return {"id": f"{source}-{target}", "source": source, "target": target}


def flows(workspace: Workspace) -> tuple[str, Any]:
    team = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Writer",
                "description": "writes",
                "nodes": [
                    {"id": "s", "type": "start"},
                    {"id": "w", "type": "agent", "label": "Writer", "agent": {"system_prompt": "WRITER"}},
                    {"id": "ok", "type": "approval", "label": "Check", "approval": {"on_reject": "branch"}},
                ],
                "edges": [edge("s", "w"), edge("w", "ok")],
            }
        )
    )
    lead = workspace.save_workflow(
        WorkflowIn.model_validate(
            {
                "name": "Lead",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {
                        "id": "lead",
                        "type": "orchestrator",
                        "label": "Lead",
                        "orchestrator": {"members": [{"workflow_id": team.id}]},
                    },
                ],
                "edges": [edge("start", "lead")],
            }
        )
    )
    return team.id, lead


async def test_an_instruction_stored_by_the_api_reaches_the_orchestrator_in_the_worker(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db, other_api_db = connect(), connect(), connect()
    api = processes(api_db)
    ids: dict[str, str] = {}
    worker = processes(
        worker_db,
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["team"], task="Draft", reason="start")),
                parsed(lambda model: model(action="finish", answer="final with summary", reason="as asked")),
            ],
            "WRITER": [text("draft")],
        },
    )
    workspace = api("alpha")
    QueuedRuns(JobQueue(api_db), "alpha", workspace.runs).attach()
    ids["team"], lead = flows(workspace)
    started = await workspace.run(lead, RunIn(input="x"))
    runner = RunWorker(JobQueue(worker_db), worker, name="w1")
    await runner.run_once()
    paused = workspace.runs.get(started.id)
    assert paused is not None and paused.status == "paused", paused.error if paused else None

    # Two API processes store messages at the same time: one order, nothing lost.
    other = processes(other_api_db)
    limits = ChatLimits(max_instructions=50, max_messages=100)

    def send(number: int) -> str:
        chat = (workspace if number % 2 else other("alpha")).runs.chat
        found, _ = chat.submit(
            run_id=started.id,
            target_run_id=started.id,
            node_id="lead",
            kind="instruction",
            content=f"Add a summary {number}",
            client_id=f"c{number}",
            limits=limits,
        )
        return found.id

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        sent = list(pool.map(send, range(12)))
    stored = workspace.runs.chat.messages(started.id)
    assert len(stored) == 12 and [item.seq for item in stored] == list(range(1, 13))
    assert {item.id for item in stored} == set(sent)
    # Another tenant sees nothing of it.
    assert api("beta").runs.chat.messages(started.id) == []

    (waiting,) = approvals_of_tree(workspace.runs, paused)
    workspace.decide(waiting.run_id, waiting.node_id, "approved", "")
    await runner.run_once()
    done = workspace.runs.get(started.id)
    assert done is not None and done.status == "succeeded", done.error
    prompts = [str(call["messages"]) for call in worker.client.messages.calls if BOSS in str(call.get("system"))]
    assert "Add a summary 11" in prompts[1] and "Guidance" not in prompts[0]
    final = workspace.runs.chat.messages(started.id)
    assert {item.status for item in final} == {"considered"} and {item.step for item in final} == {1}
    assert len(done.nodes["lead"].guidance) == 12


async def test_a_worker_that_lost_its_lease_delivers_nothing(
    connect: Callable[[], PostgresDatabase], processes: Callable[..., Process]
) -> None:
    api_db, worker_db = connect(), connect()
    api = processes(api_db)
    ids: dict[str, str] = {}
    holder: dict[str, Any] = {}

    def lose_the_run(request: dict[str, Any]) -> Any:
        # While the team works, another process takes the run over (its fence changes): the lease is gone.
        runs = holder["worker"]("alpha").runs
        runs.abandon(holder["run_id"], because="test: lease lost")
        return text("draft")

    worker = processes(
        worker_db,
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["team"], task="Draft", reason="start")),
                parsed(lambda model: model(action="finish", answer="never", reason="never")),
            ],
            "WRITER": [lose_the_run],
        },
    )
    holder["worker"] = worker
    workspace = api("alpha")
    QueuedRuns(JobQueue(api_db), "alpha", workspace.runs).attach()
    ids["team"], lead = flows(workspace)
    started = await workspace.run(lead, RunIn(input="x"))
    holder["run_id"] = started.id
    message, _ = workspace.runs.chat.submit(
        run_id=started.id, target_run_id=started.id, node_id="lead", kind="instruction", content="Late wish"
    )
    await RunWorker(JobQueue(worker_db), worker, name="w1").run_once()
    stored = workspace.runs.chat.get(started.id, message.id)
    # It was part of the first decision (delivered, then considered once that decision was recorded) or it stayed
    # open; it is never considered for a decision the lost worker made after losing the run.
    assert stored is not None
    assert stored.status in ("considered", "queued", "delivered")
    if stored.status == "considered":
        assert stored.step == 0
    prompts = [str(call["messages"]) for call in worker.client.messages.calls if BOSS in str(call.get("system"))]
    assert len(prompts) == 1  # the worker that lost the run asked no further decision
