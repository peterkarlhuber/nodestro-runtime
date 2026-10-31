"""A loopback OAuth 2.1 authorization server and an MCP server that requires its tokens (WP-6 tests).

Nothing outside this process is contacted. The authorization server implements what the MCP authorization
specification needs: authorization server metadata (RFC 8414), dynamic client registration (RFC 7591), the
authorization code grant with PKCE S256, the ``resource`` indicator (RFC 8707), the ``iss`` response parameter
(RFC 9207) and rotating refresh tokens. Knobs on :class:`AuthServer` produce the misbehaving servers the tests need.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from mcp_support import LoopbackServer, build_server, free_port


@dataclass
class AuthServer:
    port: int
    issuer: str
    resource: str = ""
    #: Lifetime of access tokens in seconds.
    expires_in: int = 3600
    #: Offer PKCE S256 in the metadata.
    s256: bool = True
    #: Overrides for the metadata (for example a token endpoint at a refused address).
    metadata_overrides: dict[str, Any] = field(default_factory=dict)
    #: The ``resource`` the protected resource metadata names (default: the MCP server's URL).
    prm_resource: str | None = None
    #: Refuse refresh grants (the grant was revoked).
    refuse_refresh: bool = False
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    access_tokens: set[str] = field(default_factory=set)
    refresh_tokens: set[str] = field(default_factory=set)
    requests: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def metadata(self) -> dict[str, Any]:
        base = self.issuer
        data: dict[str, Any] = {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True,
            "scopes_supported": ["docs.read"],
        }
        if self.s256:
            data["code_challenge_methods_supported"] = ["S256"]
        else:
            data["code_challenge_methods_supported"] = ["plain"]
        data.update(self.metadata_overrides)
        return data

    def authorize(self, url: str) -> dict[str, str]:
        """What a browser does: open the authorization URL, sign in (always allowed here), follow the redirect.

        Returns the callback's query parameters (``code``, ``state``, ``iss``)."""
        query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        client = self.clients.get(query.get("client_id", ""))
        assert client is not None, "unknown client"
        assert query["redirect_uri"] in client["redirect_uris"], "redirect_uri not registered"
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(16)
        with self.lock:
            self.codes[code] = {
                "client_id": query["client_id"],
                "redirect_uri": query["redirect_uri"],
                "challenge": query["code_challenge"],
                "resource": query.get("resource"),
                "scope": query.get("scope"),
            }
        return {"code": code, "state": query["state"], "iss": self.issuer}

    def _issue(self, scope: str | None) -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        with self.lock:
            self.access_tokens.add(access)
            self.refresh_tokens.add(refresh)
        answer: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": refresh,
        }
        if scope:
            answer["scope"] = scope
        return answer

    def token(self, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        grant = form.get("grant_type")
        if grant == "authorization_code":
            with self.lock:
                entry = self.codes.pop(form.get("code", ""), None)
            if entry is None:
                return 400, {"error": "invalid_grant"}
            verifier = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            if challenge != entry["challenge"]:
                return 400, {"error": "invalid_grant", "error_description": "PKCE"}
            if form.get("client_id") != entry["client_id"] or form.get("redirect_uri") != entry["redirect_uri"]:
                return 400, {"error": "invalid_grant"}
            if form.get("resource") != self.resource:
                return 400, {"error": "invalid_target"}
            return 200, self._issue(entry["scope"])
        if grant == "refresh_token":
            refresh = form.get("refresh_token", "")
            with self.lock:
                known = refresh in self.refresh_tokens
                self.refresh_tokens.discard(refresh)  # rotation: a refresh token is valid once
            if not known or self.refuse_refresh:
                return 400, {"error": "invalid_grant"}
            if form.get("resource") != self.resource:
                return 400, {"error": "invalid_target"}
            return 200, self._issue(None)
        return 400, {"error": "unsupported_grant_type"}


async def _json(send: Any, status: int, body: Any, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    import json

    data = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), *(headers or [])],
        }
    )
    await send({"type": "http.response.body", "body": data})


async def _body(receive: Any) -> bytes:
    chunks = b""
    while True:
        message = await receive()
        chunks += message.get("body", b"")
        if not message.get("more_body"):
            return chunks


def _run(app: Any, port: int) -> Any:
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    runner = uvicorn.Server(config)
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not runner.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    return runner


def serve_auth_server() -> tuple[AuthServer, Any]:
    """The authorization server on its own port (another origin than the MCP server)."""
    import json

    port = free_port()
    auth = AuthServer(port=port, issuer=f"http://127.0.0.1:{port}")

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        path, method = scope["path"], scope["method"]
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        body = await _body(receive)
        auth.requests.append({"path": path, "method": method, "headers": headers, "body": body.decode()})
        if path == "/.well-known/oauth-authorization-server" and method == "GET":
            await _json(send, 200, auth.metadata())
        elif path == "/register" and method == "POST":
            data = json.loads(body or b"{}")
            client_id = f"client-{secrets.token_hex(4)}"
            record = {**data, "client_id": client_id, "client_id_issued_at": int(time.time())}
            auth.clients[client_id] = record
            await _json(send, 201, record)
        elif path == "/token" and method == "POST":
            form = {key: values[0] for key, values in parse_qs(body.decode()).items()}
            status, answer = auth.token(form)
            await _json(send, status, answer)
        else:
            await _json(send, 404, {"error": "not_found"})

    runner = _run(app, port)
    return auth, runner


def serve_protected(auth: AuthServer, *, require: bool = True) -> LoopbackServer:
    """An MCP server that accepts only access tokens of ``auth`` and publishes its resource metadata (RFC 9728)."""
    server = build_server("nodestro-oauth-test")
    mcp_app = server.streamable_http_app()
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    auth.resource = url
    handle = LoopbackServer(url=url, port=port, mcp=server)
    metadata_url = f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource/mcp"

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            path = scope["path"]
            headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
            handle.requests.append({"path": path, "headers": headers, "method": scope["method"]})
            if path == "/.well-known/oauth-protected-resource/mcp":
                await _json(
                    send,
                    200,
                    {
                        "resource": auth.prm_resource or url,
                        "authorization_servers": [auth.issuer],
                        "scopes_supported": ["docs.read"],
                    },
                )
                return
            if path.startswith("/.well-known/"):
                await _json(send, 404, {"error": "not_found"})
                return
            presented = headers.get("authorization", "")
            token = presented[7:] if presented.startswith("Bearer ") else ""
            if require and token not in auth.access_tokens:
                challenge = f'Bearer resource_metadata="{metadata_url}", scope="docs.read"'
                await _json(send, 401, {"error": "invalid_token"}, [(b"www-authenticate", challenge.encode())])
                return
        await mcp_app(scope, receive, send)

    runner = _run(app, port)

    def stop() -> None:
        runner.should_exit = True

    handle.stop = stop
    return handle


def callback_query(params: dict[str, str]) -> str:
    return urlencode(params)
