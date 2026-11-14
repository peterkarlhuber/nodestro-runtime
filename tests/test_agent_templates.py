"""Versions of agent templates (NAL-B1): immutable, numbered, and lossless for templates from before versions."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import postgres_support
from pydantic import ValidationError
import pytest

from nodestro_runtime.agent_templates import AgentTemplates
from nodestro_runtime.schemas import (
    AgentConfig,
    AgentTemplate,
    AgentTemplateIn,
    TemplateMeta,
    TemplateRef,
    Workflow,
)
from nodestro_runtime.storage import SQLiteStore


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


def test_create_stores_version_one(store: Any) -> None:
    templates = AgentTemplates(store)
    created = templates.create(AgentConfig(name="Writer"), TemplateMeta(category="text"), note="first")
    assert created.version == 1 and created.meta.origin == "user"
    [only] = templates.versions(created.id)
    assert (only.version, only.agent.name, only.note, only.meta.category) == (1, "Writer", "first", "text")


def test_each_change_is_a_new_version_and_old_ones_stay(store: Any) -> None:
    templates = AgentTemplates(store)
    created = templates.create(AgentConfig(name="Writer", system_prompt="Be brief."))
    second = templates.save(created.id, AgentConfig(name="Writer", system_prompt="Be very brief."), note="shorter")
    third = templates.save(created.id, AgentConfig(name="Writer 2", system_prompt="Be very brief."))
    assert (second.version, third.version) == (2, 3)
    assert third.created_at == created.created_at and third.updated_at >= second.updated_at
    assert [item.agent.system_prompt for item in templates.versions(created.id)] == [
        "Be brief.",
        "Be very brief.",
        "Be very brief.",
    ]
    assert templates.version(created.id, 2).note == "shorter"
    assert templates.version(created.id, 1).agent.name == "Writer"  # unchanged by later saves
    with pytest.raises(KeyError):
        templates.version(created.id, 4)


def test_an_unchanged_save_makes_no_version(store: Any) -> None:
    templates = AgentTemplates(store)
    created = templates.create(AgentConfig(name="Writer"))
    again = templates.save(created.id, AgentConfig(name="Writer"), note="nothing changed")
    assert again.version == 1 and len(templates.versions(created.id)) == 1


def test_meta_changes_make_a_version_and_none_keeps_the_meta(store: Any) -> None:
    templates = AgentTemplates(store)
    created = templates.create(AgentConfig(name="Writer"), TemplateMeta(summary="Writes"))
    changed = templates.save(created.id, AgentConfig(name="Writer"), TemplateMeta(summary="Writes well"))
    kept = templates.save(created.id, AgentConfig(name="Writer!"))
    assert (changed.version, kept.version) == (2, 3)
    assert kept.meta.summary == "Writes well"


def test_a_template_from_before_versions_keeps_its_state_as_v1(store: Any) -> None:
    """A stored template of an older version (no ``meta``, no ``version``, no stored versions) loses nothing."""
    store.put_raw(
        "templates",
        "tpl_old",
        {
            "id": "tpl_old",
            "agent": {"name": "Old", "system_prompt": "Old prompt"},
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    templates = AgentTemplates(store)
    old = templates.get("tpl_old")
    assert old is not None and old.version == 1 and old.meta == TemplateMeta()
    [virtual] = templates.versions("tpl_old")
    assert virtual.version == 1 and virtual.agent.system_prompt == "Old prompt"
    changed = templates.save("tpl_old", AgentConfig(name="Old", system_prompt="New prompt"))
    assert changed.version == 2
    assert [item.agent.system_prompt for item in templates.versions("tpl_old")] == ["Old prompt", "New prompt"]
    # Reading twice or saving the same again changes nothing (idempotent).
    templates.save("tpl_old", AgentConfig(name="Old", system_prompt="New prompt"))
    assert len(templates.versions("tpl_old")) == 2


def test_unknown_templates(store: Any) -> None:
    templates = AgentTemplates(store)
    assert templates.versions("tpl_none") == []
    with pytest.raises(KeyError):
        templates.save("tpl_none", AgentConfig())
    assert templates.delete("tpl_none") is False


def test_delete_removes_the_versions(store: Any) -> None:
    templates = AgentTemplates(store)
    created = templates.create(AgentConfig(name="Writer"))
    templates.save(created.id, AgentConfig(name="Writer 2"))
    assert templates.delete(created.id) is True
    assert templates.get(created.id) is None and templates.versions(created.id) == []


def test_old_clients_and_documents_still_validate() -> None:
    assert AgentTemplateIn.model_validate({"agent": {"name": "A"}}).meta == TemplateMeta()
    assert AgentTemplate.model_validate({"id": "tpl_x", "agent": {}}).version == 1
    agent = AgentConfig.model_validate({"name": "A"})
    assert agent.origin is None and agent.task is None


def test_origin_and_meta_are_checked() -> None:
    with pytest.raises(ValidationError):
        TemplateMeta(origin="somewhere")
    with pytest.raises(ValidationError):
        TemplateMeta(origin="import:abc")
    TemplateMeta(origin="builtin:nodestro-examples@0.3.0/web-research")
    TemplateMeta(origin="import:" + "a" * 64)
    with pytest.raises(ValidationError):
        TemplateMeta.model_validate({"requirements": {"capabilities": ["telepathy"]}})
    with pytest.raises(ValidationError):
        TemplateRef(template_id="tpl_x", version=0)


def test_a_workflow_agent_keeps_its_template_origin() -> None:
    workflow = Workflow.model_validate(
        {
            "name": "W",
            "nodes": [
                {
                    "id": "a",
                    "type": "agent",
                    "agent": {"name": "A", "task": "tool_agent", "origin": {"template_id": "tpl_x", "version": 3}},
                }
            ],
            "edges": [],
        }
    )
    again = Workflow.model_validate_json(workflow.model_dump_json())
    agent = again.nodes[0].agent
    assert agent is not None and agent.origin == TemplateRef(template_id="tpl_x", version=3)
    assert agent.task == "tool_agent"
