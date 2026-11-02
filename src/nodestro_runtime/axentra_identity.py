"""Users, tenants, memberships and sessions of the server profile on Axentra (migration step M5; off by default).

``NODESTRO_IDENTITY_BACKEND`` (read with the other Axentra settings, :class:`~nodestro_runtime.axentra_platform.
AxentraConfig`) chooses where the identity records live. A change is a restart of the API and the workers:

``nodestro`` (default)
    :class:`~nodestro_runtime.identity_store.IdentityStore` alone, exactly as before. Axentra is not touched.
``mirror``
    Nodestro's tables answer every read. **Every write** also writes Axentra's records in the **same PostgreSQL
    transaction** (:meth:`AxentraLink.joined`: Nodestro's connection with Axentra's services joined to it), so a
    failure on either side rolls back both: tenants, users (an Axentra external identity keyed by the OIDC issuer
    and subject), memberships (role map below), sessions (the same token: same SHA-256 hash on both sides),
    ``last_seen_at`` touches, logouts, removals and housekeeping.
``axentra``
    Axentra answers the reads: session validity (absolute and idle expiry and revocation on the database clock,
    :meth:`axentra.identity.service.IdentityService.resolve_session`), roles and the permissions they grant
    (:attr:`Session.permissions`, from Axentra's role definitions), memberships, member lists, the tenant list and
    tenant status (suspended when either side says so). The API lets Axentra run the OIDC login too
    (``nodestro_api.identity.AxentraLoginFlow``). Writes still go to both sides in one transaction, so Nodestro's
    tables stay current and switching back to ``mirror`` or ``nodestro`` is a restart (the rollback).

What stays Nodestro-only in every mode: bootstrap tokens (setup links; plan gap G-5 decided: one store for every
mode and the rollback), Nodestro's ids (``u_…``, tenant ids) in every contract, the e-mail column (Axentra keeps an
address only when it is not another Axentra user's: **identities are never merged by e-mail**), the active tenant of
a session (``nodestro_sessions.tenant_id``), the last-owner guard and removal ending the member's sessions in the
tenant at once. Login states are Nodestro's in ``nodestro``/``mirror`` mode and Axentra's in ``axentra`` mode.

Role map (plan 3.4): ``owner`` -> ``tenant_owner`` (Axentra's last-owner guard applies), ``admin`` ->
``tenant_admin``, ``viewer`` -> ``viewer`` (built-in), ``editor`` and ``operator`` -> custom tenant roles of the
same key in product ``nodestro`` (:data:`CUSTOM_ROLES`). Nodestro's ``read/edit/run/admin`` are product permissions
``nodestro.workspace:*`` (:data:`PRODUCT_PERMISSIONS`); both are applied as versioned access definitions
(:func:`access_definitions`, :data:`ACCESS_REVISION`; Axentra's ``ax.access_definitions``): once per revision, never
downgraded by an older process, a conflicting revision stops the start.

Sessions: Axentra's ``axentra_sessions.token_hash`` is the hex form of Nodestro's ``bytea`` hash of the same token,
so the hash itself joins the two rows (no mapping row, no third copy of a hash). New sessions are started with
Axentra (:meth:`~axentra.identity.service.IdentityService.start_session` makes the token) and Nodestro stores the
hash of that token; both rows get Nodestro's expiry (``session_lifetime``).

Operator side (:func:`backfill_identity`, :func:`reconcile_identity`, :func:`identity_readiness`): the backfill
copies Nodestro's records into Axentra (idempotent, resumable, one transaction per record), the reconciliation
compares both sides and reports differences by ids and counts only (never tokens or hashes). ``axentra`` mode
refuses to start while the quick readiness check finds records that are not on both sides.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any, Literal, cast

from .axentra_platform import LINK_LOCK_CLASS, AxentraConfig, AxentraLink, AxentraServices
from .identity_store import (
    ROLE_GRANTS,
    ROLES,
    TENANT_STATUSES,
    IdentityStore,
    Membership,
    Role,
    Session,
    Tenant,
    TenantStatus,
    User,
    _hash,
    _now,
    tenant_suspended,
)
from .problems import NodestroError
from .storage_postgres import PostgresDatabase

logger = logging.getLogger(__name__)

Mode = Literal["mirror", "axentra"]

#: Nodestro role -> Axentra tenant role key.
ROLE_TO_AXENTRA: dict[str, str] = {
    "owner": "tenant_owner",
    "admin": "tenant_admin",
    "editor": "editor",
    "operator": "operator",
    "viewer": "viewer",
}
#: Axentra tenant role key -> Nodestro role; any other Axentra role grants nothing in Nodestro.
ROLE_FROM_AXENTRA: dict[str, Role] = {value: cast(Role, key) for key, value in ROLE_TO_AXENTRA.items()}
#: Nodestro's permissions as product permissions of product ``nodestro`` (tenant scope): Nodestro name -> Axentra
#: name and description. ``tenant_owner``/``tenant_admin`` hold every tenant-scoped product permission by Axentra's
#: rules; ``viewer`` holds ``read`` through ``default_roles``; the custom roles list theirs.
PRODUCT_PERMISSIONS: dict[str, tuple[str, str]] = {
    "read": ("nodestro.workspace:read", "Look at workflows, runs, files, settings and live event streams"),
    "edit": ("nodestro.workspace:edit", "Create, change and delete workflows, agents, knowledge, memory and files"),
    "run": ("nodestro.workspace:run", "Execute runs, approvals, batches, schedules, tool code and model requests"),
    "admin": ("nodestro.workspace:admin", "Tenant settings, provider keys, API keys, publishing, packages"),
}
#: Axentra product permission name -> Nodestro permission name.
PERMISSION_FROM_AXENTRA: dict[str, str] = {name: key for key, (name, _text) in PRODUCT_PERMISSIONS.items()}
_GENERIC = {
    "editor": ("tenant:read", "members:read", "files:read", "files:write", "usage:read", "notify:preferences"),
    "operator": ("tenant:read", "members:read", "files:read", "usage:read", "notify:preferences"),
}
#: Custom tenant roles of product ``nodestro`` (plan 3.4): generic Axentra tenant permissions (no
#: ``members:manage``, no tenant settings) plus Nodestro's permissions of the role (:data:`ROLE_GRANTS`).
CUSTOM_ROLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "editor": ("Editor", _GENERIC["editor"] + tuple(PRODUCT_PERMISSIONS[p][0] for p in ROLE_GRANTS["editor"])),
    "operator": ("Operator", _GENERIC["operator"] + tuple(PRODUCT_PERMISSIONS[p][0] for p in ROLE_GRANTS["operator"])),
}
#: The revision of :func:`access_definitions`. Raise it whenever their content changes (a test pins the content
#: hash of every revision): Axentra applies a newer revision once, and an older process never downgrades it.
ACCESS_REVISION = 1
#: ``auth_method`` and ``user_agent`` of the Axentra sessions Nodestro starts (it keeps its own OIDC flow).
SESSION_METHOD = "oidc"
SESSION_AGENT = "nodestro"
_NAME_LIMIT = 200  # axentra_users.name / axentra_tenants.name


def access_definitions() -> Any:
    """Nodestro's permissions and custom roles for Axentra (:class:`axentra.access.definitions.AccessDefinitions`,
    revision :data:`ACCESS_REVISION`), applied at every start of ``mirror``/``axentra`` (no "last start wins")."""
    from axentra.access.definitions import AccessDefinitions, RoleDefinition
    from axentra.access.product_permissions import ProductPermission

    return AccessDefinitions(
        ACCESS_REVISION,
        tuple(
            ProductPermission(
                name,
                "tenant",
                text,
                default_roles=("viewer",) if key in ROLE_GRANTS["viewer"] else (),
                read_like=key == "read",
            )
            for key, (name, text) in PRODUCT_PERMISSIONS.items()
        ),
        tuple(
            RoleDefinition(key, label, "tenant", permissions, f"Nodestro role {key}")
            for key, (label, permissions) in CUSTOM_ROLES.items()
        ),
    )


def axentra_name(name: str | None) -> str:
    """The name Axentra stores for a Nodestro user name (``''`` for none; Axentra trims and caps at 200)."""
    return (name or "").strip()[:_NAME_LIMIT]


def axentra_tenant_name(tenant_id: str, name: str) -> str:
    return name.strip()[:_NAME_LIMIT] or tenant_id


def _platform_scope() -> Any:
    from axentra.data.database import DbScope

    return DbScope.platform()


def _system() -> Any:
    from axentra.core.context import Principal

    return Principal.system("nodestro-identity")


class AxentraIdentityStore(IdentityStore):
    """:class:`IdentityStore` with Axentra underneath (``mode`` ``mirror`` or ``axentra``, see the module docs)."""

    def __init__(
        self,
        database: PostgresDatabase,
        link: AxentraLink,
        *,
        mode: Mode,
        session_lifetime: timedelta = timedelta(hours=12),
        idle_timeout: timedelta = timedelta(hours=2),
        login_lifetime: timedelta = timedelta(minutes=10),
    ) -> None:
        if mode not in ("mirror", "axentra"):
            raise ValueError(f"unknown identity mode {mode!r}")
        super().__init__(
            database, session_lifetime=session_lifetime, idle_timeout=idle_timeout, login_lifetime=login_lifetime
        )
        self.link = link
        self.mode: Mode = mode
        self.platform = link.services.platform

    def __repr__(self) -> str:
        return f"AxentraIdentityStore(mode={self.mode!r})"

    @property
    def product_id(self) -> str:
        return self.link.services.product_id

    # ---- start-up --------------------------------------------------------------------------------------

    def configuration_problems(self) -> list[str]:
        """Settings that would make the two sides disagree (idle limit; Axentra resolves sessions in ``axentra``)."""
        idle = timedelta(minutes=int(self.platform.settings.security.session_idle_minutes))
        if idle != self.idle_timeout:
            return [
                f"Axentra's session idle limit ({idle}) differs from Nodestro's ({self.idle_timeout}); "
                "set security.session_idle_minutes in NODESTRO_AXENTRA_CONFIG to match"
            ]
        return []

    def prepare(self) -> None:
        """Check the settings and apply Nodestro's access definitions (versioned, idempotent); ``axentra`` mode also
        requires the backfill (:func:`identity_readiness`), so a server never answers from an Axentra side that is
        missing records."""
        problems = self.configuration_problems()
        if problems:
            raise NodestroError("profile.invalid", "The server cannot start: " + "; ".join(problems), problems=problems)
        self.apply_access()
        missing = identity_readiness(self)
        if missing and self.mode == "axentra":
            raise NodestroError(
                "identity.backfill_required",
                "NODESTRO_IDENTITY_BACKEND=axentra needs every identity record on both sides; run "
                "'nodestro-server axentra backfill-identity' and 'nodestro-server axentra reconcile' first ("
                + "; ".join(missing)
                + ")",
                problems=missing,
            )
        if missing:
            logger.warning(
                "Identity mirror: records are not on both sides yet (%s); run the backfill", "; ".join(missing)
            )

    def apply_access(self) -> Any:
        """Apply :func:`access_definitions` to product ``nodestro`` (:meth:`axentra.access.definitions.
        AccessDefinitionService.apply`): applied once per revision, a no-op when equal, never downgraded by an older
        process (``stale``, logged). The same revision with other content stops the start
        (``identity.access_conflict``: two builds disagree about one revision)."""
        from axentra.core.errors import Conflict

        try:
            result = self.platform.access_definitions.apply(
                self.product_id, access_definitions(), applied_by=f"nodestro-runtime revision {ACCESS_REVISION}"
            )
        except Conflict as exc:
            raise NodestroError(
                "identity.access_conflict",
                f"Axentra holds other access definitions for revision {ACCESS_REVISION}: {exc}",
                revision=ACCESS_REVISION,
            ) from None
        if result.outcome == "applied":
            logger.info("Access definitions revision %s applied (%s)", ACCESS_REVISION, result.as_dict())
        elif result.outcome == "stale":
            logger.warning(
                "Axentra holds access definitions revision %s, newer than this process's %s; they stay",
                result.stored_revision,
                ACCESS_REVISION,
            )
        return result

    def ensure_roles(self) -> list[str]:
        """Apply the access definitions (:meth:`apply_access`); returns the custom role keys created."""
        return list(self.apply_access().roles["created"])

    # ---- helpers ----------------------------------------------------------------------------------------

    @contextmanager
    def _ax(self, *, read_only: bool = False) -> Iterator[Any]:
        """An Axentra transaction in platform scope (joined to the Nodestro transaction when one is joined)."""
        with self.platform.db.transaction(_platform_scope(), read_only=read_only) as tx:
            yield tx

    def _bridge(self, exc: Exception, **params: Any) -> Exception:
        """A Nodestro error for an Axentra refusal that has a Nodestro meaning; anything else stays as it is."""
        from axentra.core.errors import Conflict

        if isinstance(exc, Conflict) and "owner" in str(exc):
            return NodestroError("identity.last_owner", "A tenant needs at least one owner", **params)
        return exc

    def _tenant_link(self, tenant_id: str) -> str:
        """The Axentra tenant of a Nodestro tenant, created and linked when missing (inside the caller's
        transaction)."""
        known = self.link.tenant(tenant_id)
        if known is not None:
            return known
        rows = self.database._query("SELECT name FROM nodestro_tenants WHERE id = %s", (tenant_id,))
        if not rows:
            raise NodestroError("identity.tenant_unknown", f"No tenant {tenant_id}", tenant=tenant_id)
        return self.link.ensure_tenant(tenant_id, axentra_tenant_name(tenant_id, str(rows[0][0])))

    def _email_for(self, tx: Any, email: str | None, user_id: str | None) -> str | None:
        """The address Axentra may store for a user: Nodestro's (verified) address, normalized, unless another
        Axentra user has it (then ``None``: never linked, never merged by e-mail) or Axentra refuses its form."""
        if not email:
            return None
        from axentra.core.errors import ValidationFailed
        from axentra.identity.service import normalize_email

        try:
            normalized = normalize_email(email)
        except ValidationFailed:
            return None
        # Serializes the check-then-write of one address across processes (unique index on lower(email)).
        tx.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (LINK_LOCK_CLASS, "email:" + normalized.lower()))
        taken = tx.one(
            "SELECT 1 FROM axentra_users WHERE lower(email) = lower(%s) AND id IS DISTINCT FROM %s",
            (normalized, user_id),
        )
        return None if taken else normalized

    def _ensure_user(self, connection: Any, user_id: str, *, sync: bool = False) -> tuple[str, str]:
        """The Axentra user of a Nodestro user, created (or linked by its identity) when missing; ``sync`` also
        writes Nodestro's name and address to an existing one. Returns ``(axentra id, how)`` with ``how``
        ``linked``, ``created`` or ``identity``. Runs inside the caller's joined transaction."""
        row = connection.execute(
            "SELECT issuer, subject, email, name FROM nodestro_users WHERE id = %s", (user_id,)
        ).fetchone()
        if row is None:
            raise NodestroError("identity.user_unknown", f"No user {user_id}", user=user_id)
        issuer, subject, email, name = row
        found = connection.execute(
            "SELECT axentra_id FROM nodestro_axentra_ids WHERE kind = 'user' AND nodestro_id = %s", (user_id,)
        ).fetchone()
        how = "linked"
        if found is not None:
            axentra_id = str(found[0])
        else:
            with self._ax() as tx:
                known = tx.one(
                    "SELECT user_id FROM axentra_user_identities WHERE provider = %s AND subject = %s",
                    (issuer, subject),
                )
            if known is not None:
                # The identity (issuer, subject) is the key on both sides; an Axentra user that already has it is
                # this person, unless it belongs to another Nodestro user (which would be a corrupt link).
                axentra_id = str(known["user_id"])
                other = connection.execute(
                    "SELECT nodestro_id FROM nodestro_axentra_ids WHERE kind = 'user' AND axentra_id = %s",
                    (axentra_id,),
                ).fetchone()
                if other is not None:
                    raise NodestroError(
                        "identity.link_conflict",
                        f"The identity of {user_id} belongs to the Axentra user of {other[0]}",
                        user=user_id,
                    )
                how = "identity"
            else:
                with self._ax() as tx:
                    address = self._email_for(tx, email, None)
                created = self.platform.identity.create_external_user(
                    _system(),
                    provider=issuer,
                    subject=subject,
                    # Without an address of its own the identity row keeps Nodestro's address for reference only.
                    email=address or email,
                    email_verified=address is not None,
                    name=axentra_name(name),
                )
                axentra_id = created.id
                how = "created"
            self.link.link(connection, "user", user_id, axentra_id)
        if sync and how != "created":
            self._sync_profile(axentra_id, email, name)
        return axentra_id, how

    def _sync_profile(self, axentra_id: str, email: str | None, name: str | None) -> bool:
        """Write Nodestro's name and address (see :meth:`_email_for`) to the Axentra user; whether the address was
        withheld because it is another user's or not acceptable to Axentra."""
        with self._ax() as tx:
            address = self._email_for(tx, email, axentra_id)
            tx.execute(
                "UPDATE axentra_users SET email = %s, name = %s, updated_at = now()"
                " WHERE id = %s AND (email IS DISTINCT FROM %s OR name IS DISTINCT FROM %s)",
                (address, axentra_name(name), axentra_id, address, axentra_name(name)),
            )
        return bool(email) and address is None

    def _revoke(self, hashes: list[bytes], *, audited: bool = True) -> int:
        """Revoke the Axentra sessions of these Nodestro session hashes (inside the caller's transaction)."""
        if not hashes:
            return 0
        keys = [item.hex() for item in hashes]
        if not audited:
            with self._ax() as tx:
                return int(
                    tx.execute(
                        "UPDATE axentra_sessions SET revoked_at = now()"
                        " WHERE token_hash = ANY(%s) AND revoked_at IS NULL",
                        (keys,),
                    )
                )
        with self._ax() as tx:
            rows = tx.all(
                "SELECT id, user_id FROM axentra_sessions WHERE token_hash = ANY(%s) AND revoked_at IS NULL", (keys,)
            )
        for row in rows:
            self.platform.identity.revoke_session(_system(), row["user_id"], row["id"])
        return len(rows)

    # ---- tenants ------------------------------------------------------------------------------------------

    def create_tenant(self, tenant_id: str, name: str) -> Tenant:
        with self.link.joined():
            tenant = super().create_tenant(tenant_id, name)
            self.link.ensure_tenant(tenant_id, axentra_tenant_name(tenant_id, tenant.name))
        return tenant

    def set_tenant_status(self, tenant_id: str, status: TenantStatus) -> Tenant:
        """Suspend or resume the tenant on both sides, in one transaction."""
        if status not in TENANT_STATUSES:
            raise NodestroError("identity.tenant_status", f"Unknown tenant status {status!r}", status=status)
        with self.link.joined():
            tenant = super().set_tenant_status(tenant_id, status)
            axentra_tenant = self._tenant_link(tenant_id)
            self.platform.tenancy.update_tenant(_system(), self.product_id, axentra_tenant, status=status)
        return tenant if self.mode == "mirror" else (self.tenant(tenant_id) or tenant)

    def tenant_active(self, tenant_id: str) -> bool:
        if self.mode == "mirror":
            return super().tenant_active(tenant_id)
        found = self.tenant(tenant_id)
        return found is not None and found.status == "active"

    def tenant(self, tenant_id: str) -> Tenant | None:
        if self.mode == "mirror":
            return super().tenant(tenant_id)
        found = self._tenants(tenant_id)
        return found[0] if found else None

    def tenants(self) -> list[Tenant]:
        if self.mode == "mirror":
            return super().tenants()
        return self._tenants(None)

    def _tenants(self, tenant_id: str | None) -> list[Tenant]:
        """Axentra's tenants of product ``nodestro`` under their Nodestro ids (name from Axentra; the creation time
        is Nodestro's, the tenant's data and files are filed under it). A tenant is suspended when either side says
        so (fail closed: a suspension made directly in Axentra counts, and resuming needs Nodestro's command)."""
        where = " AND l.nodestro_id = %s" if tenant_id is not None else ""
        params: tuple[Any, ...] = (self.product_id,) + ((tenant_id,) if tenant_id is not None else ())
        with self._ax(read_only=True) as tx:
            rows = tx.all(
                "SELECT l.nodestro_id, t.name, n.created_at,"
                " CASE WHEN t.status = 'active' AND n.status = 'active' THEN 'active' ELSE 'suspended' END AS status"
                " FROM axentra_tenants t"
                " JOIN nodestro_axentra_ids l ON l.kind = 'tenant' AND l.axentra_id = t.id"
                " JOIN nodestro_tenants n ON n.id = l.nodestro_id"
                " WHERE t.product_id = %s" + where + " ORDER BY l.nodestro_id",
                params,
            )
        return [
            Tenant(id=row["nodestro_id"], name=row["name"], created_at=row["created_at"], status=row["status"])
            for row in rows
        ]

    # ---- users and roles ------------------------------------------------------------------------------------

    def login(self, issuer: str, subject: str, *, email: str | None = None, name: str | None = None) -> User:
        with self.link.joined() as connection:
            user = super().login(issuer, subject, email=email, name=name)
            axentra_id, _how = self._ensure_user(connection, user.id, sync=True)
            with self._ax() as tx:
                tx.execute("UPDATE axentra_users SET last_login_at = now() WHERE id = %s", (axentra_id,))
        return user

    def set_role(self, tenant_id: str, user_id: str, role: Role) -> Membership:
        if role not in ROLES:
            raise NodestroError("identity.role", f"Unknown role {role!r}", role=role)
        with self.link.joined() as connection:
            membership = super().set_role(tenant_id, user_id, role)  # Nodestro's guard runs first
            axentra_tenant = self._tenant_link(tenant_id)
            axentra_user, _how = self._ensure_user(connection, user_id)
            try:
                self.platform.tenancy.set_member(
                    _system(), self.product_id, axentra_tenant, role=ROLE_TO_AXENTRA[role], user_id=axentra_user
                )
            except Exception as exc:
                raise self._bridge(exc, tenant=tenant_id) from None
        return membership

    def remove_member(self, tenant_id: str, user_id: str) -> None:
        from axentra.core.errors import NotFound

        with self.link.joined() as connection:
            hashes = [
                bytes(row[0])
                for row in connection.execute(
                    "SELECT token_hash FROM nodestro_sessions WHERE tenant_id = %s AND user_id = %s FOR UPDATE",
                    (tenant_id, user_id),
                ).fetchall()
            ]
            super().remove_member(tenant_id, user_id)  # guard, membership and the sessions in this tenant
            axentra_tenant = self.link.tenant(tenant_id)
            axentra_user = self.link.lookup("user", user_id)
            if axentra_tenant is not None and axentra_user is not None:
                try:
                    self.platform.tenancy.remove_member(_system(), self.product_id, axentra_tenant, axentra_user)
                except NotFound:
                    pass  # not a member on Axentra's side either (Nodestro's removal is a no-op then, too)
                except Exception as exc:
                    raise self._bridge(exc, tenant=tenant_id) from None
            self._revoke(hashes)

    def role(self, tenant_id: str, user_id: str) -> Role | None:
        if self.mode == "mirror":
            return super().role(tenant_id, user_id)
        found = self._memberships(tenant_id=tenant_id, user_id=user_id)
        return found[0].role if found else None

    def memberships(self, user_id: str) -> list[Membership]:
        if self.mode == "mirror":
            return super().memberships(user_id)
        return self._memberships(user_id=user_id)

    def members(self, tenant_id: str) -> list[Membership]:
        if self.mode == "mirror":
            return super().members(tenant_id)
        return self._memberships(tenant_id=tenant_id)

    def _memberships(self, *, tenant_id: str | None = None, user_id: str | None = None) -> list[Membership]:
        """Axentra's memberships in product ``nodestro`` under Nodestro's ids and role names (an Axentra role
        without a Nodestro meaning grants nothing and is left out)."""
        where, params = ["m.product_id = %s"], [self.product_id]
        if tenant_id is not None:
            where.append("lt.nodestro_id = %s")
            params.append(tenant_id)
        if user_id is not None:
            where.append("lu.nodestro_id = %s")
            params.append(user_id)
        with self._ax(read_only=True) as tx:
            rows = tx.all(
                "SELECT lt.nodestro_id AS tenant_id, lu.nodestro_id AS user_id, m.role,"
                " CASE WHEN t.status = 'active' AND n.status = 'active' THEN 'active' ELSE 'suspended' END AS status"
                " FROM axentra_memberships m"
                " JOIN nodestro_axentra_ids lt ON lt.kind = 'tenant' AND lt.axentra_id = m.tenant_id"
                " JOIN nodestro_axentra_ids lu ON lu.kind = 'user' AND lu.axentra_id = m.user_id"
                " JOIN axentra_users u ON u.id = m.user_id AND u.status = 'active'"
                " JOIN axentra_tenants t ON t.id = m.tenant_id"
                " JOIN nodestro_tenants n ON n.id = lt.nodestro_id"
                " WHERE " + " AND ".join(where) + " ORDER BY lt.nodestro_id, lu.nodestro_id",
                params,
            )
        return [
            Membership(
                tenant_id=row["tenant_id"],
                user_id=row["user_id"],
                role=ROLE_FROM_AXENTRA[row["role"]],
                tenant_status=row["status"],
            )
            for row in rows
            if row["role"] in ROLE_FROM_AXENTRA
        ]

    # ---- sessions ---------------------------------------------------------------------------------------------

    def create_session(self, user_id: str, tenant_id: str) -> str:
        if self.role(tenant_id, user_id) is None:
            raise NodestroError("identity.not_a_member", "This account has no role in the tenant", tenant=tenant_id)
        if not self.tenant_active(tenant_id):
            raise tenant_suspended(tenant_id)
        with self.link.joined() as connection:
            axentra_user, _how = self._ensure_user(connection, user_id)
            started = self.platform.identity.start_session(
                axentra_user, SESSION_METHOD, ip=None, user_agent=SESSION_AGENT
            )
            token = str(started.token)
            expires_at = _now() + self.session_lifetime  # Nodestro's lifetime on both sides, one value
            with self._ax() as tx:
                tx.execute(
                    "UPDATE axentra_sessions SET expires_at = %s WHERE id = %s", (expires_at, started.info.session_id)
                )
            connection.execute(
                "INSERT INTO nodestro_sessions (token_hash, user_id, tenant_id, expires_at) VALUES (%s, %s, %s, %s)",
                (_hash(token), user_id, tenant_id, expires_at),
            )
        return token

    def session(self, token: str) -> Session | None:
        if self.mode == "mirror":
            return super().session(token)  # Nodestro's tables; the touch is mirrored by _touch
        if not token or len(token) > 256:
            return None
        from axentra.core.errors import AxentraError

        try:
            _principal, info = self.platform.identity.resolve_session(token)
        except AxentraError:
            return None
        from axentra.access.product_permissions import load_catalog

        token_hash = _hash(token)
        with self._ax(read_only=True) as tx:
            row = tx.one(
                "SELECT s.user_id, s.tenant_id, m.role, r.permissions AS custom,"
                " t.status = 'active' AND n.status = 'active' AS active,"
                " s.last_seen_at < clock_timestamp() - interval '1 minute' AS stale"
                " FROM nodestro_sessions s"
                " JOIN nodestro_axentra_ids lu ON lu.kind = 'user' AND lu.nodestro_id = s.user_id"
                " JOIN nodestro_axentra_ids lt ON lt.kind = 'tenant' AND lt.nodestro_id = s.tenant_id"
                " JOIN axentra_memberships m ON m.tenant_id = lt.axentra_id AND m.user_id = lu.axentra_id"
                " JOIN axentra_tenants t ON t.id = lt.axentra_id"
                " JOIN nodestro_tenants n ON n.id = s.tenant_id"
                " LEFT JOIN axentra_roles r ON r.product_id = m.product_id AND r.key = m.role"
                " WHERE s.token_hash = %s AND lu.axentra_id = %s AND m.product_id = %s",
                (token_hash, info.user.id, self.product_id),
            )
            if row is None or row["role"] not in ROLE_FROM_AXENTRA:
                return None
            if not row["active"]:
                raise tenant_suspended(row["tenant_id"])
            # Axentra's role definitions decide Nodestro's permissions in this mode (read in this request: a change
            # to a role or its definitions applies to the next request).
            catalog = load_catalog(tx, self.product_id)
            custom = {row["role"]: frozenset(row["custom"])} if row["custom"] is not None else {}
            granted = catalog.role_permissions(row["role"], custom) & catalog.tenant_mask
        if row["stale"]:
            self._touch(token_hash)
        return Session(
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            role=ROLE_FROM_AXENTRA[row["role"]],
            expires_at=info.expires_at,
            permissions=tuple(
                sorted(PERMISSION_FROM_AXENTRA[name] for name in granted if name in PERMISSION_FROM_AXENTRA)
            ),
        )

    def _touch(self, token_hash: bytes) -> None:
        with self.link.joined() as connection:
            connection.execute(
                "UPDATE nodestro_sessions SET last_seen_at = clock_timestamp() WHERE token_hash = %s", (token_hash,)
            )
            with self._ax() as tx:
                tx.execute(
                    "UPDATE axentra_sessions SET last_seen_at = now() WHERE token_hash = %s AND revoked_at IS NULL",
                    (token_hash.hex(),),
                )

    def end_session(self, token: str) -> None:
        with self.link.joined():
            super().end_session(token)
            self._revoke([_hash(token)])

    def end_sessions(self, user_id: str) -> int:
        with self.link.joined():
            ended = super().end_sessions(user_id)
            axentra_user = self.link.lookup("user", user_id)
            revoked = (
                self.platform.identity.revoke_all_sessions(_system(), axentra_user) if axentra_user is not None else 0
            )
        return ended if self.mode == "mirror" else int(revoked)

    # ---- bootstrap and housekeeping ------------------------------------------------------------------------------

    def use_bootstrap(self, token_hash: bytes, user_id: str) -> str:
        with self.link.joined() as connection:
            tenant_id = super().use_bootstrap(token_hash, user_id)
            if self.mode == "axentra" and not self.tenant_active(tenant_id):
                raise tenant_suspended(tenant_id)  # rolled back with the use of the link
            axentra_tenant = self._tenant_link(tenant_id)
            axentra_user, _how = self._ensure_user(connection, user_id)
            try:
                self.platform.tenancy.set_member(
                    _system(), self.product_id, axentra_tenant, role="tenant_owner", user_id=axentra_user
                )
            except Exception as exc:
                raise self._bridge(exc, tenant=tenant_id) from None
        return tenant_id

    def cleanup(self) -> int:
        """Delete expired and idle sessions and expired login states (Nodestro), revoke the same sessions and purge
        expired records on Axentra's side, in one transaction."""
        with self.link.joined() as connection:
            hashes = [
                bytes(row[0])
                for row in connection.execute(
                    "DELETE FROM nodestro_sessions WHERE expires_at <= clock_timestamp()"
                    " OR last_seen_at <= clock_timestamp() - %s RETURNING token_hash",
                    (self.idle_timeout,),
                ).fetchall()
            ]
            states = connection.execute(
                "DELETE FROM nodestro_login_states WHERE expires_at <= clock_timestamp()"
            ).rowcount
            self._revoke(hashes, audited=False)
            self.platform.identity.purge_expired()
        return len(hashes) + int(states)


