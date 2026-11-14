"""The skill library (NAL-B3): versions, assignment to agents, loading at run time and freezing in releases."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from orkexa.skills import parse_skill
import postgres_support
import pytest

from nodestro_runtime.agent_test import AgentTestIn
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.release import ReleaseBundle, freeze_skill, skill_bindings
from nodestro_runtime.schemas import AgentConfig, RunIn, SkillBinding, Workflow, WorkflowIn
from nodestro_runtime.skill_tools import LOAD_TOOL, READ_TOOL, SKILLS_NOTICE
from nodestro_runtime.skills import Skills, check
from nodestro_runtime.storage import SQLiteStore
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ENABLE_ENV
from nodestro_runtime.workspace import Workspace


def skill_md(
    name: str = "pdf-review", body: str = "Check page numbers first.", description: str = "Reviews PDFs."
) -> bytes:
    return f"---\nname: {name}\ndescription: {description}\nmetadata:\n  language: en\n---\n{body}\n".encode()


def package(name: str = "pdf-review", body: str = "Check page numbers first.", **files: bytes) -> Any:
    """A package; ``references__a_md`` is the file ``references/a.md``."""
    paths = {".".join(key.replace("__", "/").rsplit("_", 1)): data for key, data in files.items()}
    return parse_skill({"SKILL.md": skill_md(name, body), **paths})


@pytest.fixture(params=["sqlite", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    """The desktop's SQLite store and a tenant's store on PostgreSQL (skipped without a server)."""
    if request.param == "sqlite":
        database = SQLiteStore(tmp_path / "nodestro.db")
        database.migrate(before_migration=lambda current, target: None)
        yield database
        database.close()
        return
    postgres_support.require_postgres()
    pytest.importorskip("psycopg")
    from nodestro_runtime.storage_postgres import PostgresDatabase

    schema = postgres_support.new_schema()
    server = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        server.migrate()
        yield server.store("acme")
    finally:
        server.close()
        postgres_support.drop_schema(schema)


# ---- the library ------------------------------------------------------------------------------------------------


def test_versions_are_immutable_and_share_files(store: Any) -> None:
    skills = Skills(store)
    first = skills.create(package(references__a_md=b"A"), note="first")
    assert (first.version, first.name, first.language, first.origin) == (1, "pdf-review", "en", "user")
    same = skills.save(first.id, package(references__a_md=b"A"))
    assert same.version == 1  # unchanged files: no version
    second = skills.save(first.id, package(body="Check tables too.", references__a_md=b"A"), note="tables")
    assert second.version == 2
    [v1, v2] = skills.versions(first.id)
    assert (v1.note, v2.note) == ("first", "tables") and v1.digest != v2.digest
    assert skills.package(first.id, 1).body.strip() == "Check page numbers first."
    assert skills.package(first.id, 2).file("references/a.md").data == b"A"  # type: ignore[union-attr]
    # One blob for the shared file.
    shared = [ref.sha256 for ref in v1.files if ref.path == "references/a.md"]
    assert shared == [ref.sha256 for ref in v2.files if ref.path == "references/a.md"]


def test_names_are_unique_and_invalid_packages_are_refused(store: Any) -> None:
    skills = Skills(store)
    skills.create(package())
    with pytest.raises(NodestroError) as taken:
        skills.create(package())
    assert taken.value.code == "skill.name_taken"
    with pytest.raises(NodestroError) as invalid:
        skills.create(parse_skill({"SKILL.md": b"---\nname: Bad Name\ndescription: x\n---\n"}))
    assert invalid.value.code == "skill.invalid" and invalid.value.params["reason"] == "skill.name_invalid"


def test_status_and_delete(store: Any) -> None:
    skills = Skills(store)
    kept = skills.create(package("kept", references__a_md=b"A"))
    gone = skills.create(package("gone", references__a_md=b"A", references__b_md=b"B"))
    assert skills.set_status(gone.id, "archived").status == "archived"
    assert skills.delete(gone.id) and not skills.delete(gone.id)
    assert skills.versions(gone.id) == []
    # The file both used stays; the other one is gone.
    assert skills.package(kept.id, 1).file("references/a.md") is not None
    assert [skill.name for skill in skills.all()] == ["kept"]


def test_secrets_in_files_are_a_warning() -> None:
    issues = check(package(references__keys_md=b"use sk-ant-api03-abcdefghijklmnop"))
    [warning] = [issue for issue in issues if issue.code == "skill.secret_suspected"]
    assert warning.severity == "warning" and warning.params == {"path": "references/keys.md", "kinds": "api_key"}


# ---- agents and runs --------------------------------------------------------------------------------------------


def _workspace(tmp_path: Path, script: dict[str, Any]) -> tuple[Workspace, ScriptedClient]:
    client = ScriptedClient(script)
    return Workspace(tmp_path / "data", client_factory=lambda _p: client, integrations=[]), client


def _flow(agent: AgentConfig) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "S",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "a", "type": "agent", "label": "A", "agent": agent.model_dump()},
            ],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


async def test_a_run_loads_a_skill_on_demand_without_asking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_skill`` and ``read_skill_file`` are the run's own tools: with per-call approvals on, they are not
    asked about; every use is recorded on the step and as an event."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    script = {
        "SKILLED": [
            tool_use(LOAD_TOOL, {"name": "pdf-review"}),
            tool_use(READ_TOOL, {"name": "pdf-review", "path": "references/checklist.md"}),
            tool_use(LOAD_TOOL, {"name": "pdf-review"}),
            text("done"),
        ]
    }
    workspace, client = _workspace(tmp_path, script)
    try:
        skill = workspace.skills.create(package(references__checklist_md=b"- page numbers"))
        agent = AgentConfig(name="A", system_prompt="SKILLED", skills=[SkillBinding(skill_id=skill.id, version=1)])
        saved = workspace.save_workflow(_flow(agent))
        run = await workspace.wait((await workspace.run(saved, RunIn(input="x"))).id, timeout=10)
        assert run.status == "succeeded", run.error
        uses = [(use.name, use.version, use.path) for use in run.nodes["a"].skills]
        assert uses == [("pdf-review", 1, None), ("pdf-review", 1, "references/checklist.md")]
        assert not any(call.approval for call in run.nodes["a"].tool_calls)
        system = str(client.messages.calls[0]["system"])
        assert SKILLS_NOTICE in system
        assert "<name>pdf-review</name>" in system and "Check page numbers first." not in system
        results = [
            block["content"]
            for message in client.messages.calls[-1]["messages"]
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        flat = str(results)
        assert '<skill_content name=\\"pdf-review\\">' in flat or 'skill_content name="pdf-review"' in flat
        assert "- page numbers" in flat and "already loaded" in flat
    finally:
        workspace.close()


def test_always_skills_are_in_the_prompt_and_need_no_load_tool(tmp_path: Path) -> None:
    workspace, client = _workspace(tmp_path, {"ALWAYS": [text("ok")]})
    try:
        skill = workspace.skills.create(package())
        agent = AgentConfig(
            name="A", system_prompt="ALWAYS", skills=[SkillBinding(skill_id=skill.id, version=1, mode="always")]
        )
        result, _ = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
        assert result.status == "succeeded", result.error
        request = client.messages.calls[0]
        assert "Check page numbers first." in str(request["system"])
        names = [tool["name"] for tool in request.get("tools", [])]
        assert LOAD_TOOL not in names and READ_TOOL in names
    finally:
        workspace.close()


def test_missing_inactive_and_newer_skills_in_the_inspector(tmp_path: Path) -> None:
    workspace, _client = _workspace(tmp_path, {})
    try:
        skill = workspace.skills.create(package())
        workspace.skills.save(skill.id, package(body="Newer."))
        binding = SkillBinding(skill_id=skill.id, version=1)
        codes = [issue.code for issue in workspace.builder.validate(AgentConfig(skills=[binding]))]
        assert "skill.newer_version" in codes
        missing = SkillBinding(skill_id=skill.id, version=9)
        issues = workspace.builder.validate(AgentConfig(skills=[missing]))
        assert [(i.code, i.field) for i in issues if i.code.startswith("skill.")] == [("skill.not_found", "skills")]
        workspace.skills.set_status(skill.id, "disabled")
        codes = [issue.code for issue in workspace.builder.validate(AgentConfig(skills=[binding]))]
        assert "skill.inactive" in codes
    finally:
        workspace.close()


def test_imported_skills_next_to_writing_tools_are_a_warning(tmp_path: Path) -> None:
    workspace, _client = _workspace(tmp_path, {})
    try:
        skill = workspace.skills.create(package(), origin="import:" + "a" * 64)
        binding = SkillBinding(skill_id=skill.id, version=1)
        reading = workspace.builder.validate(AgentConfig(skills=[binding]))
        assert "skill.imported_with_write_tools" not in [issue.code for issue in reading]
        writing = workspace.builder.validate(AgentConfig(skills=[binding], server_tools=["code_execution"]))
        assert "skill.imported_with_write_tools" in [issue.code for issue in writing]
    finally:
        workspace.close()


def test_large_always_skills_are_a_warning(tmp_path: Path) -> None:
    workspace, _client = _workspace(tmp_path, {})
    try:
        skill = workspace.skills.create(package(body="word " * 20_000))
        agent = AgentConfig(skills=[SkillBinding(skill_id=skill.id, version=1, mode="always")])
        assert "skill.always_large" in [issue.code for issue in workspace.builder.validate(agent)]
    finally:
        workspace.close()


# ---- releases ---------------------------------------------------------------------------------------------------


def test_a_release_freezes_its_skills() -> None:
    first = package()
    workflow = Workflow.model_validate(
        {
            **_flow(AgentConfig(skills=[SkillBinding(skill_id="skl_1", version=1)])).model_dump(),
            "id": "wf_1",
        }
    )
    assert skill_bindings([workflow]) == [("skl_1", 1)]
    bundle = ReleaseBundle(workflow_id="wf_1", workflow=workflow, skills=[freeze_skill("skl_1", 1, "user", first)])
    again = ReleaseBundle.model_validate_json(bundle.model_dump_json())
    assert again.digest() == bundle.digest()
    skill, frozen = again.skill_source()("skl_1", 1)
    assert skill.name == "pdf-review" and frozen.files == first.files
    with pytest.raises(KeyError):
        again.skill_source()("skl_1", 2)


def test_bundles_without_skills_keep_their_digest() -> None:
    import hashlib
    import json

    workflow = Workflow.model_validate({**_flow(AgentConfig()).model_dump(), "id": "wf_1"})
    bundle = ReleaseBundle(workflow_id="wf_1", workflow=workflow)
    data = bundle.model_dump(
        mode="json",
        exclude={"created_at", "tool_trust", "mcp_tools", "builtin_versions", "required_connections", "skills"},
    )
    for node in data["workflow"]["nodes"]:
        if node.get("agent"):
            for key in ("tool_settings", "skills", "task", "origin"):
                del node["agent"][key]
    expected = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    assert bundle.digest() == expected
