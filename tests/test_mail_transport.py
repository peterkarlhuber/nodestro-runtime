"""NAL-A4: connection checks and a person's test send for ``smtp`` and ``webhook`` connections (local servers only)."""

from __future__ import annotations

from collections.abc import Iterator
from email import message_from_bytes
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

import pytest
from smtp_support import TestSmtpServer, loopback_resolver, self_signed

from nodestro_runtime import mail_transport
from nodestro_runtime.connections import ConnectionIn, ConnectionService, ResolvedConnection
from nodestro_runtime.mail import SIGNATURE_HEADER, verify_signature
from nodestro_runtime.mail_transport import (
    SmtpOptions,
    TransportError,
    check_smtp,
    check_webhook,
    checked_addresses,
    send_smtp_test,
    send_webhook_test,
)
from nodestro_runtime.mcp.vault import TokenVault
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.server_settings import SecretBox
from nodestro_runtime.storage import SQLiteStore

PASSWORD = "secret-pass"


def _smtp(port: int, **settings: Any) -> ResolvedConnection:
    values = {"host": "mail.test", "port": port, "from_address": "bot@example.com", "security": "starttls"}
    values.update(settings)
    return ResolvedConnection(
        id="conn_000000000001",
        type="smtp",
        provider="generic",
        name="Mail",
        settings=values,
        egress=(),
        secret=settings.pop("secret", PASSWORD),
        mode=settings.pop("mode", "desktop"),
    )


def test_addresses_follow_the_egress_rules() -> None:
    assert checked_addresses("mail.test", 25, mode="desktop", resolver=loopback_resolver) == ["127.0.0.1"]
    for mode, answers, reason in (
        ("server", ["127.0.0.1"], "loopback"),
        ("desktop", ["10.0.0.5"], "private"),
        ("desktop", ["93.184.215.14", "169.254.169.254"], None),
    ):
        with pytest.raises(TransportError) as caught:
            checked_addresses("mail.test", 25, mode=mode, resolver=lambda _h, _p, a=answers: a)
        assert caught.value.code == "connection.address_refused"
        if reason:
            assert reason in str(caught.value.params.get("reason"))
    with pytest.raises(TransportError, match="address_refused"):
        checked_addresses("metadata.google.internal", 80, mode="desktop", resolver=loopback_resolver)


def test_smtp_check_uses_starttls_and_logs_in_without_sending(tmp_path: Path) -> None:
    server_tls, client_tls = self_signed(tmp_path)
    with TestSmtpServer(tls=server_tls) as server:
        options = SmtpOptions(resolver=loopback_resolver, tls=client_tls, timeout=5)
        result = check_smtp(_smtp(server.port), options)
        assert result.ok and result.kind == "service"
        assert server.state.logins == ["bot@example.com"] and server.state.secured == [True]
        assert server.messages == []
        assert check_smtp(_smtp(server.port, secret="wrong"), options).code == "connection.auth_failed"


def test_smtp_verifies_the_certificate(tmp_path: Path) -> None:
    server_tls, _ = self_signed(tmp_path)
    with TestSmtpServer(tls=server_tls) as server:
        result = check_smtp(_smtp(server.port), SmtpOptions(resolver=loopback_resolver, timeout=5))
        assert result.code == "connection.tls_failed"


def test_implicit_tls(tmp_path: Path) -> None:
    server_tls, client_tls = self_signed(tmp_path)
    with TestSmtpServer(tls=server_tls, implicit=True) as server:
        options = SmtpOptions(resolver=loopback_resolver, tls=client_tls, timeout=5)
        assert check_smtp(_smtp(server.port, security="tls"), options).ok


def test_starttls_is_required_except_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestSmtpServer() as server:
        options = SmtpOptions(resolver=loopback_resolver, timeout=5)
        assert check_smtp(_smtp(server.port), options).ok  # this computer: plain is allowed
        monkeypatch.setattr(mail_transport, "_is_loopback", lambda _addresses: False)
        assert check_smtp(_smtp(server.port), options).code == "connection.tls_required"


def test_oauth_and_refused_hosts_fail_before_connecting() -> None:
    assert check_smtp(_smtp(1, auth="oauth2")).code == "connection.auth_unsupported"
    refused = _smtp(25, mode="server")
    assert check_smtp(refused, SmtpOptions(resolver=loopback_resolver)).code == "connection.address_refused"


def test_smtp_test_send_goes_to_the_own_address_only(tmp_path: Path) -> None:
    server_tls, client_tls = self_signed(tmp_path)
    with TestSmtpServer(tls=server_tls) as server:
        options = SmtpOptions(resolver=loopback_resolver, tls=client_tls, timeout=5)
        with pytest.raises(TransportError, match="test_address_missing"):
            send_smtp_test(_smtp(server.port), options)
        send_smtp_test(_smtp(server.port, test_address="me@example.org"), options)
        [received] = server.messages
        assert received.sender == "bot@example.com" and received.recipients == ["me@example.org"]
        parsed = message_from_bytes(received.data, policy=default)
        assert parsed["To"] == "me@example.org" and parsed["X-Unsent"] is None
        assert "cannot send e-mail" in parsed.get_body(("plain",)).get_content()


