"""Request options cannot add tools or a provider-hosted MCP server (MCP-0 finding F-1).

The end-to-end tests send real SDK requests to a stub HTTP server on the loopback interface; no provider and no MCP
server is contacted.
"""

from __future__ import annotations

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any, ClassVar

import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import Connection, check_request_options, forbidden_request_options
from nodestro_runtime.schemas import DEFAULT_AGENT_MODEL, AgentConfig
from nodestro_runtime.workspace import Workspace

MCP_SERVERS = [{"type": "url", "url": "https://mcp.example.invalid/mcp", "name": "remote", "authorization_token": "t"}]
MCP_TOOLSET = [{"type": "mcp_toolset", "mcp_server_name": "remote"}]
OPENAI_MCP_TOOL = [{"type": "mcp", "server_label": "remote", "server_url": "https://mcp.example.invalid/mcp"}]

FORBIDDEN: list[tuple[str, dict[str, Any], str]] = [
    ("anthropic", {"betas": ["mcp-client-2025-11-20"], "mcp_servers": MCP_SERVERS}, "mcp_servers"),
    ("anthropic", {"betas": ["mcp-client-2025-04-04"], "mcp_servers": MCP_SERVERS}, "betas=mcp-client-2025-04-04"),
    ("anthropic", {"betas": ["mcp-client-2025-11-20"]}, "betas=mcp-client-2025-11-20"),
    (
        "anthropic",
        {"extra_headers": {"anthropic-beta": "mcp-client-2025-11-20"}, "extra_body": {"mcp_servers": MCP_SERVERS}},
        "extra_body.mcp_servers",
    ),
    ("anthropic", {"extra_body": {"tools": MCP_TOOLSET}}, "extra_body.tools"),
    ("anthropic", {"extra_headers": {"Anthropic-Beta": "anything"}}, "extra_headers.Anthropic-Beta"),
    ("openai", {"extra_body": {"tools": OPENAI_MCP_TOOL}}, "extra_body.tools"),
    ("openai", {"extra_body": {"tool_choice": "required"}}, "extra_body.tool_choice"),
    ("openai", {"include": OPENAI_MCP_TOOL}, "include[0].type=mcp"),
    ("gemini", {"http_options": {"extra_body": {"tools": [{"x": 1}]}}}, "http_options.extra_body.tools"),
    ("compatible", {"extra_body": {"messages": []}}, "extra_body.messages"),
    ("compatible", {"extra_body": "not an object"}, "extra_body"),
]

ALLOWED: list[tuple[str, dict[str, Any]]] = [
    ("anthropic", {}),
    ("anthropic", {"output_config": {"task_budget": {"type": "tokens", "total": 20000}}}),
    ("anthropic", {"betas": ["context-management-2025-06-27"], "context_management": {"edits": []}}),
    ("anthropic", {"extra_headers": {"X-Request-Tag": "nightly"}}),
    ("anthropic", {"extra_body": {"temperature": 0.2}}),
    ("openai", {"service_tier": "flex"}),
    ("compatible", {"extra_body": {"top_k": 20, "repetition_penalty": 1.1}}),
    ("gemini", {"candidate_count": 1}),
]


@pytest.mark.parametrize(("provider", "options", "path"), FORBIDDEN)
def test_forbidden_options_are_named(provider: str, options: dict[str, Any], path: str) -> None:
    assert path in forbidden_request_options(options)
    with pytest.raises(NodestroError) as caught:
        check_request_options(options)
    assert caught.value.code == "config.reserved_request_options"
    assert path in caught.value.params["keys"]


@pytest.mark.parametrize(("provider", "options"), ALLOWED)
def test_other_options_stay_allowed(provider: str, options: dict[str, Any]) -> None:
    assert forbidden_request_options(options) == []
    check_request_options(options)
    Connection(provider=provider, api_key="k").options(options)


@pytest.mark.parametrize(("provider", "options", "path"), FORBIDDEN)
def test_connection_options_refuse(provider: str, options: dict[str, Any], path: str) -> None:
    with pytest.raises(NodestroError, match="not allowed"):
        Connection(provider=provider, api_key="k").options(options)


_MODELS = {
    "anthropic": DEFAULT_AGENT_MODEL,
    "openai": "openai:gpt-5",
    "gemini": "gemini:gemini-2.5-pro",
    "compatible": "compatible:local-model",
}


@pytest.mark.parametrize(("provider", "options", "path"), FORBIDDEN)
def test_inspector_reports_the_refusal(tmp_path: Path, provider: str, options: dict[str, Any], path: str) -> None:
    workspace = Workspace(tmp_path)
    config = AgentConfig(model=_MODELS[provider], request_options=options, effort=None)
    issues = workspace.builder.validate(config)
    refused = [issue for issue in issues if issue.code == "config.reserved_request_options"]
    assert refused and path in refused[0].params["keys"]


# ---- end to end against a loopback stub ----------------------------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
        payload = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "stub"}}).encode()
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture
def stub() -> Iterator[str]:
    _Recorder.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _send(workspace: Workspace, provider: str, base_url: str, options: dict[str, Any]) -> None:
    config = AgentConfig(model=_MODELS[provider], request_options=options, effort=None)
    connection = Connection(provider=provider, api_key="sk-test", base_url=base_url)
    built = workspace.builder.build(config, connection=connection)
    with pytest.raises(Exception):  # noqa: B017 - the stub answers every request with HTTP 400
        built.agent.ask("hello")


@pytest.mark.parametrize(
    ("provider", "options"),
    [(provider, options) for provider, options, _path in FORBIDDEN if provider in ("anthropic", "openai")],
)
def test_forbidden_options_never_reach_the_provider(
    tmp_path: Path, stub: str, provider: str, options: dict[str, Any]
) -> None:
    workspace = Workspace(tmp_path)
    base_url = stub + ("/v1" if provider == "openai" else "")
    with pytest.raises(NodestroError) as caught:
        _send(workspace, provider, base_url, options)
    assert caught.value.code == "config.reserved_request_options"
    assert _Recorder.requests == []


def test_allowed_options_are_still_sent(tmp_path: Path, stub: str) -> None:
    workspace = Workspace(tmp_path)
    options = {"betas": ["context-management-2025-06-27"], "context_management": {"edits": []}}
    _send(workspace, "anthropic", stub, options)
    [request] = _Recorder.requests
    assert request["body"]["context_management"] == {"edits": []}
    assert "mcp_servers" not in request["body"]
    assert "context-management-2025-06-27" in request["headers"].get("anthropic-beta", "")


def test_openai_extra_body_parameters_are_still_sent(tmp_path: Path, stub: str) -> None:
    workspace = Workspace(tmp_path)
    _send(workspace, "openai", stub + "/v1", {"extra_body": {"metadata": {"job": "nightly"}}})
    [request] = _Recorder.requests
    assert request["path"].endswith("/responses")
    assert request["body"]["metadata"] == {"job": "nightly"}
    assert all(tool.get("type") != "mcp" for tool in request["body"].get("tools", []))
