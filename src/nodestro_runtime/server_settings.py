"""What a server needs before it starts (plan step PR-12), and the encryption of tenant secrets (ADR-0003 point 6).

:meth:`ServerSettings.from_env` reads the server's configuration from the environment and refuses an incomplete or
unsafe one with ``profile.invalid``, listing every problem at once. Nothing has a default that would weaken it:

========================================  ==================================================================
``NODESTRO_DATABASE_URL`` (or ``_FILE``)  PostgreSQL connection string (libpq ``key=value`` or URL)
``NODESTRO_PUBLIC_ORIGIN``                the studio's origin as browsers see it: ``https://host[:port]``;
                                          ``http://`` only for ``localhost``/loopback (development)
``NODESTRO_OIDC_ISSUER``                  the OpenID Connect issuer (``https://``, loopback ``http://`` for
                                          development)
``NODESTRO_OIDC_CLIENT_ID``               the studio's client at the identity provider
``NODESTRO_OIDC_CLIENT_SECRET`` (or       its client secret
``_FILE``)
``NODESTRO_SECRET_KEY`` (or ``_FILE``)    at least 32 random bytes, base64: encrypts tenant secrets at rest
========================================  ==================================================================

A ``…_FILE`` variable names a file holding the value (container secrets); giving both forms is refused. Secret
values never appear in ``repr``, errors or logs.

:class:`SecretBox` encrypts tenant secrets (provider API keys) with AES-256-GCM. Each tenant gets its own key,
derived from the server's secret key with HKDF-SHA256; the tenant and the field are bound as associated data, so
a value copied to another tenant or another field does not decrypt. Rotation of the server key is not supported
yet: changing it makes the stored secrets unreadable (they must be entered again).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit

from .problems import NodestroError

#: Prefix of an encrypted value; anything else is not a secret this module wrote.
ENCRYPTED_PREFIX = "nodestro:enc:v1:"
_KEY_BYTES = 32
_NONCE_BYTES = 12
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class ServerSettings:
    """A server's validated configuration (see the module documentation); secrets are left out of ``repr``."""

    database_url: str = field(repr=False)
    public_origin: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str = field(repr=False)
    secret_key: bytes = field(repr=False)

    @property
    def redirect_uri(self) -> str:
        """Where the identity provider sends a browser back after the login."""
        return f"{self.public_origin}/api/auth/callback"

    @property
    def secure_cookies(self) -> bool:
        """Whether session cookies carry ``Secure`` (always, except for a loopback development origin)."""
        return urlsplit(self.public_origin).scheme == "https"

    def secret_box(self) -> SecretBox:
        return SecretBox(self.secret_key)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ServerSettings:
        """The settings in ``environ`` (default: ``os.environ``); ``profile.invalid`` lists every problem at once."""
        environ = os.environ if environ is None else environ
        problems: list[dict[str, str]] = []

        def value(name: str, *, secret: bool = False) -> str:
            direct = environ.get(name, "").strip()
            path = environ.get(f"{name}_FILE", "").strip()
            if direct and path:
                problems.append(_problem(name, "", f"set either {name} or {name}_FILE, not both"))
                return ""
            if path:
                try:
                    text = Path(path).read_text(encoding="utf-8").strip()
                except OSError as exc:
                    problems.append(_problem(f"{name}_FILE", path, f"{name}_FILE cannot be read: {exc.strerror}"))
                    return ""
                if not text:
                    problems.append(_problem(f"{name}_FILE", path, f"the file in {name}_FILE is empty"))
                return text
            if not direct:
                hint = f" (or {name}_FILE)" if secret else ""
                problems.append(_problem(name, "", f"{name}{hint} is required"))
            return direct

        database_url = value("NODESTRO_DATABASE_URL", secret=True)
        origin = _origin(environ.get("NODESTRO_PUBLIC_ORIGIN", "").strip(), "NODESTRO_PUBLIC_ORIGIN", problems)
        issuer = _origin(
            environ.get("NODESTRO_OIDC_ISSUER", "").strip(), "NODESTRO_OIDC_ISSUER", problems, path_allowed=True
        )
        client_id = environ.get("NODESTRO_OIDC_CLIENT_ID", "").strip()
        if not client_id:
            problems.append(_problem("NODESTRO_OIDC_CLIENT_ID", "", "NODESTRO_OIDC_CLIENT_ID is required"))
        client_secret = value("NODESTRO_OIDC_CLIENT_SECRET", secret=True)
        key_text = value("NODESTRO_SECRET_KEY", secret=True)
        key = b""
        if key_text:
            try:
                key = base64.b64decode(key_text + "=" * (-len(key_text) % 4), altchars=b"-_", validate=False)
            except (binascii.Error, ValueError):
                key = b""
            if len(key) < _KEY_BYTES:
                problems.append(
                    _problem(
                        "NODESTRO_SECRET_KEY",
                        "",
                        f"NODESTRO_SECRET_KEY must be at least {_KEY_BYTES} random bytes, base64-encoded "
                        "(for example: openssl rand -base64 32)",
                    )
                )
        if problems:
            raise NodestroError(
                "profile.invalid",
                "The server cannot start: " + "; ".join(item["reason"] for item in problems),
                problems=problems,
            )
        return cls(
            database_url=database_url,
            public_origin=origin,
            oidc_issuer=issuer,
            oidc_client_id=client_id,
            oidc_client_secret=client_secret,
            secret_key=key,
        )


