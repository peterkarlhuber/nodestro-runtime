"""Tenants, users, roles and sessions of the server profile, in PostgreSQL (ADR-0003).

The HTTP API (``nodestro_api.identity``) logs people in with OpenID Connect and asks this store who they are; the
store keeps the records. Secrets never reach the database in plain text: session tokens, login states and bootstrap
tokens are random 256-bit values, and only their SHA-256 hashes are stored.

- A **user** is one OpenID Connect identity (issuer and subject); nobody is created by default, and there is no
  password and no default administrator.
- A **membership** gives a user one role in a tenant: ``owner``, ``admin``, ``editor``, ``operator`` or ``viewer``.
- A **session** belongs to one user in one tenant. It ends at a fixed time and after a period without requests,
  and it is worth nothing once the membership is gone.
- A **bootstrap token** (made by an operator command) makes the first user who logs in with it the owner of an
  empty tenant; it works once, and only before the tenant has an owner.
- A tenant is ``active`` or ``suspended`` (an operator command). A suspended tenant is closed: its sessions are
  refused at once (``identity.tenant_suspended``), no session starts in it, its setup links do not work and workers
  neither start nor continue its runs. Nothing is deleted; resuming restores access.

With ``NODESTRO_IDENTITY_BACKEND=mirror`` or ``axentra`` the server uses
:class:`~nodestro_runtime.axentra_identity.AxentraIdentityStore` instead, which has this interface and keeps these
tables and Axentra's in step (migration step M5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import secrets
from typing import Any, Literal, cast

from pydantic import BaseModel

from .problems import NodestroError
from .storage_postgres import TENANT_ID, PostgresDatabase

Role = Literal["owner", "admin", "editor", "operator", "viewer"]
ROLES: tuple[Role, ...] = ("owner", "admin", "editor", "operator", "viewer")
TenantStatus = Literal["active", "suspended"]
TENANT_STATUSES: tuple[TenantStatus, ...] = ("active", "suspended")
#: What each role may do in its tenant, by Nodestro permission name (``nodestro_api.permissions.Permission``); the
#: API's ``ROLE_PERMISSIONS`` is this table, and the Axentra definitions are registered from it.
ROLE_GRANTS: dict[Role, tuple[str, ...]] = {
    "owner": ("read", "edit", "run", "admin"),
    "admin": ("read", "edit", "run", "admin"),
    "editor": ("read", "edit", "run"),
    "operator": ("read", "run"),
    "viewer": ("read",),
}


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _now() -> datetime:
    return datetime.now(UTC)


class Tenant(BaseModel):
    id: str
    name: str
    created_at: datetime
    status: TenantStatus = "active"


class User(BaseModel):
    id: str
    issuer: str
    subject: str
    email: str | None = None
    name: str | None = None


class Membership(BaseModel):
    tenant_id: str
    user_id: str
    role: Role
    #: The status of the tenant (a membership of a suspended tenant grants nothing while it is suspended).
    tenant_status: TenantStatus = "active"


class Session(BaseModel):
    """A valid session with the user's current role in its tenant."""

    user_id: str
    tenant_id: str
    role: Role
    expires_at: datetime
    #: The Nodestro permissions of the role (``read``, ``edit``, ``run``, ``admin``) when the identity backend
    #: decides them (``axentra`` mode: Axentra's role definitions); ``None``: :data:`ROLE_GRANTS` applies.
    permissions: tuple[str, ...] | None = None


def tenant_suspended(tenant_id: str) -> NodestroError:
    return NodestroError("identity.tenant_suspended", "This tenant is suspended", tenant=tenant_id)


class LoginState(BaseModel):
    """What the login started with, for the callback: OIDC nonce, PKCE verifier, where to go afterwards."""

    nonce: str
    code_verifier: str
    return_to: str
    #: The tenant the person asked for (``None``: the only one they belong to).
    tenant_id: str | None = None
    bootstrap_token: bool = False
    bootstrap_hash: bytes | None = None


