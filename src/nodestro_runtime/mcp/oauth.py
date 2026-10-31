"""OAuth 2.1 for remote MCP servers (ADR-0007, work package WP-6).

The flow follows the MCP authorization specification and uses the official SDK's building blocks (metadata models,
discovery URLs, issuer checks, token parsing, the refreshing ``OAuthClientProvider``); Nodestro only splits the
authorization code grant into two steps, because the person signs in in a browser and the callback can reach another
process (the server's API runs separately from its workers):

1. :func:`abegin` asks the MCP server without credentials, discovers the protected resource metadata (RFC 9728) and
   the authorization server metadata (RFC 8414, issuer checked), registers a client (dynamic client registration,
   RFC 7591, or a client ID metadata document) and returns the authorization URL with PKCE ``S256``, ``state`` and the
   ``resource`` indicator (RFC 8707). The verifier and the discovered metadata stay with the server record, sealed
   like a token (:class:`PendingAuthorization`).
2. :func:`acomplete` checks ``state`` and the issuer of the response (RFC 9207), exchanges the code at the token
   endpoint and returns :class:`OAuthCredentials`, which the service seals like every other token (decision D-3).

At call time :class:`NodestroOAuth` presents the access token only to the MCP server and refreshes it at the
discovered token endpoint; when a person must sign in again it fails with ``mcp.oauth_reauthorize`` instead of
waiting for a browser. Every request of the flow (metadata, registration, token endpoint) goes through the guarded
HTTP client: the address check of the threat model (T7) applies to authorization servers as well, and on servers the
operator's endpoint list must name them.
"""

from __future__ import annotations

from collections.abc import Coroutine
import hashlib
import json
import secrets
import time
from typing import Any, TypeVar
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from ..problems import NodestroError
from .models import McpServer
from .netguard import EgressPolicy, Resolver, guarded_client

T = TypeVar("T")

#: How long a started sign-in waits for the browser's callback.
PENDING_SECONDS = 600
#: Refresh this long before the access token expires.
_EXPIRY_MARGIN = 30
_CALLBACK_PATH = "/api/mcp/oauth/callback"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class OAuthCredentials(_Model):
    """What a completed sign-in leaves behind; stored only sealed (it holds the tokens and the client secret)."""

    issuer: str
    resource: str
    auth_server_url: str | None = None
    #: The authorization server metadata (RFC 8414) and the protected resource metadata (RFC 9728), as discovered.
    metadata: dict[str, Any]
    resource_metadata: dict[str, Any] | None = None
    #: The registered client (``client_id``, maybe ``client_secret``), bound to ``issuer``.
    client: dict[str, Any]
    redirect_uri: str
    tokens: dict[str, Any]
    #: Absolute expiry of the access token (epoch seconds), ``None`` when the server named none.
    expires_at: float | None = None
    scope: str | None = None
    #: Set when a refresh produced new tokens that the service still has to store.
    changed: bool = Field(default=False, exclude=True)

    def dumps(self) -> str:
        return self.model_dump_json()

    @classmethod
    def loads(cls, text: str) -> OAuthCredentials | None:
        try:
            return cls.model_validate_json(text)
        except ValueError:
            return None

    def summary(self) -> dict[str, Any]:
        """What the API may show: never a token, a secret or the client's registration."""
        return {
            "issuer": self.issuer,
            "scope": self.scope,
            "expires_at": self.expires_at,
            "refreshable": bool(self.tokens.get("refresh_token")),
        }


class PendingAuthorization(_Model):
    """A started sign-in, stored sealed until the callback arrives (it holds the PKCE verifier)."""

    state: str
    verifier: str
    redirect_uri: str
    issuer: str
    resource: str
    auth_server_url: str | None = None
    metadata: dict[str, Any]
    resource_metadata: dict[str, Any] | None = None
    client: dict[str, Any]
    scope: str | None = None
    expires_at: float
    actor: str | None = None

    def dumps(self) -> str:
        return self.model_dump_json()

    @classmethod
    def loads(cls, text: str) -> PendingAuthorization | None:
        try:
            return cls.model_validate_json(text)
        except ValueError:
            return None


def state_digest(state: str) -> str:
    """The lookup key of a pending sign-in; the state itself is compared only after opening the sealed record."""
    return hashlib.sha256(state.encode()).hexdigest()