# ---- webhooks -----------------------------------------------------------------------------------------------------


class _Receiver(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]]
    status = 204

    def _answer(self) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        self.seen.append({"method": self.command, "headers": dict(self.headers), "body": self.rfile.read(size)})
        self.send_response(self.status)
        if self.status in (301, 302):
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_POST = do_PUT = _answer

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture
def receiver() -> Iterator[tuple[int, list[dict[str, Any]], type[_Receiver]]]:
    handler = type("Handler", (_Receiver,), {"seen": [], "status": 204})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], handler.seen, handler
    server.shutdown()
    server.server_close()


def _webhook(url: str, *, auth: str = "hmac-sha256", mode: str = "desktop", method: str = "POST") -> ResolvedConnection:
    return ResolvedConnection(
        id="conn_000000000002",
        type="webhook",
        provider="generic",
        name="Hook",
        settings={"url": url, "method": method, "auth": auth},
        egress=(),
        secret="signing-key",
        mode=mode,  # type: ignore[arg-type]
    )


def test_webhook_check_and_signed_test_send(receiver: tuple[int, list[dict[str, Any]], type[_Receiver]]) -> None:
    port, seen, _ = receiver
    url = f"http://127.0.0.1:{port}/hook"
    assert check_webhook(_webhook(url)).ok
    assert seen[-1]["method"] == "HEAD" and seen[-1]["body"] == b""
    result = send_webhook_test(_webhook(url))
    assert result == {"status": 204}
    body = seen[-1]["body"]
    assert json.loads(body)["test"] is True
    header = seen[-1]["headers"][SIGNATURE_HEADER]
    assert verify_signature("signing-key", header, body, now_ts=int(time.time()))
    send_webhook_test(_webhook(url, auth="bearer", method="PUT"))
    assert seen[-1]["method"] == "PUT" and seen[-1]["headers"]["Authorization"] == "Bearer signing-key"


def test_webhook_refuses_redirects_errors_and_internal_targets(
    receiver: tuple[int, list[dict[str, Any]], type[_Receiver]],
) -> None:
    port, _, handler = receiver
    url = f"http://127.0.0.1:{port}/hook"
    handler.status = 302
    with pytest.raises(TransportError, match="redirect_refused"):
        send_webhook_test(_webhook(url))
    handler.status = 500
    with pytest.raises(TransportError) as caught:
        send_webhook_test(_webhook(url))
    assert caught.value.code == "connection.http_status" and caught.value.params == {"status": 500}
    assert check_webhook(_webhook(url, mode="server")).code == "connection.address_refused"
    assert check_webhook(_webhook("http://10.0.0.1/hook")).code == "connection.address_refused"


# ---- the service: limit and audit ---------------------------------------------------------------------------------


def test_test_sends_are_limited_and_audited(
    tmp_path: Path, receiver: tuple[int, list[dict[str, Any]], type[_Receiver]]
) -> None:
    port, seen, handler = receiver
    store = SQLiteStore(tmp_path / "store.db")
    try:
        service = ConnectionService(store, TokenVault(SecretBox(os.urandom(32)), "local", namespace="connection"))
        hook = service.create(
            ConnectionIn(type="webhook", name="Hook", settings={"url": f"http://127.0.0.1:{port}/h", "auth": "none"}),
            actor="admin",
        )
        assert service.test_send(hook.id, actor="admin") == {"ok": True, "status": 204}
        handler.status = 500
        with pytest.raises(NodestroError) as caught:
            service.test_send(hook.id, actor="admin")
        assert caught.value.code == "connection.http_status"
        handler.status = 204
        for _ in range(3):
            service.test_send(hook.id, actor="admin")
        with pytest.raises(NodestroError) as limited:
            service.test_send(hook.id, actor="admin")
        assert limited.value.code == "connection.test_send_limit"
        assert len(seen) == 5
        events = [event for event in service.events() if event.kind == "test_sent"]
        outcomes = sorted(event.details["outcome"] for event in events)
        assert outcomes == ["failed"] + ["sent"] * 4 + ["started"] * 5
        assert all(event.actor == "admin" for event in events)
        assert "127.0.0.1" not in json.dumps([event.details for event in events])
        search = service.create(ConnectionIn(type="search", name="S", provider="test"))
        with pytest.raises(NodestroError, match="no test send"):
            service.test_send(search.id)
    finally:
        store.close()