def _problem(name: str, value: str, reason: str) -> dict[str, str]:
    return {"field": name, "value": value, "reason": reason}


def _origin(text: str, name: str, problems: list[dict[str, str]], *, path_allowed: bool = False) -> str:
    """``text`` without a trailing slash if it is an https origin (or a loopback http one); else a problem."""
    if not text:
        problems.append(_problem(name, "", f"{name} is required"))
        return ""
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    try:
        parts.port  # noqa: B018 - raises for an invalid port
    except ValueError:
        problems.append(_problem(name, text, f"{name} has an invalid port"))
        return ""
    if parts.scheme not in ("https", "http") or not host or parts.username or parts.password:
        problems.append(_problem(name, text, f"{name} must be an https:// address"))
        return ""
    if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
        problems.append(_problem(name, text, f"{name} must use https:// (http:// only for localhost)"))
        return ""
    if parts.query or parts.fragment or (parts.path not in ("", "/") and not path_allowed):
        problems.append(_problem(name, text, f"{name} must be an origin (scheme, host and port only)"))
        return ""
    return text.rstrip("/")


class SecretBox:
    """Encrypts and decrypts tenant secrets (see the module documentation)."""

    def __init__(self, key: bytes) -> None:
        if len(key) < _KEY_BYTES:
            raise ValueError(f"the key must have at least {_KEY_BYTES} bytes")
        self._key = bytes(key)

    def __repr__(self) -> str:
        return "SecretBox(<key hidden>)"

    def _tenant_key(self, tenant_id: str) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        return HKDF(
            algorithm=hashes.SHA256(),
            length=_KEY_BYTES,
            salt=None,
            info=b"nodestro tenant secrets v1\0" + tenant_id.encode(),
        ).derive(self._key)

    @staticmethod
    def _context(tenant_id: str, name: str) -> bytes:
        return f"{tenant_id}\0{name}".encode()

    def encrypt(self, tenant_id: str, name: str, plaintext: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(_NONCE_BYTES)
        sealed = AESGCM(self._tenant_key(tenant_id)).encrypt(
            nonce, plaintext.encode("utf-8"), self._context(tenant_id, name)
        )
        return ENCRYPTED_PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")

    def decrypt(self, tenant_id: str, name: str, token: str) -> str:
        """The secret in ``token``; ``secrets.unreadable`` if it was not written for this tenant and field with this
        server's key (a changed key, a copied value or a damaged one)."""
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not is_encrypted(token):
            raise NodestroError("secrets.unreadable", "A stored secret is not encrypted; enter it again")
        try:
            raw = base64.urlsafe_b64decode(token[len(ENCRYPTED_PREFIX) :].encode("ascii"))
            plaintext = AESGCM(self._tenant_key(tenant_id)).decrypt(
                raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], self._context(tenant_id, name)
            )
        except (InvalidTag, ValueError, binascii.Error):
            raise NodestroError(
                "secrets.unreadable", "A stored secret cannot be decrypted with this server's key; enter it again"
            ) from None
        return plaintext.decode("utf-8")


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX)


__all__ = ["ENCRYPTED_PREFIX", "SecretBox", "ServerSettings", "is_encrypted"]