# ---- choosing the store ----------------------------------------------------------------------------------------


def open_identity_store(
    database: PostgresDatabase, config: AxentraConfig, services: AxentraServices | None
) -> IdentityStore:
    """The identity store of a server process for ``config.identity_backend`` (``services`` started; needed for
    ``mirror`` and ``axentra``). ``axentra`` refuses to start before the backfill (``identity.backfill_required``)."""
    backend = config.identity_backend
    if backend == "nodestro":
        return IdentityStore(database)
    if services is None or not config.enabled:
        raise NodestroError(
            "profile.invalid", f"NODESTRO_IDENTITY_BACKEND={backend} needs NODESTRO_AXENTRA=on", backend=backend
        )
    store = AxentraIdentityStore(database, AxentraLink(services, database), mode=backend)
    store.prepare()
    logger.info("Identity backend: %s", backend)
    return store


# ---- readiness, backfill and reconciliation -----------------------------------------------------------------------


def identity_readiness(store: AxentraIdentityStore) -> list[str]:
    """Quick check (one statement, counts only) that every identity record is on both sides: unlinked tenants and
    users, membership and live session counts, the custom roles. Empty: ready for ``axentra`` mode."""
    with store._ax(read_only=True) as tx:
        row = tx.one(
            "SELECT"
            " (SELECT count(*) FROM nodestro_tenants n WHERE NOT EXISTS (SELECT 1 FROM nodestro_axentra_ids l"
            "   JOIN axentra_tenants t ON t.id = l.axentra_id WHERE l.kind = 'tenant' AND l.nodestro_id = n.id))"
            "   AS tenants,"
            " (SELECT count(*) FROM nodestro_users n WHERE NOT EXISTS (SELECT 1 FROM nodestro_axentra_ids l"
            "   JOIN axentra_users u ON u.id = l.axentra_id WHERE l.kind = 'user' AND l.nodestro_id = n.id)) AS users,"
            " (SELECT count(*) FROM nodestro_memberships) AS n_members,"
            " (SELECT count(*) FROM axentra_memberships WHERE product_id = %s) AS a_members,"
            " (SELECT count(*) FROM nodestro_sessions WHERE expires_at > now()) AS n_sessions,"
            " (SELECT count(*) FROM nodestro_sessions s JOIN axentra_sessions a"
            "   ON a.token_hash = encode(s.token_hash, 'hex') AND a.revoked_at IS NULL"
            "   WHERE s.expires_at > now()) AS both_sessions,"
            " (SELECT count(*) FROM axentra_roles WHERE product_id = %s AND key = ANY(%s)) AS roles",
            (store.product_id, store.product_id, list(CUSTOM_ROLES)),
        )
    assert row is not None
    problems = []
    if row["tenants"]:
        problems.append(f"{row['tenants']} tenants not linked")
    if row["users"]:
        problems.append(f"{row['users']} users not linked")
    if row["n_members"] != row["a_members"]:
        problems.append(f"memberships: {row['n_members']} in Nodestro, {row['a_members']} in Axentra")
    if row["n_sessions"] != row["both_sessions"]:
        problems.append(f"{row['n_sessions'] - row['both_sessions']} live sessions missing in Axentra")
    if row["roles"] != len(CUSTOM_ROLES):
        problems.append("custom roles missing")
    return problems