def check_redirect_uri(uri: str) -> str:
    """Nodestro's own callback: ``https`` or a loopback ``http`` address (OAuth 2.1 for native apps), fixed path."""
    parts = urlsplit(uri)
    loopback = parts.hostname in ("127.0.0.1", "::1", "localhost")
    if parts.scheme not in ("https", "http") or (parts.scheme == "http" and not loopback):
        raise NodestroError("mcp.oauth_redirect_invalid", "The sign-in callback must use https or a loopback address")
    if parts.path != _CALLBACK_PATH or parts.query or parts.fragment or parts.username or parts.password:
        raise NodestroError("mcp.oauth_redirect_invalid", f"The sign-in callback must be {_CALLBACK_PATH}")
    return uri


def _failed(message: str, **params: Any) -> NodestroError:
    return NodestroError("mcp.oauth_failed", message[:300], **params)


async def guarded(coro: Coroutine[Any, Any, T]) -> T:
    """Run a step of the flow; anything but a Nodestro error (a malformed answer, a model error) is a failed sign-in."""
    try:
        return await coro
    except NodestroError:
        raise
    except Exception as exc:
        raise _failed(f"The sign-in failed ({type(exc).__name__})", detail=type(exc).__name__) from None


def _client_for(policy: EgressPolicy, server: McpServer, resolver: Resolver | None, verify: Any) -> Any:
    return guarded_client(
        policy,
        server.url,
        allow_loopback=server.allow_loopback,
        timeout=min(server.timeout_seconds, 30.0),
        resolver=resolver,
        verify=verify,
    )


async def _send(http: Any, request: Any) -> Any:
    """Send one request of the flow; the guarded transport checks its URL like every other MCP request."""
    try:
        response = await http.send(request)
        await response.aread()
        return response
    except NodestroError:
        raise
    except Exception as exc:
        raise _failed(f"{request.url.host} cannot be reached ({type(exc).__name__})", host=request.url.host) from None


def _probe_request(url: str) -> Any:
    import httpx2
    from mcp_types import LATEST_PROTOCOL_VERSION

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "nodestro", "version": "oauth-probe"},
        },
    }
    return httpx2.Request(
        "POST",
        url,
        json=body,
        headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION},
    )


