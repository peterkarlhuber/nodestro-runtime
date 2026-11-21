"""Conversations (N-04) on PostgreSQL: rows of the tenant's documents, never visible to another tenant.

Needs ``NODESTRO_TEST_POSTGRES``. The tenant SQL check (``test_tenant_filters``) covers the statements: the module has
no SQL of its own, every read and write goes through the tenant-bound document store.
"""

from __future__ import annotations

from collections.abc import Iterator
import inspect
from pathlib import Path

import postgres_support
import pytest

from nodestro_runtime import conversations
from nodestro_runtime.agent_templates import AgentTemplates
from nodestro_runtime.conversations import (
    COLLECTION,
    AgentChoice,
    ConversationIn,
    ConversationNotFound,
)
from nodestro_runtime.schemas import AgentConfig
from nodestro_runtime.tenant_sql_check import statements_of, tenant_tables
from nodestro_runtime.workspace import Workspace

pytest.importorskip("psycopg")

from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.storage_postgres import PostgresDatabase

MIGRATIONS = Path(conversations.__file__).resolve().parent / "migrations" / "postgres"


@pytest.fixture
def database() -> Iterator[PostgresDatabase]:
    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    yield opened
    opened.close()
    postgres_support.drop_schema(schema)


def test_conversations_are_rows_of_their_tenant_only(database: PostgresDatabase, tmp_path: Path) -> None:
    alpha = Workspace(tmp_path / "alpha", integrations=[], storage=tenant_storage(database, "alpha"))
    beta = Workspace(tmp_path / "beta", integrations=[], storage=tenant_storage(database, "beta"))
    try:
        template_id = AgentTemplates(alpha.store).create(AgentConfig(system_prompt="AGENT")).id
        created = alpha.conversations.create(
            ConversationIn(agent=AgentChoice(template_id=template_id), model="anthropic:claude-sonnet-5-5"),
            owner="usr_alice",
        )
        assert (created.tenant_id, created.owner) == ("alpha", "usr_alice")
        assert [item.id for item in alpha.conversations.summaries("usr_alice")] == [created.id]
        assert alpha.conversations.summaries("usr_bob") == []
        assert beta.conversations.summaries(None) == []
        with pytest.raises(ConversationNotFound):
            beta.conversations.get(created.id, None)
        rows = database._query(
            "SELECT tenant_id, data->>'tenant_id', data->>'owner' FROM nodestro_documents WHERE collection = %s",
            (COLLECTION,),
        )
        assert [tuple(row) for row in rows] == [("alpha", "alpha", "usr_alice")]
    finally:
        alpha.close()
        beta.close()


def test_the_module_has_no_sql_of_its_own() -> None:
    tables = tenant_tables(MIGRATIONS)
    assert "nodestro_documents" in tables
    source = inspect.getsource(conversations)
    assert statements_of(source, "nodestro_runtime/conversations.py", tables) == []


def test_the_answer_reservation_holds_across_processes(database: PostgresDatabase, tmp_path: Path) -> None:
    """M-2 on PostgreSQL: two workspaces of one tenant (two API processes) reserve an answer of one conversation;
    the second is refused under the store lock, and a released reservation frees the conversation again."""
    from nodestro_runtime.problems import NodestroError

    first = Workspace(tmp_path / "one", integrations=[], storage=tenant_storage(database, "alpha"))
    second = Workspace(tmp_path / "two", integrations=[], storage=tenant_storage(database, "alpha"))
    try:
        template_id = AgentTemplates(first.store).create(AgentConfig(system_prompt="AGENT")).id
        created = first.conversations.create(
            ConversationIn(agent=AgentChoice(template_id=template_id), model="anthropic:claude-sonnet-5-5"),
            owner="usr_alice",
        )
        _, user, answer, _, _ = first.conversations._reserve(created.id, "usr_alice", "one", [])
        with pytest.raises(NodestroError) as caught:
            second.conversations._reserve(created.id, "usr_alice", "two", [])
        assert caught.value.code == "conversation.answer_running"
        first.conversations._release(created.id, user, answer)
        assert second.conversations.messages(created.id, "usr_alice") == []
        second.conversations._reserve(created.id, "usr_alice", "two", [])
        assert [item.seq for item in first.conversations.messages(created.id, "usr_alice")] == [3, 4]
    finally:
        first.close()
        second.close()