@dataclass
class BackfillReport:
    """What :func:`backfill_identity` did (or, with ``dry_run``, would do); ids and counts only."""

    dry_run: bool
    roles_created: list[str] = field(default_factory=list)
    tenants: dict[str, int] = field(default_factory=lambda: {"total": 0, "created": 0, "renamed": 0, "suspended": 0})
    #: Tenants suspended in Axentra but active in Nodestro: kept suspended (never resumed by a backfill).
    suspended_in_axentra_only: list[str] = field(default_factory=list)
    #: Nodestro tenant id -> Axentra slug, for every tenant whose slug differs from its id.
    slug_changes: dict[str, str] = field(default_factory=dict)
    users: dict[str, int] = field(default_factory=lambda: {"total": 0, "created": 0, "by_identity": 0})
    #: Users stored without an e-mail address in Axentra (another user has it, or Axentra refuses its form).
    users_without_email: list[str] = field(default_factory=list)
    memberships: dict[str, int] = field(default_factory=lambda: {"total": 0, "added": 0, "changed": 0, "removed": 0})
    sessions: dict[str, int] = field(
        default_factory=lambda: {"live": 0, "copied": 0, "updated": 0, "revoked_in_axentra": 0, "ended": 0}
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "roles_created": list(self.roles_created),
            "tenants": dict(self.tenants),
            "slug_changes": dict(sorted(self.slug_changes.items())),
            "suspended_in_axentra_only": sorted(self.suspended_in_axentra_only),
            "users": dict(self.users),
            "users_without_email": sorted(self.users_without_email),
            "memberships": dict(self.memberships),
            "sessions": dict(self.sessions),
        }


