"""The two ways Nodestro talks to a mail server or webhook in NAL-A4: a *check* of a connection and a person's *test
send* from the studio. No tool and no agent reaches this module (the draft tools only build files; the
``tests/test_draft_tools.py`` import check keeps it that way). Real delivery from workflows is NAL-A5 and stays locked.

SMTP (rules ported from Levuvo Mail ``send-service.mjs:27-43``, code new, no Axentra import):

- encrypted transport only: implicit TLS (``security="tls"``, usually 465) or STARTTLS (587), which is required
  unless the server is this computer (loopback, never on a server);
- TLS 1.2 or newer, certificate and host name verified;
- every resolved address is checked by the shared egress rules (:func:`orkexa.net.guard.address_problem`) and the
  connection goes to the checked address (no second lookup);
- one time limit of :data:`SMTP_TIMEOUT` seconds per network operation;
- the check logs in (when there is a secret) and quits; it never sends.

Webhooks go through Orkexa's guarded HTTP client (scheme, ports, every resolved address, no environment proxies, no
redirects). A loopback target is only allowed outside the server profile and then only that one host.

Results never carry the service's own text, an address or the secret: only codes (``connection.*``) and, for an
HTTP answer, its status.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import ipaddress
import json
import smtplib
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

from orkexa.net.guard import METADATA_HOSTS, address_problem

from .connections import CheckResult, ResolvedConnection
from .mail import SIGNATURE_HEADER, MailDraft, build_mail, signature
from .schemas import now

#: Seconds for each SMTP network step (connect, greeting, TLS, login, data).
SMTP_TIMEOUT = 20.0
#: The SMTP ports a server profile may contact (submission, implicit TLS, relay, the common alternative).
SERVER_SMTP_PORTS = frozenset({25, 465, 587, 2525})
#: Seconds for a whole webhook exchange.
WEBHOOK_TIMEOUT = 15.0
TEST_SUBJECT = "Nodestro test message"
TEST_TEXT = (
    "This is a test message sent by a person from Nodestro to check this mail connection.\n"
    "Agents and workflows cannot send e-mail in this version; they prepare drafts only.\n"
)

Resolver = Callable[[str, int], list[str]]


class TransportError(Exception):
    """A step failed; ``code`` is the error code shown to the person (never the service's message)."""

    def __init__(self, code: str, **params: Any) -> None:
        super().__init__(code)
        self.code = code
        self.params = params


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _system_resolver(host: str, port: int) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def checked_addresses(host: str, port: int, *, mode: str, resolver: Resolver | None = None) -> list[str]:
    """Every address of ``host``, each allowed by the egress rules (loopback only outside the server profile); one
    refused answer refuses the name (mixed answers are an attack)."""
    name = host.strip().strip("[]").lower().rstrip(".")
    if name in METADATA_HOSTS:
        raise TransportError("connection.address_refused", reason="metadata")
    try:
        answers = [str(ipaddress.ip_address(name))]
    except ValueError:
        try:
            answers = (resolver or _system_resolver)(name, port)
        except OSError:
            raise TransportError("connection.dns_failed") from None
    if not answers:
        raise TransportError("connection.dns_failed")
    found: list[str] = []
    for answer in answers:
        if "%" in answer:
            raise TransportError("connection.address_refused", reason="scoped")
        try:
            address = ipaddress.ip_address(answer)
        except ValueError:
            continue
        reason = address_problem(address, allow_loopback=mode != "server")
        if reason is not None:
            raise TransportError("connection.address_refused", reason=reason)
        found.append(str(address))
    if not found:
        raise TransportError("connection.dns_failed")
    return found


def _is_loopback(addresses: list[str]) -> bool:
    return all(ipaddress.ip_address(item).is_loopback for item in addresses)


# ---- SMTP ---------------------------------------------------------------------------------------------------------


class _PinnedSMTP(smtplib.SMTP):
    """``smtplib.SMTP`` that connects to a checked address while TLS verifies the configured host name."""

    def __init__(
        self, address: str, server_name: str, *, implicit_tls: bool, context: ssl.SSLContext, timeout: float
    ) -> None:
        self._address = address
        self._implicit_tls = implicit_tls
        self._tls = context
        super().__init__(timeout=timeout)
        self._host = server_name  # what STARTTLS verifies (smtplib only sets it when __init__ connects)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        sock = socket.create_connection((self._address, port), timeout)
        if self._implicit_tls:
            sock = self._tls.wrap_socket(sock, server_hostname=host)
        return sock


@dataclass(frozen=True)
class SmtpOptions:
    """Test hooks: a resolver and the TLS context (production: system name lookup, default verification)."""

    resolver: Resolver | None = None
    tls: ssl.SSLContext | None = None
    timeout: float = SMTP_TIMEOUT


@contextmanager
def smtp_session(connection: ResolvedConnection, options: SmtpOptions | None = None) -> Iterator[smtplib.SMTP]:
    """A logged-in SMTP session to ``connection`` (see the module documentation); raises :class:`TransportError`."""
    options = options or SmtpOptions()
    settings = connection.settings
    host = str(settings["host"]).lower()
    port = int(settings.get("port") or 587)
    implicit = settings.get("security") == "tls"
    if settings.get("auth") == "oauth2":
        raise TransportError("connection.auth_unsupported")
    if connection.mode == "server" and port not in SERVER_SMTP_PORTS:
        # On a shared server a check must not become a port scanner (review L2).
        raise TransportError("connection.port_refused", port=port)
    addresses = checked_addresses(host, port, mode=connection.mode, resolver=options.resolver)
    context = options.tls or _tls_context()
    client: smtplib.SMTP | None = None
    last: TransportError | None = None
    for address in addresses:
        try:
            client = _PinnedSMTP(address, host, implicit_tls=implicit, context=context, timeout=options.timeout)
            client.connect(host, port)
            break
        except ssl.SSLError:
            last = TransportError("connection.tls_failed")
        except TimeoutError:
            last = TransportError("connection.timeout")
        except (OSError, smtplib.SMTPException):
            last = TransportError("connection.unreachable")
        client = None
    if client is None:
        raise last or TransportError("connection.unreachable")
    try:
        try:
            client.ehlo()
            if not implicit:
                if client.has_extn("starttls"):
                    client.starttls(context=context)
                    client.ehlo()
                elif not _is_loopback(addresses):
                    raise TransportError("connection.tls_required")
            username = settings.get("username") or settings.get("from_address")
            if connection.secret:
                client.login(str(username), connection.secret)
        except TransportError:
            raise
        except smtplib.SMTPAuthenticationError:
            raise TransportError("connection.auth_failed") from None
        except smtplib.SMTPNotSupportedError:
            raise TransportError("connection.auth_unsupported") from None
        except ssl.SSLError:
            raise TransportError("connection.tls_failed") from None
        except TimeoutError:
            raise TransportError("connection.timeout") from None
        except (OSError, smtplib.SMTPException):
            raise TransportError("connection.unreachable") from None
        yield client
    finally:
        try:
            client.quit()
        except (OSError, smtplib.SMTPException):
            client.close()


def check_smtp(connection: ResolvedConnection, options: SmtpOptions | None = None) -> CheckResult:
    """Connect, secure, log in and quit; nothing is sent."""
    try:
        with smtp_session(connection, options):
            pass
    except TransportError as exc:
        return CheckResult(ok=False, code=exc.code, kind="service")
    return CheckResult(ok=True, kind="service")


def send_smtp_test(connection: ResolvedConnection, options: SmtpOptions | None = None) -> dict[str, Any]:
    """Send :data:`TEST_TEXT` to the connection's own ``test_address`` (a person's action, never an agent's)."""
    settings = connection.settings
    target = settings.get("test_address")
    if not target:
        raise TransportError("connection.test_address_missing")
    sender = str(settings["from_address"])
    built = build_mail(
        MailDraft(to=(str(target),), subject=TEST_SUBJECT, body_text=TEST_TEXT, from_address=sender), unsent=False
    )
    with smtp_session(connection, options) as client:
        try:
            refused = client.sendmail(sender, [str(target)], built.data)
        except smtplib.SMTPRecipientsRefused:
            raise TransportError("connection.recipient_refused") from None
        except smtplib.SMTPSenderRefused:
            raise TransportError("connection.sender_refused") from None
        except TimeoutError:
            raise TransportError("connection.timeout") from None
        except (OSError, smtplib.SMTPException):
            raise TransportError("connection.send_failed") from None
    if refused:
        raise TransportError("connection.recipient_refused")
    return {"size": len(built.data)}


# ---- webhooks -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookOptions:
    """Test hooks for the guarded client (``resolver`` async ``(host, port) -> [address]``)."""

    resolver: Any = None
    timeout: float = WEBHOOK_TIMEOUT


def _policy(url: str, mode: str, timeout: float) -> tuple[Any, Any]:
    from orkexa.net.guard import EgressPolicy, GuardedBackend

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    loopback = host == "localhost" or host.endswith(".localhost")
    with suppress(ValueError):
        loopback = loopback or ipaddress.ip_address(host.strip("[]")).is_loopback
    local = loopback and mode != "server"
    policy = EgressPolicy(
        schemes=frozenset({"https", "http"}) if local else frozenset({"https"}),
        allow_private=local,
        allow_hosts=frozenset({host}) if local else None,
        ports=None,
        max_redirects=0,
        max_bytes=64 * 1024,
        timeout=timeout,
    )
    return policy, GuardedBackend(allow_loopback=local)


async def _exchange(connection: ResolvedConnection, method: str, body: bytes | None, options: WebhookOptions) -> int:
    import httpx
    from orkexa.net.guard import EgressError
    from orkexa.net.http import deadline, guarded_client

    url = str(connection.settings["url"])
    policy, backend = _policy(url, connection.mode, options.timeout)
    headers = {"User-Agent": "Nodestro"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        auth = connection.settings.get("auth")
        if auth == "hmac-sha256" and connection.secret:
            headers[SIGNATURE_HEADER] = signature(connection.secret, int(time.time()), body)
        elif auth == "bearer" and connection.secret:
            headers["Authorization"] = f"Bearer {connection.secret}"
    try:
        policy.check_url(url)
        async with guarded_client(policy, resolver=options.resolver, backend=backend) as client:
            with deadline(policy):
                response = await client.request(method, url, content=body, headers=headers)
    except EgressError as exc:
        code = "connection.timeout" if exc.code == "timeout" else "connection.address_refused"
        raise TransportError(code) from None
    except httpx.TimeoutException:
        raise TransportError("connection.timeout") from None
    except (httpx.HTTPError, OSError):
        raise TransportError("connection.unreachable") from None
    if response.is_redirect:
        raise TransportError("connection.redirect_refused", status=response.status_code)
    return response.status_code


def _run(coroutine: Any) -> Any:
    import anyio

    async def runner() -> Any:
        return await coroutine

    try:
        return anyio.run(runner)
    except RuntimeError:  # called from a running event loop: use a worker thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(anyio.run, runner).result()


def check_webhook(connection: ResolvedConnection, options: WebhookOptions | None = None) -> CheckResult:
    """``HEAD`` the target: any HTTP answer means it is reachable (nothing is posted)."""
    try:
        _run(_exchange(connection, "HEAD", None, options or WebhookOptions()))
    except TransportError as exc:
        return CheckResult(ok=False, code=exc.code, kind="service")
    return CheckResult(ok=True, kind="service")


def probe_payload() -> dict[str, Any]:
    """The body of a test send: marked ``test: true`` so a receiver can ignore it."""
    return {"test": True, "event": "nodestro.connection_test", "id": uuid.uuid4().hex, "sent_at": now().isoformat()}


def send_webhook_test(connection: ResolvedConnection, options: WebhookOptions | None = None) -> dict[str, Any]:
    """Send :func:`probe_payload` with the connection's method, signature or token; a 2xx answer succeeds."""
    body = json.dumps(probe_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    method = str(connection.settings.get("method") or "POST")
    status = _run(_exchange(connection, method, body, options or WebhookOptions()))
    if not 200 <= status < 300:
        raise TransportError("connection.http_status", status=status)
    return {"status": status}


__all__ = [
    "SMTP_TIMEOUT",
    "TEST_SUBJECT",
    "WEBHOOK_TIMEOUT",
    "SmtpOptions",
    "TransportError",
    "WebhookOptions",
    "check_smtp",
    "check_webhook",
    "checked_addresses",
    "probe_payload",
    "send_smtp_test",
    "send_webhook_test",
    "smtp_session",
]
