"""OAuth 2.1 sign-in for MCP servers (WP-6) against a loopback authorization server and a protected MCP server."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
import json
from pathlib import Path
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp")

from mcp_oauth_support import AuthServer, serve_auth_server, serve_protected
from mcp_support import LoopbackServer, serve

from nodestro_runtime.mcp.hub import McpHub
from nodestro_runtime.mcp.models import EVENTS, SERVERS, McpEvent, McpServer, McpServerIn
from nodestro_runtime.mcp.netguard import EgressPolicy
from nodestro_runtime.mcp.oauth import check_redirect_uri
from nodestro_runtime.mcp.service import McpService
from nodestro_runtime.mcp.vault import TokenVault
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.server_settings import SecretBox
from nodestro_runtime.storage import SQLiteStore

REDIRECT = "http://127.0.0.1:8765/api/mcp/oauth/callback"


@pytest.fixture
def auth() -> Iterator[AuthServer]:
    server, runner = serve_auth_server()
    yield server
    runner.should_exit = True


@pytest.fixture
def protected(auth: AuthServer) -> Iterator[LoopbackServer]:
    handle = serve_protected(auth)
    yield handle
    handle.stop()


def make_service(tmp_path: Path, *, vault: bool) -> tuple[McpService, SQLiteStore]:
    store = SQLiteStore(tmp_path / f"db-{vault}.sqlite")
    store.migrate(before_migration=None)
    hub = McpHub(EgressPolicy())
    box = SecretBox(b"k" * 32) if vault else None
    return McpService(store, hub, TokenVault(box, "local")), store


@pytest.fixture(params=[True, False], ids=["vault", "session"])
def service(request: Any, tmp_path: Path) -> Iterator[McpService]:
    svc, store = make_service(tmp_path, vault=request.param)
    yield svc
    svc.hub.close()
    store.close()


def add(service: McpService, url: str, **extra: Any) -> str:
    data = McpServerIn(name="Docs", url=url, allow_loopback=True, auth="oauth", **extra)
    return service.add(data, actor="admin").id


def code(call: Any) -> str:
    with pytest.raises(NodestroError) as caught:
        call()
    return caught.value.code


def sign_in(service: McpService, auth: AuthServer, server_id: str) -> McpServer:
    url = asyncio.run(service.abegin_oauth(server_id, REDIRECT, actor="admin"))
    callback = auth.authorize(url)
    return asyncio.run(service.acomplete_oauth(callback["state"], callback["code"], callback["iss"]))


def test_sign_in_discovers_registers_and_exchanges_the_code(
    service: McpService, auth: AuthServer, protected: LoopbackServer
) -> None:
    server_id = add(service, protected.url)
    assert code(lambda: asyncio.run(service.adiscover(server_id))) == "mcp.unauthorized"

    url = asyncio.run(service.abegin_oauth(server_id, REDIRECT, actor="admin"))
    query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(f"{auth.issuer}/authorize?")
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) >= 43
    assert query["resource"] == protected.url  # RFC 8707
    assert query["redirect_uri"] == REDIRECT and query["scope"] == "docs.read"
    assert query["client_id"] in auth.clients  # dynamic client registration
    pending = service.server(server_id)
    assert pending.oauth.get("pending_until") and pending.oauth_state_digest
    assert query["state"] not in json.dumps(service.view(pending).model_dump(mode="json"))

    callback = auth.authorize(url)
    stored = asyncio.run(service.acomplete_oauth(callback["state"], callback["code"], callback["iss"]))
    assert stored.token_storage == ("vault" if service.vault.persistent else "session")
    assert stored.oauth["issuer"] == auth.issuer and stored.oauth["refreshable"] is True
    assert stored.oauth_state_digest is None and "pending_until" not in stored.oauth

    _, discovery = asyncio.run(service.adiscover(server_id))
    assert {tool.name for tool in discovery.tools} >= {"search"}
    presented = [r["headers"].get("authorization", "") for r in protected.requests if r["path"] == "/mcp"]
    assert any(header.startswith("Bearer ") for header in presented)
    # The access token never goes to the authorization server, the API view or the audit log.
    access = next(iter(auth.access_tokens))
    assert all(access not in json.dumps(r) for r in auth.requests)
    assert access not in json.dumps(service.view(service.server(server_id)).model_dump(mode="json"))
    events = service.store.list(EVENTS, McpEvent)
    assert access not in json.dumps([event.model_dump(mode="json") for event in events])
    assert {"oauth_started", "oauth_connected"} <= {event.kind for event in events}
    if service.vault.persistent:
        raw = json.dumps(service.store.get(SERVERS, server_id, McpServer).model_dump(mode="json"))
        assert access not in raw  # sealed


def test_an_expired_token_is_refreshed_and_the_rotated_tokens_are_stored(
    service: McpService, auth: AuthServer, protected: LoopbackServer
) -> None:
    auth.expires_in = 1  # within the refresh margin: every call refreshes first
    server_id = add(service, protected.url)
    sign_in(service, auth, server_id)
    before = service.credential(service.server(server_id))
    asyncio.run(service.adiscover(server_id))
    after = service.credential(service.server(server_id))
    assert before is not None and after is not None
    assert after.tokens["refresh_token"] != before.tokens["refresh_token"]  # type: ignore[union-attr]
    assert any("grant_type=refresh_token" in r["body"] for r in auth.requests)
    # After the new token expires too, the rotated refresh token works again (the old one would be refused).
    time.sleep(1.2)
    asyncio.run(service.adiscover(server_id))
    assert sum("grant_type=refresh_token" in r["body"] for r in auth.requests) == 2


def test_a_revoked_grant_asks_for_a_new_sign_in(
    service: McpService, auth: AuthServer, protected: LoopbackServer
) -> None:
    auth.expires_in = 1
    server_id = add(service, protected.url)
    sign_in(service, auth, server_id)
    auth.refuse_refresh = True
    auth.access_tokens.clear()
    assert code(lambda: asyncio.run(service.adiscover(server_id))) == "mcp.oauth_reauthorize"
    assert service.server(server_id).last_error_code == "mcp.oauth_reauthorize"


def test_the_callback_is_checked(service: McpService, auth: AuthServer, protected: LoopbackServer) -> None:
    server_id = add(service, protected.url)
    url = asyncio.run(service.abegin_oauth(server_id, REDIRECT, actor="admin"))
    callback = auth.authorize(url)
    assert code(lambda: asyncio.run(service.acomplete_oauth("forged", callback["code"]))) == "mcp.oauth_state_invalid"
    # RFC 9207: a response from another issuer is refused, and the started sign-in is used up.
    assert (
        code(lambda: asyncio.run(service.acomplete_oauth(callback["state"], callback["code"], "https://evil.example")))
        == "mcp.oauth_failed"
    )
    assert (
        code(lambda: asyncio.run(service.acomplete_oauth(callback["state"], callback["code"], callback["iss"])))
        == "mcp.oauth_state_invalid"
    )
    assert service.credential(service.server(server_id)) is None
    assert "oauth_failed" in {event.kind for event in service.store.list(EVENTS, McpEvent)}


def test_an_authorization_server_without_pkce_s256_is_refused(
    service: McpService, auth: AuthServer, protected: LoopbackServer
) -> None:
    auth.s256 = False
    server_id = add(service, protected.url)
    assert code(lambda: asyncio.run(service.abegin_oauth(server_id, REDIRECT))) == "mcp.oauth_failed"


def test_resource_metadata_for_another_resource_is_refused(
    service: McpService, auth: AuthServer, protected: LoopbackServer
) -> None:
    auth.prm_resource = "http://127.0.0.1:1/other"
    server_id = add(service, protected.url)
    assert code(lambda: asyncio.run(service.abegin_oauth(server_id, REDIRECT))) == "mcp.oauth_failed"


def test_metadata_pointing_at_a_refused_address_is_not_contacted(
    service: McpService, auth: AuthServer, protected: LoopbackServer
) -> None:
    auth.metadata_overrides = {"token_endpoint": "https://169.254.169.254/latest/meta-data"}
    server_id = add(service, protected.url)
    assert code(lambda: asyncio.run(service.abegin_oauth(server_id, REDIRECT))) == "mcp.address_refused"


def test_a_server_that_needs_no_sign_in_is_reported(service: McpService) -> None:
    open_server = serve()
    try:
        server_id = add(service, open_server.url)
        assert code(lambda: asyncio.run(service.abegin_oauth(server_id, REDIRECT))) == "mcp.oauth_not_required"
    finally:
        open_server.stop()


def test_sign_out_and_a_new_address_forget_the_credentials(
    service: McpService, auth: AuthServer, protected: LoopbackServer
) -> None:
    server_id = add(service, protected.url)
    sign_in(service, auth, server_id)
    service.sign_out(server_id, actor="admin")
    stored = service.server(server_id)
    assert stored.token_storage == "none" and stored.oauth == {}
    assert code(lambda: asyncio.run(service.adiscover(server_id))) == "mcp.unauthorized"

    sign_in(service, auth, server_id)
    moved = McpServerIn(name="Docs", url=protected.url + "/", allow_loopback=True, auth="oauth")
    service.update(server_id, moved, actor="admin")
    assert service.credential(service.server(server_id)) is None


def test_inputs_are_checked(service: McpService, protected: LoopbackServer) -> None:
    assert (
        code(lambda: add(service, protected.url, token="pasted"))  # an OAuth server gets no pasted token
        == "mcp.oauth_token_input"
    )
    bearer = service.add(McpServerIn(name="Plain", url=protected.url, allow_loopback=True)).id
    assert code(lambda: asyncio.run(service.abegin_oauth(bearer, REDIRECT))) == "mcp.oauth_not_configured"
    for uri in (
        "http://example.com/api/mcp/oauth/callback",
        "https://example.com/other",
        "https://example.com/api/mcp/oauth/callback?x=1",
        "javascript:alert(1)",
    ):
        with pytest.raises(NodestroError) as caught:
            check_redirect_uri(uri)
        assert caught.value.code == "mcp.oauth_redirect_invalid"
    assert check_redirect_uri("https://nodestro.example.com/api/mcp/oauth/callback")


def test_the_session_mode_keeps_nothing_on_disk(tmp_path: Path, auth: AuthServer, protected: LoopbackServer) -> None:
    service, store = make_service(tmp_path, vault=False)
    try:
        server_id = add(service, protected.url)
        sign_in(service, auth, server_id)
        raw = json.dumps(service.store.get(SERVERS, server_id, McpServer).model_dump(mode="json"))
        for token in auth.access_tokens | auth.refresh_tokens:
            assert token not in raw
        # A new process (another hub) has no session: the person signs in again.
        restarted = McpService(store, McpHub(EgressPolicy()), TokenVault(None, "local"))
        assert restarted.credential(restarted.server(server_id)) is None
        restarted.hub.close()
    finally:
        service.hub.close()
        store.close()


def test_sealed_credentials_survive_a_restart(tmp_path: Path, auth: AuthServer, protected: LoopbackServer) -> None:
    service, store = make_service(tmp_path, vault=True)
    try:
        server_id = add(service, protected.url)
        sign_in(service, auth, server_id)
        restarted = McpService(store, McpHub(EgressPolicy()), TokenVault(SecretBox(b"k" * 32), "local"))
        _, discovery = asyncio.run(restarted.adiscover(server_id))
        assert discovery.tools
        restarted.hub.close()
        # Another tenant's key opens nothing.
        foreign = McpService(store, McpHub(EgressPolicy()), TokenVault(SecretBox(b"k" * 32), "other-tenant"))
        assert foreign.credential(foreign.server(server_id)) is None
        foreign.hub.close()
    finally:
        service.hub.close()
        store.close()


def test_an_agent_call_uses_and_refreshes_the_oauth_token(
    service: McpService, auth: AuthServer, protected: LoopbackServer
) -> None:
    auth.expires_in = 1
    server_id = add(service, protected.url)
    sign_in(service, auth, server_id)
    asyncio.run(service.adiscover(server_id))
    service.decide(server_id, "search", approve=True, access="read", actor="admin")
    time.sleep(1.2)
    _server, _tool, result = service.call("mcp__docs__search", {"query": "invoices"})
    assert "results for invoices" in result.text and not result.is_error
    stored = service.credential(service.server(server_id))
    assert stored is not None and stored.tokens["access_token"] in auth.access_tokens  # type: ignore[union-attr]