class _DryRun(Exception):
    pass


def backfill_identity(store: AxentraIdentityStore, *, dry_run: bool = False, batch: int = 500) -> BackfillReport:
    """Copy Nodestro's identity records into Axentra (``nodestro-server axentra backfill-identity``).

    Nodestro's tables are the source. Idempotent and resumable: every tenant, user, tenant's memberships and batch
    of sessions is one transaction with the Nodestro rows locked, so a crash loses nothing and a rerun continues;
    records already equal are not written. Memberships are copied as they are (Nodestro's state satisfied its own
    last-owner guard; Axentra's guard would refuse repairing a drift such as a stale Axentra owner of a tenant that
    waits for its setup link). Axentra-only memberships and live sessions in product ``nodestro`` are removed or
    revoked; a session revoked in Axentra but live in Nodestro is ended on both sides (never revived). ``dry_run``
    runs everything in one transaction and rolls it back. Run it in a maintenance window, or while every process
    runs in ``mirror`` mode."""
    report = BackfillReport(dry_run=dry_run)
    if not dry_run:
        _backfill(store, report, batch, unit=store.link.joined)
        return report
    try:
        with store.link.joined():
            _backfill(store, report, batch, unit=store.link.joined)
            raise _DryRun
    except _DryRun:
        pass
    return report