async def abegin(
    policy: EgressPolicy,
    server: McpServer,
    redirect_uri: str,
    *,
    previous: OAuthCredentials | None = None,
    client_metadata_url: str | None = None,
    resolver: Resolver | None = None,
    verify: Any = True,
    actor: str | None = None,
) -> tuple[str, PendingAuthorization]:
    """Discover the authorization server, register Nodestro and return the URL the person opens to sign in."""
    from mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError
    from mcp.client.auth.oauth2 import PKCEParameters
    from mcp.client.auth.utils import (
        build_oauth_authorization_server_metadata_discovery_urls,
        build_protected_resource_metadata_discovery_urls,
        create_client_info_from_metadata_url,
        create_client_registration_request,
        credentials_match_issuer,
        extract_resource_metadata_from_www_auth,
        extract_scope_from_www_auth,
        get_client_metadata_scopes,
        handle_auth_metadata_response,
        handle_protected_resource_response,
        handle_registration_response,
        issuers_match,
        should_use_client_metadata_url,
        validate_metadata_issuer,
    )
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
    from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url

    check_redirect_uri(redirect_uri)

    async with _client_for(policy, server, resolver, verify) as http:
        probe = await _send(http, _probe_request(server.url))
        if probe.status_code != 401:
            raise NodestroError(
                "mcp.oauth_not_required",
                f"{server.name} answered without asking for a sign-in (HTTP {probe.status_code})",
                status=probe.status_code,
            )

        resource = resource_url_from_server_url(server.url)
        prm = None
        for url in build_protected_resource_metadata_discovery_urls(
            extract_resource_metadata_from_www_auth(probe), server.url
        ):
            found = await handle_protected_resource_response(await _send(http, _metadata_request(url)))
            if found is not None:
                prm = found
                break
        auth_server_url: str | None = None
        if prm is not None:
            if prm.resource and not check_resource_allowed(
                requested_resource=resource, configured_resource=str(prm.resource)
            ):
                raise _failed(f"The server's metadata names another resource ({prm.resource})")
            if prm.resource:
                resource = str(prm.resource)
            auth_server_url = str(prm.authorization_servers[0])
        parsed = urlsplit(server.url)
        expected_issuer = auth_server_url or f"{parsed.scheme}://{parsed.netloc}"

        asm = None
        for url in build_oauth_authorization_server_metadata_discovery_urls(auth_server_url, server.url):
            ok, found_asm = await handle_auth_metadata_response(await _send(http, _metadata_request(url)))
            if not ok:
                break
            if found_asm is not None:
                if auth_server_url is None and issuers_match(str(found_asm.issuer), expected_issuer):
                    expected_issuer = str(found_asm.issuer)
                try:
                    validate_metadata_issuer(found_asm, expected_issuer)
                except OAuthFlowError as exc:
                    raise _failed(str(exc)) from None
                asm = found_asm
                break
        if asm is None:
            raise _failed(f"No authorization server metadata found for {expected_issuer}", issuer=expected_issuer)
        if asm.code_challenge_methods_supported is not None and "S256" not in asm.code_challenge_methods_supported:
            # The MCP specification requires PKCE with S256; a server that does not offer it is refused.
            raise _failed("The authorization server does not support PKCE (S256)", issuer=expected_issuer)
        for endpoint in (asm.authorization_endpoint, asm.token_endpoint):
            policy.check_url(str(endpoint), allow_loopback=server.allow_loopback)

        grant_types = ["authorization_code", "refresh_token"]
        scope = get_client_metadata_scopes(extract_scope_from_www_auth(probe), prm, asm, grant_types)
        client_metadata = OAuthClientMetadata.model_validate(
            dict(
                redirect_uris=[redirect_uri],
                client_name="Nodestro",
                grant_types=grant_types,
                response_types=["code"],
                token_endpoint_auth_method="none",
                scope=scope,
            )
        )

        client: OAuthClientInformationFull | None = None
        if previous is not None and previous.redirect_uri == redirect_uri:
            stored = OAuthClientInformationFull.model_validate(previous.client)
            if credentials_match_issuer(stored, expected_issuer, client_metadata_url):
                client = stored
        if client is None and should_use_client_metadata_url(asm, client_metadata_url):
            client = create_client_info_from_metadata_url(
                client_metadata_url or "", redirect_uris=client_metadata.redirect_uris
            )
            client.issuer = expected_issuer
        if client is None:
            if asm.registration_endpoint is None:
                raise NodestroError(
                    "mcp.oauth_registration_unsupported",
                    "The authorization server offers no client registration for Nodestro",
                    issuer=expected_issuer,
                )
            policy.check_url(str(asm.registration_endpoint), allow_loopback=server.allow_loopback)
            request = create_client_registration_request(asm, client_metadata, expected_issuer)
            try:
                client = await handle_registration_response(await _send(http, request))
            except OAuthRegistrationError as exc:
                raise _failed(f"The client registration failed: {exc}", issuer=expected_issuer) from None
            client.issuer = expected_issuer

    pkce = PKCEParameters.generate()
    state = secrets.token_urlsafe(32)
    query = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    }
    if scope:
        query["scope"] = scope
    authorization_endpoint = str(asm.authorization_endpoint)
    separator = "&" if urlsplit(authorization_endpoint).query else "?"
    pending = PendingAuthorization(
        state=state,
        verifier=pkce.code_verifier,
        redirect_uri=redirect_uri,
        issuer=expected_issuer,
        resource=resource,
        auth_server_url=auth_server_url,
        metadata=asm.model_dump(mode="json", exclude_none=True),
        resource_metadata=prm.model_dump(mode="json", exclude_none=True) if prm is not None else None,
        client=client.model_dump(mode="json", exclude_none=True),
        scope=scope,
        expires_at=time.time() + PENDING_SECONDS,
        actor=actor,
    )
    return f"{authorization_endpoint}{separator}{urlencode(query)}", pending


def _metadata_request(url: str) -> Any:
    from mcp.client.auth.utils import create_oauth_metadata_request

    return create_oauth_metadata_request(url)


def _context(server_url: str, client: Any, redirect_uri: str, scope: str | None, storage: Any = None) -> Any:
    from mcp.client.auth.oauth2 import OAuthContext
    from mcp.shared.auth import OAuthClientMetadata

    metadata = OAuthClientMetadata.model_validate(
        dict(
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method=client.token_endpoint_auth_method or "none",
            scope=scope,
        )
    )
    context = OAuthContext(
        server_url=server_url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=None,
        callback_handler=None,
    )
    context.client_info = client
    return context


