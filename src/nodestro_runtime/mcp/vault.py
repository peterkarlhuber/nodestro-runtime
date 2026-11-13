"""Where MCP tokens live (decision D-3 of ADR-0007).

A token is stored only encrypted: on a server with the server's secret key (``SecretBox``, one derived key per
tenant), on a desktop with a key from the operating system's keychain, which the desktop app hands to the backend
in ``NODESTRO_VAULT_KEY``. Without such a key a token is never written anywhere: it lives in the process's memory
for the current session (``session``), and the server must be connected again after a restart. There is no
plain-text fallback, also not for static tokens.
"""

from __future__ import annotations

import base64
import binascii
import os

from ..problems import NodestroError
from ..server_settings import SecretBox, TenantSecrets

VAULT_KEY_ENV = "NODESTRO_VAULT_KEY"
_KEY_BYTES = 32


def vault_box_from_env(environ: dict[str, str] | None = None) -> SecretBox | None:
    """The desktop's token box from ``NODESTRO_VAULT_KEY`` (base64, at least 32 bytes); ``None`` without a key.

    A malformed key is refused loudly: silently falling back to session-only storage would hide a broken keychain."""
    env = os.environ if environ is None else environ
    text = env.get(VAULT_KEY_ENV, "").strip()
    if not text:
        return None
    try:
        key = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=False)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) < _KEY_BYTES:
        raise NodestroError(
            "profile.invalid",
            f"{VAULT_KEY_ENV} must be at least {_KEY_BYTES} random bytes, base64-encoded",
            problems=[{"field": VAULT_KEY_ENV, "value": "", "reason": "too short or not base64"}],
        )
    return SecretBox(key)


class TokenVault:
    """Seals and opens secrets of one tenant; ``persistent`` tells whether secrets can be stored at all.

    ``namespace`` keeps the secrets of different owners apart: MCP tokens (``mcp``) and the secrets of service
    connections (``connection``, :mod:`nodestro_runtime.connections`) use the same key store, and a value sealed for
    one namespace, tenant or id cannot be opened as another (the field is part of the encryption context)."""

    def __init__(self, box: TenantSecrets | None, tenant_id: str, *, namespace: str = "mcp") -> None:
        self._box = box
        self.tenant_id = tenant_id
        self.namespace = namespace

    @property
    def persistent(self) -> bool:
        return self._box is not None

    def _field(self, owner_id: str) -> str:
        return f"{self.namespace}:{owner_id}"

    def seal(self, server_id: str, token: str) -> str:
        if self._box is None:
            if self.namespace == "mcp":
                raise NodestroError("mcp.vault_unavailable", "No secure key store is available for MCP tokens")
            raise NodestroError("secrets.vault_unavailable", "No secure key store is available for secrets")
        return self._box.encrypt(self.tenant_id, self._field(server_id), token)

    def open(self, server_id: str, sealed: str) -> str | None:
        if self._box is None:
            return None
        try:
            return self._box.decrypt(self.tenant_id, self._field(server_id), sealed)
        except Exception:
            return None