def _backfill(store: AxentraIdentityStore, report: BackfillReport, batch: int, *, unit: Any) -> None:
    database = store.database
    with unit():
        report.roles_created = store.ensure_roles()
    # Tenants.
    for (tenant_id,) in database._query("SELECT id FROM nodestro_tenants ORDER BY id"):
        report.tenants["total"] += 1
        with unit() as connection:
            row = connection.execute(
                "SELECT name, status FROM nodestro_tenants WHERE id = %s FOR SHARE", (tenant_id,)
            ).fetchone()
            if row is None:
                continue
            name = axentra_tenant_name(tenant_id, str(row[0]))
            if store.link.tenant(tenant_id) is None:
                store.link.ensure_tenant(tenant_id, name)
                report.tenants["created"] += 1
            axentra_tenant = store.link.tenant(tenant_id)
            assert axentra_tenant is not None
            with store._ax() as tx:
                current = tx.one("SELECT slug, name, status FROM axentra_tenants WHERE id = %s", (axentra_tenant,))
            if current is None:
                raise NodestroError(
                    "identity.link_conflict", f"Tenant {tenant_id} is linked to a missing Axentra tenant"
                )
            if current["name"] != name:
                store.platform.tenancy.update_tenant(_system(), store.product_id, axentra_tenant, name=name)
                report.tenants["renamed"] += 1
            # A suspension is copied to Axentra, never undone by a backfill (fail closed).
            if row[1] == "suspended" and current["status"] != "suspended":
                store.platform.tenancy.update_tenant(_system(), store.product_id, axentra_tenant, status="suspended")
                report.tenants["suspended"] += 1
            elif row[1] == "active" and current["status"] == "suspended":
                report.suspended_in_axentra_only.append(tenant_id)
            if current["slug"] != tenant_id:
                report.slug_changes[tenant_id] = str(current["slug"])
    # Users.
    # Oldest first: when two people share an address, the account that had it first keeps it on Axentra's side.
    for (user_id,) in database._query("SELECT id FROM nodestro_users ORDER BY created_at, id"):
        report.users["total"] += 1
        with unit() as connection:
            row = connection.execute(
                "SELECT email, name FROM nodestro_users WHERE id = %s FOR UPDATE", (user_id,)
            ).fetchone()
            if row is None:
                continue
            axentra_user, how = store._ensure_user(connection, user_id)
            if how == "created":
                report.users["created"] += 1
            elif how == "identity":
                report.users["by_identity"] += 1
            if store._sync_profile(axentra_user, row[0], row[1]):
                report.users_without_email.append(user_id)
    # Memberships, one tenant at a time (the tenant row lock serializes with set_role/remove_member).
    for (tenant_id,) in database._query("SELECT id FROM nodestro_tenants ORDER BY id"):
        with unit() as connection:
            if (
                connection.execute("SELECT 1 FROM nodestro_tenants WHERE id = %s FOR UPDATE", (tenant_id,)).fetchone()
                is None
            ):
                continue
            _copy_memberships(store, connection, tenant_id, report)
    # Sessions, in batches (only live ones; expired ones end anyway).
    after = b""
    while True:
        with unit() as connection:
            rows = connection.execute(
                "SELECT token_hash, user_id, created_at, last_seen_at, expires_at FROM nodestro_sessions"
                " WHERE expires_at > clock_timestamp() AND token_hash > %s ORDER BY token_hash LIMIT %s FOR UPDATE",
                (after, batch),
            ).fetchall()
            if not rows:
                break
            after = bytes(rows[-1][0])
            _copy_sessions(store, connection, rows, report)
    with unit():
        with store._ax() as tx:
            stray = tx.all(
                "SELECT a.id FROM axentra_sessions a WHERE a.revoked_at IS NULL AND a.expires_at > now()"
                " AND a.auth_method = %s AND a.user_agent = %s AND NOT EXISTS (SELECT 1 FROM nodestro_sessions s"
                " WHERE encode(s.token_hash, 'hex') = a.token_hash)",
                (SESSION_METHOD, SESSION_AGENT),
            )
            if stray:
                tx.execute(
                    "UPDATE axentra_sessions SET revoked_at = now() WHERE id = ANY(%s)", ([row["id"] for row in stray],)
                )
        report.sessions["revoked_in_axentra"] += len(stray)