async def acomplete(
    policy: EgressPolicy,
    server: McpServer,
    pending: PendingAuthorization,
    *,
    state: str,
    code: str,
    iss: str | None,
    resolver: Resolver | None = None,
    verify: Any = True,
) -> OAuthCredentials:
    """Check the browser's callback and exchange the code for tokens."""
    import httpx2
    from mcp.client.auth.exceptions import OAuthFlowError, OAuthTokenError
    from mcp.client.auth.utils import handle_token_response_scopes, validate_authorization_response_iss
    from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata

    if not secrets.compare_digest(state.encode(), pending.state.encode()):
        raise NodestroError("mcp.oauth_state_invalid", "The sign-in does not belong to this request")
    if time.time() > pending.expires_at:
        raise NodestroError("mcp.oauth_expired", "The sign-in took too long; start it again")
    asm = OAuthMetadata.model_validate(pending.metadata)
    try:
        validate_authorization_response_iss(iss, asm)
    except OAuthFlowError as exc:
        raise _failed(str(exc), issuer=pending.issuer) from None
    if not code or len(code) > 4096:
        raise _failed("The authorization server sent no usable code")

    client = OAuthClientInformationFull.model_validate(pending.client)
    context = _context(server.url, client, pending.redirect_uri, pending.scope)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "client_id": client.client_id,
        "code_verifier": pending.verifier,
        "resource": pending.resource,
    }
    try:
        data, headers = context.prepare_token_auth(data, {"Content-Type": "application/x-www-form-urlencoded"})
    except OAuthTokenError as exc:
        raise _failed(str(exc)) from None
    request = httpx2.Request("POST", str(asm.token_endpoint), data=data, headers=headers)
    async with _client_for(policy, server, resolver, verify) as http:
        response = await _send(http, request)
    if response.status_code not in (200, 201):
        raise _failed(f"The token endpoint refused the code (HTTP {response.status_code})", status=response.status_code)
    try:
        token = await handle_token_response_scopes(response)
    except OAuthTokenError:
        raise _failed("The token endpoint sent an invalid answer") from None
    if token.token_type.lower() != "bearer":
        raise _failed(f"Unsupported token type {token.token_type[:40]}")
    if token.scope is None:
        token.scope = pending.scope
    return OAuthCredentials(
        issuer=pending.issuer,
        resource=pending.resource,
        auth_server_url=pending.auth_server_url,
        metadata=pending.metadata,
        resource_metadata=pending.resource_metadata,
        client=pending.client,
        redirect_uri=pending.redirect_uri,
        tokens=token.model_dump(mode="json", exclude_none=True),
        expires_at=time.time() + token.expires_in if token.expires_in else None,
        scope=token.scope,
    )


class _Storage:
    """The SDK's token storage, backed by :class:`OAuthCredentials` (the service stores them sealed afterwards)."""

    def __init__(self, credentials: OAuthCredentials) -> None:
        self.credentials = credentials

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        return OAuthToken.model_validate(self.credentials.tokens) if self.credentials.tokens else None

    async def set_tokens(self, tokens: Any) -> None:
        self.credentials.tokens = tokens.model_dump(mode="json", exclude_none=True)
        self.credentials.expires_at = time.time() + tokens.expires_in if tokens.expires_in else None
        self.credentials.scope = tokens.scope or self.credentials.scope
        self.credentials.changed = True

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        return OAuthClientInformationFull.model_validate(self.credentials.client)

    async def set_client_info(self, client_info: Any) -> None:
        # Re-registration happens only in an interactive sign-in (abegin), never during a call.
        raise NodestroError("mcp.oauth_reauthorize", "The MCP server asks for a new sign-in")


def provider(server: McpServer, credentials: OAuthCredentials) -> Any:
    """An ``httpx2.Auth`` that presents and refreshes the access token, never opening a browser."""
    from mcp.client.auth.oauth2 import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata, OAuthMetadata, ProtectedResourceMetadata

    class NodestroOAuth(OAuthClientProvider):  # type: ignore[misc,unused-ignore]
        async def _initialize(self) -> None:
            await super()._initialize()
            # The SDK learns the endpoints only from a fresh discovery; restore what the sign-in discovered so that a
            # refresh goes to the right token endpoint and the stored expiry is honored after a restart.
            self.context.oauth_metadata = OAuthMetadata.model_validate(credentials.metadata)
            if credentials.resource_metadata:
                self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(
                    credentials.resource_metadata
                )
            self.context.auth_server_url = credentials.auth_server_url
            if credentials.expires_at is not None:
                self.context.token_expiry_time = credentials.expires_at - _EXPIRY_MARGIN

    metadata = OAuthClientMetadata.model_validate(
        dict(
            redirect_uris=[credentials.redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method=credentials.client.get("token_endpoint_auth_method") or "none",
            scope=credentials.scope,
        )
    )
    return NodestroOAuth(server.url, metadata, _Storage(credentials))


def is_oauth_failure(exc: BaseException) -> bool:
    """True when the SDK gave up because a person has to sign in again."""
    from mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError, OAuthTokenError

    return isinstance(exc, OAuthFlowError | OAuthTokenError | OAuthRegistrationError)


def fingerprint(credentials: OAuthCredentials) -> str:
    """Tells two credential records apart without exposing a token (for caching one provider per server)."""
    raw = json.dumps([credentials.client.get("client_id"), credentials.tokens.get("refresh_token")], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()
