"""A small SMTP server for the tests of NAL-A4 (loopback only, one thread per connection).

It speaks just enough SMTP for ``smtplib``: EHLO, STARTTLS (when given a certificate), AUTH PLAIN and LOGIN, MAIL,
RCPT, DATA, RSET, NOOP and QUIT. Received messages are kept in :attr:`TestSmtpServer.messages`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import ipaddress
from pathlib import Path
import socket
import socketserver
import ssl
import threading
from typing import Any


@dataclass
class Received:
    sender: str
    recipients: list[str]
    data: bytes


@dataclass
class _State:
    username: str = "bot@example.com"
    password: str = "secret-pass"
    tls: ssl.SSLContext | None = None
    offer_starttls: bool = True
    messages: list[Received] = field(default_factory=list)
    logins: list[str] = field(default_factory=list)
    secured: list[bool] = field(default_factory=list)


class _Handler(socketserver.StreamRequestHandler):
    server: Any

    def _send(self, line: str) -> None:
        self.wfile.write(f"{line}\r\n".encode())
        self.wfile.flush()

    def _line(self) -> str:
        raw = self.rfile.readline(65536)
        if not raw:
            raise ConnectionError
        return raw.decode("utf-8", "replace").rstrip("\r\n")

    def handle(self) -> None:
        state: _State = self.server.state
        secure = self.server.implicit
        sender = ""
        recipients: list[str] = []
        self._send("220 test ESMTP")
        try:
            while True:
                line = self._line()
                verb = line.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    lines = ["250-test"]
                    if state.tls is not None and state.offer_starttls and not secure:
                        lines.append("250-STARTTLS")
                    lines.append("250 AUTH PLAIN LOGIN")
                    for item in lines:
                        self._send(item)
                elif verb == "STARTTLS" and state.tls is not None and not secure:
                    self._send("220 go ahead")
                    wrapped = state.tls.wrap_socket(self.connection, server_side=True)
                    self.connection = wrapped
                    self.rfile = wrapped.makefile("rb")
                    self.wfile = wrapped.makefile("wb")
                    secure = True
                elif verb == "AUTH":
                    parts = line.split(" ")
                    if parts[1].upper() == "PLAIN":
                        token = parts[2] if len(parts) > 2 else (self._send("334 ") or self._line())
                        _, user, password = base64.b64decode(token).decode().split("\0")
                    else:
                        self._send("334 VXNlcm5hbWU6")
                        user = base64.b64decode(self._line()).decode()
                        self._send("334 UGFzc3dvcmQ6")
                        password = base64.b64decode(self._line()).decode()
                    if (user, password) == (state.username, state.password):
                        state.logins.append(user)
                        state.secured.append(secure)
                        self._send("235 ok")
                    else:
                        self._send("535 authentication failed")
                elif verb == "MAIL":
                    sender = line.split(":", 1)[1].strip().strip("<>").split(">")[0]
                    recipients = []
                    self._send("250 ok")
                elif verb == "RCPT":
                    recipients.append(line.split(":", 1)[1].strip().strip("<>").split(">")[0])
                    self._send("250 ok")
                elif verb == "DATA":
                    self._send("354 go")
                    chunks: list[bytes] = []
                    while True:
                        raw = self.rfile.readline(1 << 20)
                        if raw in (b".\r\n", b".\n", b""):
                            break
                        chunks.append(raw[1:] if raw.startswith(b"..") else raw)
                    state.messages.append(Received(sender, recipients, b"".join(chunks)))
                    self._send("250 queued")
                elif verb == "QUIT":
                    self._send("221 bye")
                    return
                elif verb in ("RSET", "NOOP"):
                    self._send("250 ok")
                else:
                    self._send("502 not implemented")
        except (ConnectionError, OSError, ssl.SSLError, ValueError):
            return


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class TestSmtpServer:
    """``with TestSmtpServer(...) as server: server.port``. ``implicit=True`` wraps every connection in TLS."""

    __test__ = False

    def __init__(self, *, tls: ssl.SSLContext | None = None, implicit: bool = False, offer_starttls: bool = True):
        self.state = _State(tls=tls, offer_starttls=offer_starttls)
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.state = self.state  # type: ignore[attr-defined]
        self._server.implicit = implicit  # type: ignore[attr-defined]
        if implicit:
            assert tls is not None
            self._server.socket = tls.wrap_socket(self._server.socket, server_side=True)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def messages(self) -> list[Received]:
        return self.state.messages

    def __enter__(self) -> TestSmtpServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def self_signed(tmp_path: Path, host: str = "mail.test") -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """A server context with a fresh certificate for ``host`` and a client context that trusts only it (TLS 1.2+)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    start = datetime.now(UTC) - timedelta(minutes=5)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.minimum_version = ssl.TLSVersion.TLSv1_2
    server.load_cert_chain(cert_file, key_file)
    client = ssl.create_default_context(cafile=str(cert_file))
    client.minimum_version = ssl.TLSVersion.TLSv1_2
    return server, client


def loopback_resolver(_host: str, _port: int) -> list[str]:
    return ["127.0.0.1"]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