def _copy_memberships(store: AxentraIdentityStore, connection: Any, tenant_id: str, report: BackfillReport) -> None:
    from axentra.core.ids import new_id
    from axentra.data.database import DbScope

    wanted = {
        str(user_id): ROLE_TO_AXENTRA[str(role)]
        for user_id, role in connection.execute(
            "SELECT user_id, role FROM nodestro_memberships WHERE tenant_id = %s", (tenant_id,)
        ).fetchall()
    }
    report.memberships["total"] += len(wanted)
    axentra_tenant = store._tenant_link(tenant_id)
    users = {user_id: store._ensure_user(connection, user_id)[0] for user_id in wanted}
    product = store.product_id
    with store.platform.db.transaction(DbScope.tenant(product, axentra_tenant)) as tx:
        current = {
            row["user_id"]: row["role"]
            for row in tx.all(
                "SELECT user_id, role FROM axentra_memberships WHERE product_id = %s AND tenant_id = %s",
                (product, axentra_tenant),
            )
        }
        for user_id, role in sorted(wanted.items()):
            axentra_user = users[user_id]
            before = current.pop(axentra_user, None)
            if before == role:
                continue
            tx.execute(
                "INSERT INTO axentra_memberships (id, product_id, tenant_id, user_id, role) VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role",
                (new_id("mem"), product, axentra_tenant, axentra_user, role),
            )
            store.platform.audit.record(
                tx,
                _system(),
                "tenancy.member.set",
                target_type="user",
                target_id=axentra_user,
                product_id=product,
                tenant_id=axentra_tenant,
                details={"role": role, "source": "nodestro.backfill"},
            )
            report.memberships["added" if before is None else "changed"] += 1
        for axentra_user in sorted(current):  # on Axentra's side only
            tx.execute(
                "DELETE FROM axentra_memberships WHERE product_id = %s AND tenant_id = %s AND user_id = %s",
                (product, axentra_tenant, axentra_user),
            )
            store.platform.audit.record(
                tx,
                _system(),
                "tenancy.member.remove",
                target_type="user",
                target_id=axentra_user,
                product_id=product,
                tenant_id=axentra_tenant,
                details={"source": "nodestro.backfill"},
            )
            report.memberships["removed"] += 1


