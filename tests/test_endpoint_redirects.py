"""A model endpoint on a server's allowlist cannot redirect the server's requests elsewhere (backend audit A-05).

The allowlist (``NODESTRO_MODEL_ENDPOINTS``) checks only the configured base URL. The OpenAI SDK follows redirects,
so an allowed endpoint that answered with a redirect made the server send the prompt to any address, also one the
operator did not allow, and the answer went back to the tenant. On a server the client of an endpoint provider now
never follows a redirect. Needs ``NODESTRO_TEST_POSTGRES`` for the server workspace.
"""

from __future__ import annotations

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
from typing import Any

import postgres_support
import pytest

from nodestro_runtime.providers import endpoint_client
from nodestro_runtime.schemas import SettingsIn
from nodestro_runtime.workspace import Workspace

pytest.importorskip("openai")

_ANSWER = {
    "id": "x",
    "object": "chat.completion",
    "created": 0,
    "model": "m",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "answer from INTERNAL"}}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class _Servers:
    def __init__(self) -> None:
        self.hits: list[str] = []
        servers = self

        class Internal(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length", 0)))
                servers.hits.append(self.path)
                body = json.dumps(_ANSWER).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: Any) -> None:
                pass

        self.internal = HTTPServer(("127.0.0.1", 0), Internal)

        class Allowed(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{servers.internal.server_port}/internal{self.path}")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_: Any) -> None:
                pass

        self.allowed = HTTPServer(("127.0.0.1", 0), Allowed)
        for server in (self.allowed, self.internal):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.allowed.server_port}/v1"

    def close(self) -> None:
        for server in (self.allowed, self.internal):
            server.shutdown()
            server.server_close()


@pytest.fixture
def servers() -> Iterator[_Servers]:
    started = _Servers()
    yield started
    started.close()


def _ask(connection: Any) -> Any:
    from orkexa import Agent

    agent = Agent(model="m", max_retries=0, **connection.agent_kwargs())
    return agent.ask("the tenant's prompt")


def test_the_endpoint_client_does_not_follow_a_redirect(servers: _Servers) -> None:
    client = endpoint_client(servers.base_url, None)
    with pytest.raises(Exception) as caught:
        client.with_options(max_retries=0).chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    assert "307" in str(caught.value)
    assert servers.hits == []


def test_a_server_workspace_never_follows_an_endpoints_redirect(servers: _Servers, tmp_path: Path) -> None:
    postgres_support.require_postgres()
    pytest.importorskip("psycopg")
    from nodestro_runtime.postgres_tables import tenant_storage
    from nodestro_runtime.storage_postgres import PostgresDatabase

    schema = postgres_support.new_schema()
    database = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        database.migrate()
        allowed = (f"http://127.0.0.1:{servers.allowed.server_port}",)
        workspace = Workspace(
            tmp_path / "acme", integrations=[], storage=tenant_storage(database, "acme", endpoints=allowed)
        )
        try:
            workspace.update_settings(
                SettingsIn.model_validate({"providers": {"compatible": {"base_url": servers.base_url}}})
            )
            connection = workspace.connection("compatible")
            assert connection.base_url == servers.base_url and connection.client is not None
            with pytest.raises(Exception):  # noqa: B017 - the provider's error for the 307 answer
                _ask(connection)
            assert servers.hits == []  # before the fix: ["/internal/v1/chat/completions"] and the internal answer
        finally:
            workspace.close()
    finally:
        database.close()
        postgres_support.drop_schema(schema)


def test_the_desktop_keeps_the_sdks_own_client(tmp_path: Path, servers: _Servers) -> None:
    workspace = Workspace(tmp_path / "desktop", integrations=[])
    try:
        workspace.update_settings(
            SettingsIn.model_validate({"providers": {"compatible": {"base_url": servers.base_url}}})
        )
        assert workspace.connection("compatible").client is None
    finally:
        workspace.close()