class IdentityStore:
    """The identity records of a server database (tables of migration ``0003``)."""

    def __init__(
        self,
        database: PostgresDatabase,
        *,
        session_lifetime: timedelta = timedelta(hours=12),
        idle_timeout: timedelta = timedelta(hours=2),
        login_lifetime: timedelta = timedelta(minutes=10),
    ) -> None:
        self.database = database
        self.session_lifetime = session_lifetime
        self.idle_timeout = idle_timeout
        self.login_lifetime = login_lifetime

    # ---- tenants -------------------------------------------------------------------------------

    def create_tenant(self, tenant_id: str, name: str) -> Tenant:
        if not TENANT_ID.fullmatch(tenant_id):
            raise NodestroError("identity.tenant_id", f"Invalid tenant id {tenant_id!r}", tenant=tenant_id)
        # Never repeated (RT2-03): a repeat after a lost reply would report the new tenant as existing. When the
        # connection is lost, the stored tenant is the answer (it was created now, or existed already).
        rows = self.database._write_returning(
            "INSERT INTO nodestro_tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING"
            " RETURNING id, name, created_at, status",
            (tenant_id, name.strip() or tenant_id),
            if_lost=("SELECT id, name, created_at, status FROM nodestro_tenants WHERE id = %s", (tenant_id,)),
        )
        if not rows:
            raise NodestroError("identity.tenant_exists", f"Tenant {tenant_id} exists already", tenant=tenant_id)
        return Tenant(id=rows[0][0], name=rows[0][1], created_at=rows[0][2], status=rows[0][3])

    def tenant(self, tenant_id: str) -> Tenant | None:
        """The tenant, active or suspended (``None``: unknown)."""
        rows = self.database._query(
            "SELECT id, name, created_at, status FROM nodestro_tenants WHERE id = %s", (tenant_id,)
        )
        return Tenant(id=rows[0][0], name=rows[0][1], created_at=rows[0][2], status=rows[0][3]) if rows else None

    def tenants(self) -> list[Tenant]:
        """Every tenant, active or suspended (``status`` says which)."""
        rows = self.database._query("SELECT id, name, created_at, status FROM nodestro_tenants ORDER BY id")
        return [Tenant(id=row[0], name=row[1], created_at=row[2], status=row[3]) for row in rows]

    def tenant_active(self, tenant_id: str) -> bool:
        """Whether the tenant exists and is not suspended (the check of every worker before it executes a run)."""
        found = self.tenant(tenant_id)
        return found is not None and found.status == "active"

    def set_tenant_status(self, tenant_id: str, status: TenantStatus) -> Tenant:
        """Suspend or resume a tenant. Suspending ends nothing in the database (sessions are refused while it is
        suspended, so resuming restores them unless they expired); ``identity.tenant_unknown`` for an unknown tenant."""
        if status not in TENANT_STATUSES:
            raise NodestroError("identity.tenant_status", f"Unknown tenant status {status!r}", status=status)
        # Safe to repeat after a lost reply (JQ-3, tests/test_query_writes.py): it sets the same status again.
        rows = self.database._query(
            "UPDATE nodestro_tenants SET status = %s WHERE id = %s RETURNING id, name, created_at, status",
            (status, tenant_id),
        )
        if not rows:
            raise NodestroError("identity.tenant_unknown", f"No tenant {tenant_id}", tenant=tenant_id)
        return Tenant(id=rows[0][0], name=rows[0][1], created_at=rows[0][2], status=rows[0][3])

    # ---- users and roles -----------------------------------------------------------------------

    def login(self, issuer: str, subject: str, *, email: str | None = None, name: str | None = None) -> User:
        """The user of an OpenID Connect identity, created on the first login (without any role)."""
        if not issuer or not subject:
            raise NodestroError("identity.subject_missing", "The identity provider sent no subject")
        # Safe to repeat after a lost reply (JQ-3, tests/test_query_writes.py): the repeat meets the row the first
        # attempt inserted and returns its id.
        rows = self.database._query(
            "INSERT INTO nodestro_users (id, issuer, subject, email, name, last_login_at)"
            " VALUES (%s, %s, %s, %s, %s, clock_timestamp())"
            " ON CONFLICT (issuer, subject) DO UPDATE SET email = excluded.email, name = excluded.name,"
            " last_login_at = excluded.last_login_at RETURNING id",
            ("u_" + secrets.token_hex(12), issuer, subject, email, name),
        )
        return User(id=rows[0][0], issuer=issuer, subject=subject, email=email, name=name)

    def user(self, user_id: str) -> User | None:
        rows = self.database._query(
            "SELECT id, issuer, subject, email, name FROM nodestro_users WHERE id = %s", (user_id,)
        )
        if not rows:
            return None
        row = rows[0]
        return User(id=row[0], issuer=row[1], subject=row[2], email=row[3], name=row[4])

    def set_role(self, tenant_id: str, user_id: str, role: Role) -> Membership:
        if role not in ROLES:
            raise NodestroError("identity.role", f"Unknown role {role!r}", role=role)
        with self.database._transaction() as db:
            if role != "owner":
                self._keep_an_owner(db, tenant_id, user_id)
            db.execute(
                "INSERT INTO nodestro_memberships (tenant_id, user_id, role) VALUES (%s, %s, %s)"
                " ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = excluded.role",
                (tenant_id, user_id, role),
            )
        return Membership(tenant_id=tenant_id, user_id=user_id, role=role)

    def remove_member(self, tenant_id: str, user_id: str) -> None:
        with self.database._transaction() as db:
            self._keep_an_owner(db, tenant_id, user_id)
            db.execute("DELETE FROM nodestro_memberships WHERE tenant_id = %s AND user_id = %s", (tenant_id, user_id))
            db.execute("DELETE FROM nodestro_sessions WHERE tenant_id = %s AND user_id = %s", (tenant_id, user_id))

    @staticmethod
    def _keep_an_owner(db: Any, tenant_id: str, user_id: str) -> None:
        """Refuse to take the owner role from the tenant's last owner (``identity.last_owner``)."""
        execute = db.execute
        execute("SELECT 1 FROM nodestro_tenants WHERE id = %s FOR UPDATE", (tenant_id,))
        rows = execute(
            "SELECT user_id FROM nodestro_memberships WHERE tenant_id = %s AND role = 'owner'", (tenant_id,)
        ).fetchall()
        owners = {row[0] for row in rows}
        if owners == {user_id}:
            raise NodestroError("identity.last_owner", "A tenant needs at least one owner", tenant=tenant_id)

    def role(self, tenant_id: str, user_id: str) -> Role | None:
        rows = self.database._query(
            "SELECT role FROM nodestro_memberships WHERE tenant_id = %s AND user_id = %s", (tenant_id, user_id)
        )
        return cast(Role, rows[0][0]) if rows else None

    def memberships(self, user_id: str) -> list[Membership]:
        """Every membership of the user, with the status of its tenant."""
        rows = self.database._query(
            "SELECT m.tenant_id, m.role, t.status FROM nodestro_memberships m"
            " JOIN nodestro_tenants t ON t.id = m.tenant_id WHERE m.user_id = %s ORDER BY m.tenant_id",
            (user_id,),
        )
        return [Membership(tenant_id=row[0], user_id=user_id, role=row[1], tenant_status=row[2]) for row in rows]

    def members(self, tenant_id: str) -> list[Membership]:
        rows = self.database._query(
            "SELECT user_id, role FROM nodestro_memberships WHERE tenant_id = %s ORDER BY user_id", (tenant_id,)
        )
        return [Membership(tenant_id=tenant_id, user_id=row[0], role=row[1]) for row in rows]

    # ---- sessions ------------------------------------------------------------------------------

    def create_session(self, user_id: str, tenant_id: str) -> str:
        """A new session token for a member of the tenant (``identity.not_a_member`` otherwise;
        ``identity.tenant_suspended`` for a suspended tenant)."""
        if self.role(tenant_id, user_id) is None:
            raise NodestroError("identity.not_a_member", "This account has no role in the tenant", tenant=tenant_id)
        if not self.tenant_active(tenant_id):
            raise tenant_suspended(tenant_id)
        token = secrets.token_urlsafe(32)
        self.database._write(
            "INSERT INTO nodestro_sessions (token_hash, user_id, tenant_id, expires_at) VALUES (%s, %s, %s, %s)",
            (_hash(token), user_id, tenant_id, _now() + self.session_lifetime),
        )
        return token

    def session(self, token: str) -> Session | None:
        """The session of ``token`` with the user's current role; ``None`` when unknown, expired, idle too long
        or when the membership is gone. Raises ``identity.tenant_suspended`` while its tenant is suspended."""
        if not token or len(token) > 256:
            return None
        rows = self.database._query(
            "SELECT s.user_id, s.tenant_id, m.role, s.expires_at, s.last_seen_at, t.status FROM nodestro_sessions s"
            " JOIN nodestro_memberships m ON m.tenant_id = s.tenant_id AND m.user_id = s.user_id"
            " JOIN nodestro_tenants t ON t.id = s.tenant_id"
            " WHERE s.token_hash = %s AND s.expires_at > clock_timestamp()"
            " AND s.last_seen_at > clock_timestamp() - %s",
            (_hash(token), self.idle_timeout),
        )
        if not rows:
            return None
        user_id, tenant_id, role, expires_at, last_seen, status = rows[0]
        if status != "active":
            raise tenant_suspended(tenant_id)
        if _now() - last_seen > timedelta(minutes=1):
            self._touch(_hash(token))
        return Session(user_id=user_id, tenant_id=tenant_id, role=role, expires_at=expires_at)

    def _touch(self, token_hash: bytes) -> None:
        """Note that the session of ``token_hash`` was used now (at most once a minute)."""
        self.database._write(
            "UPDATE nodestro_sessions SET last_seen_at = clock_timestamp() WHERE token_hash = %s", (token_hash,)
        )

    def end_session(self, token: str) -> None:
        self.database._write("DELETE FROM nodestro_sessions WHERE token_hash = %s", (_hash(token),))

    def end_sessions(self, user_id: str) -> int:
        return self.database._write("DELETE FROM nodestro_sessions WHERE user_id = %s", (user_id,))

    # ---- login states --------------------------------------------------------------------------

    def create_login_state(
        self,
        *,
        nonce: str,
        code_verifier: str,
        return_to: str,
        tenant_id: str | None = None,
        bootstrap_token: str | None = None,
    ) -> str:
        """The ``state`` of a login that is starting; the callback takes it back once."""
        state = secrets.token_urlsafe(32)
        self.database._write(
            "INSERT INTO nodestro_login_states"
            " (state_hash, nonce, code_verifier, return_to, tenant_id, bootstrap_hash, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                _hash(state),
                nonce,
                code_verifier,
                return_to,
                tenant_id,
                None if bootstrap_token is None else _hash(bootstrap_token),
                _now() + self.login_lifetime,
            ),
        )
        return state

    def take_login_state(self, state: str) -> LoginState | None:
        """The login of ``state``, removed so it cannot be used twice; ``None`` when unknown or expired.

        Never repeated (RT2-03): a repeat after a lost reply would find the state gone and call it expired. The
        error is raised instead; the state is used up either way."""
        if not state or len(state) > 256:
            return None
        rows = self.database._write_returning(
            "DELETE FROM nodestro_login_states WHERE state_hash = %s"
            " RETURNING nonce, code_verifier, return_to, tenant_id, bootstrap_hash, expires_at > clock_timestamp()",
            (_hash(state),),
        )
        if not rows or not rows[0][5]:
            return None
        nonce, verifier, return_to, tenant_id, bootstrap, _valid = rows[0]
        return LoginState(
            nonce=nonce,
            code_verifier=verifier,
            return_to=return_to,
            tenant_id=tenant_id,
            bootstrap_token=bootstrap is not None,
            bootstrap_hash=None if bootstrap is None else bytes(bootstrap),
        )

    # ---- bootstrap -----------------------------------------------------------------------------

    def create_bootstrap_token(self, tenant_id: str, *, lifetime: timedelta = timedelta(hours=24)) -> str:
        """A one-time token that makes its first user the owner of ``tenant_id`` (which must have no owner)."""
        if self.tenant(tenant_id) is None:
            raise NodestroError("identity.tenant_unknown", f"No tenant {tenant_id}", tenant=tenant_id)
        if any(member.role == "owner" for member in self.members(tenant_id)):
            raise NodestroError("identity.tenant_has_owner", f"Tenant {tenant_id} has an owner", tenant=tenant_id)
        token = secrets.token_urlsafe(32)
        self.database._write(
            "INSERT INTO nodestro_bootstrap_tokens (token_hash, tenant_id, expires_at) VALUES (%s, %s, %s)",
            (_hash(token), tenant_id, _now() + lifetime),
        )
        return token

    def use_bootstrap(self, token_hash: bytes, user_id: str) -> str:
        """Make ``user_id`` the owner with the bootstrap token of ``token_hash``; returns the tenant.

        Works once, before expiry and only while the tenant has no owner (``identity.bootstrap_invalid``)."""
        with self.database._transaction() as db:
            row = db.execute(
                "UPDATE nodestro_bootstrap_tokens SET used_at = clock_timestamp(), used_by = %s"
                " WHERE token_hash = %s AND used_at IS NULL AND expires_at > clock_timestamp() RETURNING tenant_id",
                (user_id, token_hash),
            ).fetchone()
            if row is None:
                raise NodestroError("identity.bootstrap_invalid", "This setup link is invalid, used or expired")
            tenant_id = str(row[0])
            status = db.execute("SELECT status FROM nodestro_tenants WHERE id = %s FOR UPDATE", (tenant_id,)).fetchone()
            if status is not None and status[0] != "active":
                raise tenant_suspended(tenant_id)  # rolled back: the link stays usable once the tenant is resumed
            owner = db.execute(
                "SELECT 1 FROM nodestro_memberships WHERE tenant_id = %s AND role = 'owner'", (tenant_id,)
            ).fetchone()
            if owner is not None:
                raise NodestroError("identity.bootstrap_invalid", "This setup link is invalid, used or expired")
            db.execute(
                "INSERT INTO nodestro_memberships (tenant_id, user_id, role) VALUES (%s, %s, 'owner')"
                " ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = 'owner'",
                (tenant_id, user_id),
            )
        return tenant_id

    def cleanup(self) -> int:
        """Delete expired sessions and login states; returns how many."""
        removed = self.database._write(
            "DELETE FROM nodestro_sessions WHERE expires_at <= clock_timestamp()"
            " OR last_seen_at <= clock_timestamp() - %s",
            (self.idle_timeout,),
        )
        return removed + self.database._write(
            "DELETE FROM nodestro_login_states WHERE expires_at <= clock_timestamp()", ()
        )


__all__ = [
    "ROLES",
    "ROLE_GRANTS",
    "TENANT_STATUSES",
    "IdentityStore",
    "LoginState",
    "Membership",
    "Role",
    "Session",
    "Tenant",
    "TenantStatus",
    "User",
    "tenant_suspended",
]