def _copy_sessions(store: AxentraIdentityStore, connection: Any, rows: list[Any], report: BackfillReport) -> None:
    from axentra.core.ids import new_id, new_token

    keys = [bytes(row[0]).hex() for row in rows]
    with store._ax() as tx:
        existing = {
            row["token_hash"]: row
            for row in tx.all(
                "SELECT id, token_hash, user_id, revoked_at FROM axentra_sessions WHERE token_hash = ANY(%s)", (keys,)
            )
        }
    ended: list[bytes] = []
    for token_hash, user_id, created_at, last_seen_at, expires_at in rows:
        token_hash = bytes(token_hash)
        report.sessions["live"] += 1
        axentra_user, _how = store._ensure_user(connection, str(user_id))
        found = existing.get(token_hash.hex())
        if found is not None and (found["revoked_at"] is not None or found["user_id"] != axentra_user):
            ended.append(token_hash)  # revoked (or another user's) on Axentra's side: never revived, ended here
            continue
        with store._ax() as tx:
            if found is None:
                tx.execute(
                    "INSERT INTO axentra_sessions (id, user_id, token_hash, csrf_token, auth_method, ip, user_agent,"
                    " created_at, last_seen_at, expires_at) VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s, %s)",
                    (
                        new_id("ses"),
                        axentra_user,
                        token_hash.hex(),
                        new_token(24),
                        SESSION_METHOD,
                        SESSION_AGENT,
                        created_at,
                        last_seen_at,
                        expires_at,
                    ),
                )
                report.sessions["copied"] += 1
            elif tx.execute(
                "UPDATE axentra_sessions SET expires_at = %s, last_seen_at = greatest(last_seen_at, %s)"
                " WHERE id = %s AND (expires_at IS DISTINCT FROM %s OR last_seen_at < %s)",
                (expires_at, last_seen_at, found["id"], expires_at, last_seen_at),
            ):
                report.sessions["updated"] += 1
    if ended:
        connection.execute("DELETE FROM nodestro_sessions WHERE token_hash = ANY(%s)", (ended,))
        store._revoke(ended, audited=False)
        report.sessions["ended"] += len(ended)


@dataclass
class ReconcileReport:
    """Differences between Nodestro's and Axentra's identity records (ids and counts only; never tokens or hashes).
    ``ok`` when there are none."""

    counts: dict[str, int] = field(default_factory=dict)
    differences: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(self.differences.values())

    def add(self, kind: str, item: str) -> None:
        self.differences.setdefault(kind, []).append(item)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "counts": dict(self.counts),
            "differences": {key: sorted(value) for key, value in sorted(self.differences.items()) if value},
        }


def reconcile_identity(store: AxentraIdentityStore) -> ReconcileReport:
    """Compare tenants, users, memberships with roles, live sessions and the custom roles of both sides
    (``nodestro-server axentra reconcile``), in one repeatable-read snapshot. Read-only."""
    from axentra.identity.service import normalize_email

    report = ReconcileReport()
    product = store.product_id
    database = store.database
    with database._transaction() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        with store.platform.db.joined(connection), store._ax(read_only=True) as tx:
            tenants = tx.all(
                "SELECT n.id, n.name, n.status, l.axentra_id, t.id AS found, t.name AS ax_name, t.product_id,"
                " t.status AS ax_status"
                " FROM nodestro_tenants n"
                " LEFT JOIN nodestro_axentra_ids l ON l.kind = 'tenant' AND l.nodestro_id = n.id"
                " LEFT JOIN axentra_tenants t ON t.id = l.axentra_id ORDER BY n.id"
            )
            extra_tenants = tx.all(
                "SELECT t.id FROM axentra_tenants t WHERE t.product_id = %s AND NOT EXISTS"
                " (SELECT 1 FROM nodestro_axentra_ids l WHERE l.kind = 'tenant' AND l.axentra_id = t.id)",
                (product,),
            )
            users = tx.all(
                "SELECT n.id, n.issuer, n.subject, n.email, n.name, l.axentra_id, u.id AS found, u.email AS ax_email,"
                " u.name AS ax_name, u.status, EXISTS (SELECT 1 FROM axentra_user_identities i WHERE i.user_id = u.id"
                " AND i.provider = n.issuer AND i.subject = n.subject) AS identity"
                " FROM nodestro_users n LEFT JOIN nodestro_axentra_ids l ON l.kind = 'user' AND l.nodestro_id = n.id"
                " LEFT JOIN axentra_users u ON u.id = l.axentra_id ORDER BY n.id"
            )
            addresses = {
                str(row["email"]).lower(): str(row["id"])
                for row in tx.all("SELECT id, email FROM axentra_users WHERE email IS NOT NULL")
            }
            nodestro_members = {
                (row["tenant_id"], row["user_id"]): row["role"]
                for row in tx.all("SELECT tenant_id, user_id, role FROM nodestro_memberships")
            }
            axentra_members = tx.all(
                "SELECT lt.nodestro_id AS tenant_id, lu.nodestro_id AS user_id, m.role FROM axentra_memberships m"
                " LEFT JOIN nodestro_axentra_ids lt ON lt.kind = 'tenant' AND lt.axentra_id = m.tenant_id"
                " LEFT JOIN nodestro_axentra_ids lu ON lu.kind = 'user' AND lu.axentra_id = m.user_id"
                " WHERE m.product_id = %s",
                (product,),
            )
            sessions = tx.one(
                "SELECT"
                " (SELECT count(*) FROM nodestro_sessions WHERE expires_at > now()) AS nodestro,"
                " (SELECT count(*) FROM axentra_sessions WHERE revoked_at IS NULL AND expires_at > now()) AS axentra",
            )
            session_problems = tx.all(
                "SELECT s.user_id, CASE WHEN a.id IS NULL THEN 'missing in Axentra'"
                " WHEN a.revoked_at IS NOT NULL THEN 'revoked in Axentra'"
                " WHEN l.axentra_id IS DISTINCT FROM a.user_id THEN 'another user in Axentra'"
                " ELSE 'different expiry' END AS problem"
                " FROM nodestro_sessions s"
                " LEFT JOIN axentra_sessions a ON a.token_hash = encode(s.token_hash, 'hex')"
                " LEFT JOIN nodestro_axentra_ids l ON l.kind = 'user' AND l.nodestro_id = s.user_id"
                " WHERE s.expires_at > now() AND (a.id IS NULL OR a.revoked_at IS NOT NULL"
                " OR l.axentra_id IS DISTINCT FROM a.user_id"
                " OR abs(extract(epoch FROM a.expires_at - s.expires_at)) > 1)"
            )
            axentra_only_sessions = tx.all(
                "SELECT coalesce(l.nodestro_id, a.user_id) AS user_id FROM axentra_sessions a"
                " LEFT JOIN nodestro_axentra_ids l ON l.kind = 'user' AND l.axentra_id = a.user_id"
                " WHERE a.revoked_at IS NULL AND a.expires_at > now() AND NOT EXISTS"
                " (SELECT 1 FROM nodestro_sessions s WHERE encode(s.token_hash, 'hex') = a.token_hash)"
            )
    report.counts = {
        "tenants": len(tenants),
        "users": len(users),
        "memberships_nodestro": len(nodestro_members),
        "memberships_axentra": len(axentra_members),
        "sessions_nodestro": int(sessions["nodestro"]) if sessions else 0,
        "sessions_axentra": int(sessions["axentra"]) if sessions else 0,
    }
    for row in tenants:
        if row["axentra_id"] is None:
            report.add("tenants", f"{row['id']}: not linked")
        elif row["found"] is None:
            report.add("tenants", f"{row['id']}: linked Axentra tenant missing")
        elif row["product_id"] != product:
            report.add("tenants", f"{row['id']}: Axentra tenant in another product")
        else:
            if row["ax_name"] != axentra_tenant_name(row["id"], row["name"]):
                report.add("tenants", f"{row['id']}: name differs")
            if row["status"] != row["ax_status"]:
                report.add("tenants", f"{row['id']}: {row['status']} in Nodestro, {row['ax_status']} in Axentra")
    for row in extra_tenants:
        report.add("tenants", f"{row['id']}: Axentra tenant without a Nodestro tenant")
    for row in users:
        user_id = row["id"]
        if row["axentra_id"] is None:
            report.add("users", f"{user_id}: not linked")
            continue
        if row["found"] is None:
            report.add("users", f"{user_id}: linked Axentra user missing")
            continue
        if not row["identity"]:
            report.add("users", f"{user_id}: identity (issuer, subject) missing in Axentra")
        if row["status"] != "active":
            report.add("users", f"{user_id}: disabled in Axentra")
        if row["ax_name"] != axentra_name(row["name"]):
            report.add("users", f"{user_id}: name differs")
        expected: str | None = None
        if row["email"]:
            try:
                expected = normalize_email(row["email"])
            except Exception:
                expected = None
        if row["ax_email"] != expected and not (
            row["ax_email"] is None
            and (expected is None or addresses.get(expected.lower(), row["found"]) != row["found"])
        ):
            report.add("users", f"{user_id}: e-mail address differs")
    seen: set[tuple[str, str]] = set()
    for row in axentra_members:
        if row["tenant_id"] is None or row["user_id"] is None:
            report.add("memberships", "an Axentra membership of an unlinked tenant or user")
            continue
        key = (row["tenant_id"], row["user_id"])
        seen.add(key)
        wanted = nodestro_members.get(key)
        if wanted is None:
            report.add("memberships", f"{key[0]}/{key[1]}: only in Axentra ({row['role']})")
        elif ROLE_TO_AXENTRA[wanted] != row["role"]:
            report.add("memberships", f"{key[0]}/{key[1]}: role {wanted} in Nodestro, {row['role']} in Axentra")
    for key, role in nodestro_members.items():
        if key not in seen:
            report.add("memberships", f"{key[0]}/{key[1]}: only in Nodestro ({role})")
    for row in session_problems:
        report.add("sessions", f"a session of {row['user_id']}: {row['problem']}")
    for row in axentra_only_sessions:
        report.add("sessions", f"a session of {row['user_id']}: only in Axentra")
    for problem in store.platform.access_definitions.drift(product, access_definitions()):
        report.add("roles", problem)
    return report


__all__ = [
    "ACCESS_REVISION",
    "CUSTOM_ROLES",
    "PERMISSION_FROM_AXENTRA",
    "PRODUCT_PERMISSIONS",
    "ROLE_FROM_AXENTRA",
    "ROLE_TO_AXENTRA",
    "AxentraIdentityStore",
    "BackfillReport",
    "ReconcileReport",
    "access_definitions",
    "backfill_identity",
    "identity_readiness",
    "open_identity_store",
    "reconcile_identity",
]
