"""Identity on Axentra (``NODESTRO_IDENTITY_BACKEND``): mirror atomicity, backfill, reconciliation, switching modes.

The store contract itself runs for every backend in ``test_identity_store.py``. Needs ``NODESTRO_TEST_POSTGRES`` and
the ``axentra`` package (Python 3.11+).
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from typing import Any

import postgres_support
import pytest

from nodestro_runtime.identity_store import IdentityStore
from nodestro_runtime.problems import NodestroError

pytest.importorskip("psycopg")
pytest.importorskip("axentra", reason="Axentra needs Python 3.11+")

from nodestro_runtime.axentra_identity import (
    CUSTOM_ROLES,
    PRODUCT_PERMISSIONS,
    ROLE_TO_AXENTRA,
    AxentraIdentityStore,
    backfill_identity,
    identity_readiness,
    open_identity_store,
    reconcile_identity,
)
from nodestro_runtime.axentra_platform import AxentraConfig, AxentraLink
from nodestro_runtime.storage_postgres import PostgresDatabase

ISSUER = "https://login.example"
OTHER = "https://other-login.example"


@pytest.fixture
def database() -> Iterator[PostgresDatabase]:
    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    opened.test_schema = schema  # type: ignore[attr-defined]
    yield opened
    opened.close()
    postgres_support.drop_schema(schema)


@pytest.fixture
def services(database: PostgresDatabase) -> Iterator[Any]:
    from identity_support import axentra_services

    opened = axentra_services(database)
    yield opened
    opened.close()


def _store(database: PostgresDatabase, services: Any, mode: str) -> AxentraIdentityStore:
    store = AxentraIdentityStore(database, AxentraLink(services, database), mode=mode)  # type: ignore[arg-type]
    store.ensure_roles()
    return store


@pytest.fixture
def mirror(database: PostgresDatabase, services: Any) -> AxentraIdentityStore:
    return _store(database, services, "mirror")


def _count(database: PostgresDatabase, sql: str, *params: Any) -> int:
    return int(postgres_support.owner_of(database)._query(sql, params)[0][0])


def _seed(identity: IdentityStore) -> dict[str, Any]:
    """Two tenants, five people with every role, an ownerless tenant and sessions, written by ``identity``."""
    identity.create_tenant("acme", "ACME")
    identity.create_tenant("acme_eu", "ACME EU")
    identity.create_tenant("b", "B (waits for its setup link)")
    people = {
        role: identity.login(ISSUER, f"sub-{role}", email=f"{role}@example.com", name=role.title())
        for role in ("owner", "admin", "editor", "operator", "viewer")
    }
    for role, user in people.items():
        identity.set_role("acme", user.id, role)  # type: ignore[arg-type]
    identity.set_role("acme_eu", people["owner"].id, "owner")
    identity.set_role("acme_eu", people["viewer"].id, "editor")
    tokens = {role: identity.create_session(user.id, "acme") for role, user in people.items()}
    tokens["owner-eu"] = identity.create_session(people["owner"].id, "acme_eu")
    return {"people": people, "tokens": tokens}


# ---- configuration --------------------------------------------------------------------------------------


def test_the_identity_backend_is_nodestro_by_default_and_the_others_need_axentra() -> None:
    import base64
    import secrets

    from nodestro_runtime.server_settings import ServerSettings

    base = {
        "NODESTRO_DATABASE_URL": "postgresql://nodestro@db/nodestro",
        "NODESTRO_PUBLIC_ORIGIN": "https://studio.example",
        "NODESTRO_OIDC_ISSUER": "https://id.example",
        "NODESTRO_OIDC_CLIENT_ID": "studio",
        "NODESTRO_OIDC_CLIENT_SECRET": "s3cret",
        "NODESTRO_SECRET_KEY": base64.b64encode(b"k" * 32).decode(),
    }
    assert ServerSettings.from_env(base).axentra.identity_backend == "nodestro"
    for backend in ("mirror", "axentra"):
        with pytest.raises(NodestroError) as caught:
            ServerSettings.from_env({**base, "NODESTRO_IDENTITY_BACKEND": backend})
        assert caught.value.code == "profile.invalid"
        assert [item["field"] for item in caught.value.params["problems"]] == ["NODESTRO_IDENTITY_BACKEND"]
    with pytest.raises(NodestroError):
        ServerSettings.from_env({**base, "NODESTRO_IDENTITY_BACKEND": "both"})
    ring = "k1:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    on = {**base, "NODESTRO_AXENTRA": "on", "NODESTRO_AXENTRA_KEYRING": ring, "NODESTRO_IDENTITY_BACKEND": "Mirror"}
    assert ServerSettings.from_env(on).axentra.identity_backend == "mirror"


def test_the_store_follows_the_switch(database: PostgresDatabase, services: Any) -> None:
    plain = open_identity_store(database, AxentraConfig(), None)
    assert type(plain) is IdentityStore
    config = AxentraConfig(enabled=True, keyring="x", identity_backend="mirror")
    with pytest.raises(NodestroError) as caught:
        open_identity_store(database, config, None)
    assert caught.value.code == "profile.invalid"
    mirrored = open_identity_store(database, config, services)
    assert isinstance(mirrored, AxentraIdentityStore) and mirrored.mode == "mirror"
    roles = {row[0] for row in postgres_support.owner_of(database)._query("SELECT key FROM axentra_roles")}
    assert roles == set(CUSTOM_ROLES)


def test_a_different_idle_limit_in_axentra_is_refused(database: PostgresDatabase, services: Any) -> None:
    from datetime import timedelta

    store = AxentraIdentityStore(
        database, AxentraLink(services, database), mode="mirror", idle_timeout=timedelta(minutes=30)
    )
    with pytest.raises(NodestroError) as caught:
        store.prepare()
    assert caught.value.code == "profile.invalid"


# ---- mirror: one transaction --------------------------------------------------------------------------------


def test_every_write_reaches_both_sides(database: PostgresDatabase, mirror: AxentraIdentityStore) -> None:
    seeded = _seed(mirror)
    assert reconcile_identity(mirror).ok, reconcile_identity(mirror).as_dict()
    assert identity_readiness(mirror) == []
    roles = {
        row[0]: row[1]
        for row in postgres_support.owner_of(database)._query(
            "SELECT l.nodestro_id, m.role FROM axentra_memberships m JOIN nodestro_axentra_ids l"
            " ON l.kind = 'user' AND l.axentra_id = m.user_id JOIN nodestro_axentra_ids t ON t.kind = 'tenant'"
            " AND t.axentra_id = m.tenant_id WHERE t.nodestro_id = 'acme'"
        )
    }
    assert roles == {user.id: ROLE_TO_AXENTRA[role] for role, user in seeded["people"].items()}
    # The same token on both sides: Axentra stores the hex form of Nodestro's hash.
    token = seeded["tokens"]["viewer"]
    from axentra.identity.tokens import hash_token

    assert _count(database, "SELECT count(*) FROM axentra_sessions WHERE token_hash = %s", hash_token(token)) == 1
    mirror.end_session(token)
    assert _count(database, "SELECT count(*) FROM nodestro_sessions") == 5
    assert reconcile_identity(mirror).ok
    mirror.remove_member("acme", seeded["people"]["editor"].id)
    assert mirror.end_sessions(seeded["people"]["owner"].id) == 2
    assert reconcile_identity(mirror).ok


def test_a_failing_axentra_write_rolls_back_the_nodestro_write(
    database: PostgresDatabase, mirror: AxentraIdentityStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror.create_tenant("acme", "ACME")
    user = mirror.login(ISSUER, "sub-1")
    mirror.set_role("acme", user.id, "owner")
    other = mirror.login(ISSUER, "sub-2")

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("Axentra is down")

    monkeypatch.setattr(mirror.platform.tenancy, "set_member", refuse)
    with pytest.raises(RuntimeError):
        mirror.set_role("acme", other.id, "admin")
    assert mirror.role("acme", other.id) is None
    monkeypatch.setattr(mirror.platform.tenancy, "create_tenant", refuse)
    with pytest.raises(RuntimeError):
        mirror.create_tenant("beta", "Beta")
    assert mirror.tenant("beta") is None
    monkeypatch.setattr(mirror.platform.identity, "create_external_user", refuse)
    with pytest.raises(RuntimeError):
        mirror.login(ISSUER, "sub-3")
    assert _count(database, "SELECT count(*) FROM nodestro_users WHERE subject = 'sub-3'") == 0
    monkeypatch.setattr(mirror.platform.identity, "start_session", refuse)
    with pytest.raises(RuntimeError):
        mirror.create_session(user.id, "acme")
    assert _count(database, "SELECT count(*) FROM nodestro_sessions") == 0


def test_a_failing_nodestro_write_rolls_back_the_axentra_write(
    database: PostgresDatabase, mirror: AxentraIdentityStore
) -> None:
    import psycopg

    mirror.create_tenant("acme", "ACME")
    user = mirror.login(ISSUER, "sub-1")
    mirror.set_role("acme", user.id, "owner")
    before = _count(database, "SELECT count(*) FROM axentra_audit_events")
    postgres_support.owner_of(database)._write(
        "CREATE FUNCTION refuse_session() RETURNS trigger LANGUAGE plpgsql AS"
        " $$ BEGIN RAISE EXCEPTION 'refused'; END $$",
        (),
    )
    postgres_support.owner_of(database)._write(
        "CREATE TRIGGER refuse BEFORE INSERT ON nodestro_sessions FOR EACH ROW EXECUTE FUNCTION refuse_session()", ()
    )
    with pytest.raises(psycopg.Error):
        mirror.create_session(user.id, "acme")  # Axentra's session is written first, then Nodestro's fails
    assert _count(database, "SELECT count(*) FROM axentra_sessions") == 0
    assert _count(database, "SELECT count(*) FROM axentra_audit_events") == before  # its login event too


def test_the_last_owner_is_kept_on_both_sides(database: PostgresDatabase, services: Any) -> None:
    for mode in ("mirror", "axentra"):
        store = _store(database, services, mode)
        tenant = f"t-{mode}"
        store.create_tenant(tenant, tenant)
        owner = store.login(ISSUER, f"owner-{mode}")
        store.set_role(tenant, owner.id, "owner")
        for change, args in ((store.set_role, (tenant, owner.id, "viewer")), (store.remove_member, (tenant, owner.id))):
            with pytest.raises(NodestroError) as caught:
                change(*args)  # type: ignore[operator]
            assert caught.value.code == "identity.last_owner"
        assert store.role(tenant, owner.id) == "owner"
        assert reconcile_identity(store).ok


def test_a_tenant_switch_rotates_the_session(database: PostgresDatabase, services: Any) -> None:
    for mode in ("mirror", "axentra"):
        store = _store(database, services, mode)
        for tenant in (f"a-{mode}", f"b-{mode}"):
            store.create_tenant(tenant, tenant)
        user = store.login(ISSUER, f"switcher-{mode}")
        store.set_role(f"a-{mode}", user.id, "owner")
        store.set_role(f"b-{mode}", user.id, "owner")
        first = store.create_session(user.id, f"a-{mode}")
        second = store.create_session(user.id, f"b-{mode}")  # what /api/auth/switch does
        store.end_session(first)
        assert first != second and store.session(first) is None
        assert store.session(second).tenant_id == f"b-{mode}"  # type: ignore[union-attr]


# ---- e-mail addresses: never merged ---------------------------------------------------------------------------


def test_two_issuers_with_one_address_stay_two_people(database: PostgresDatabase, mirror: AxentraIdentityStore) -> None:
    first = mirror.login(ISSUER, "alice", email="Alice@Example.com", name="Alice")
    second = mirror.login(OTHER, "alice-elsewhere", email="alice@example.com", name="Alice too")
    assert first.id != second.id
    assert (mirror.user(first.id).email, mirror.user(second.id).email) == (  # type: ignore[union-attr]
        "Alice@Example.com",
        "alice@example.com",
    )  # Nodestro keeps both addresses
    rows = postgres_support.owner_of(database)._query(
        "SELECT l.nodestro_id, u.email, i.provider FROM nodestro_axentra_ids l JOIN axentra_users u ON u.id ="
        " l.axentra_id JOIN axentra_user_identities i ON i.user_id = u.id WHERE l.kind = 'user' ORDER BY i.provider"
    )
    assert {row[0]: (row[1], row[2]) for row in rows} == {
        first.id: ("Alice@example.com", ISSUER),
        second.id: (None, OTHER),  # the address belongs to another Axentra user: stored without one
    }
    assert _count(database, "SELECT count(DISTINCT axentra_id) FROM nodestro_axentra_ids WHERE kind = 'user'") == 2
    # The first person drops the address: the second one does not get it automatically (no linking by e-mail),
    # but on their next login it is free and becomes theirs on Axentra's side too.
    mirror.login(ISSUER, "alice", email=None, name="Alice")
    mirror.login(OTHER, "alice-elsewhere", email="alice@example.com", name="Alice too")
    emails = dict(
        postgres_support.owner_of(database)._query(
            "SELECT l.nodestro_id, u.email FROM nodestro_axentra_ids l JOIN axentra_users u ON u.id = l.axentra_id"
        )
    )
    assert emails == {first.id: None, second.id: "alice@example.com"}
    assert reconcile_identity(mirror).ok


def test_a_person_without_a_verified_address_logs_in(database: PostgresDatabase, services: Any) -> None:
    for mode in ("mirror", "axentra"):
        store = _store(database, services, mode)
        store.create_tenant(f"t-{mode}", "T")
        user = store.login(ISSUER, f"no-mail-{mode}", email=None, name=None)
        store.set_role(f"t-{mode}", user.id, "viewer")
        token = store.create_session(user.id, f"t-{mode}")
        assert store.session(token).role == "viewer"  # type: ignore[union-attr]
    assert reconcile_identity(store).ok


# ---- roles ------------------------------------------------------------------------------------------------------


def test_every_role_means_the_same_on_both_sides(database: PostgresDatabase, services: Any) -> None:
    from axentra.access.permissions import BUILTIN_ROLES

    store = _store(database, services, "axentra")
    store.create_tenant("acme", "ACME")
    owner = store.login(ISSUER, "owner")
    store.set_role("acme", owner.id, "owner")
    for role in ("owner", "admin", "editor", "operator", "viewer"):
        user = store.login(ISSUER, f"user-{role}")
        store.set_role("acme", user.id, role)  # type: ignore[arg-type]
        assert store.role("acme", user.id) == role
        token = store.create_session(user.id, "acme")
        assert store.session(token).role == role  # type: ignore[union-attr]
    # The custom roles grant no administration in Axentra (members, roles, keys, tenant settings).
    admin = BUILTIN_ROLES["tenant_admin"].permissions
    for key, (_name, permissions) in CUSTOM_ROLES.items():
        assert not {"members:manage", "tenant:update", "keys:manage"} & set(permissions), key
        # Generic permissions below tenant_admin's; Nodestro's own come from the product permissions.
        nodestro = {name for name, _text in PRODUCT_PERMISSIONS.values()}
        assert set(permissions) - nodestro < admin


# ---- switching modes: the same sessions ---------------------------------------------------------------------------


def test_sessions_survive_switching_to_axentra_and_back(database: PostgresDatabase, services: Any) -> None:
    plain = IdentityStore(database)
    seeded = _seed(plain)
    axentra = _store(database, services, "axentra")
    with pytest.raises(NodestroError) as caught:
        axentra.prepare()  # nothing copied yet: axentra mode refuses to start
    assert caught.value.code == "identity.backfill_required"
    report = backfill_identity(axentra).as_dict()
    assert report["tenants"]["created"] == 3 and report["users"]["created"] == 5
    assert report["slug_changes"] == {"acme_eu": "acme-eu", "b": "b-0"}
    assert report["sessions"]["copied"] == 6 and report["memberships"]["added"] == 7
    axentra.prepare()
    for name, token in seeded["tokens"].items():
        found = axentra.session(token)
        assert found is not None, name  # issued before the switch, accepted after it (same hash)
    new = axentra.create_session(seeded["people"]["admin"].id, "acme")
    assert plain.session(new) is not None  # and back: a session made in axentra mode works in nodestro mode
    assert reconcile_identity(axentra).ok


def test_removal_in_axentra_mode_ends_access_at_once(database: PostgresDatabase, services: Any) -> None:
    plain = IdentityStore(database)
    seeded = _seed(plain)
    axentra = _store(database, services, "axentra")
    backfill_identity(axentra)
    viewer = seeded["people"]["viewer"]
    axentra.remove_member("acme", viewer.id)
    assert axentra.session(seeded["tokens"]["viewer"]) is None
    assert plain.session(seeded["tokens"]["viewer"]) is None  # Nodestro's tables were kept current
    assert axentra.memberships(viewer.id) == plain.memberships(viewer.id)
    assert [m.tenant_id for m in axentra.memberships(viewer.id)] == ["acme_eu"]


def test_axentra_mode_reads_axentra(database: PostgresDatabase, services: Any) -> None:
    plain = IdentityStore(database)
    seeded = _seed(plain)
    axentra = _store(database, services, "axentra")
    backfill_identity(axentra)
    assert axentra.tenants() == plain.tenants()
    assert axentra.members("acme") == plain.members("acme")
    owner = seeded["people"]["owner"]
    # A change made on Axentra's side alone is what axentra mode sees (and what reconcile reports).
    postgres_support.owner_of(database)._write(
        "UPDATE axentra_memberships SET role = 'viewer' WHERE role = 'tenant_admin'", ()
    )
    admin = seeded["people"]["admin"]
    assert axentra.role("acme", admin.id) == "viewer" and plain.role("acme", admin.id) == "admin"
    assert axentra.session(seeded["tokens"]["admin"]).role == "viewer"  # type: ignore[union-attr]
    postgres_support.owner_of(database)._write(
        "UPDATE axentra_users SET status = 'disabled' WHERE id = %s", (axentra.link.lookup("user", owner.id),)
    )
    assert axentra.session(seeded["tokens"]["owner"]) is None
    assert not reconcile_identity(axentra).ok


# ---- backfill and reconciliation ---------------------------------------------------------------------------------


def test_the_backfill_is_idempotent_and_dry_runs_change_nothing(database: PostgresDatabase, services: Any) -> None:
    seeded = _seed(IdentityStore(database))
    store = _store(database, services, "mirror")
    dry = backfill_identity(store, dry_run=True).as_dict()
    assert dry["dry_run"] and dry["users"]["created"] == 5
    assert _count(database, "SELECT count(*) FROM nodestro_axentra_ids") == 0
    assert _count(database, "SELECT count(*) FROM axentra_tenants") == 0
    first = backfill_identity(store).as_dict()
    assert {key: value for key, value in first.items() if key != "dry_run"} == {
        key: value for key, value in dry.items() if key != "dry_run"
    }
    second = backfill_identity(store).as_dict()
    assert second["tenants"]["created"] == 0 and second["users"]["created"] == 0
    assert second["memberships"] == {"total": 7, "added": 0, "changed": 0, "removed": 0}
    assert second["sessions"] == {"live": 6, "copied": 0, "updated": 0, "revoked_in_axentra": 0, "ended": 0}
    report = reconcile_identity(store)
    assert report.ok, report.as_dict()
    text = json.dumps(report.as_dict()) + json.dumps(first)
    for token in seeded["tokens"].values():
        assert token not in text
    hashes = [bytes(row[0]).hex() for row in database._query("SELECT token_hash FROM nodestro_sessions")]
    assert hashes and not any(item in text for item in hashes)


def test_the_backfill_resumes_after_a_failure(
    database: PostgresDatabase, services: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nodestro_runtime.axentra_identity as module

    _seed(IdentityStore(database))
    store = _store(database, services, "mirror")
    real = module._copy_memberships
    calls: list[str] = []

    def crash_on_second(store: Any, connection: Any, tenant_id: str, report: Any) -> None:
        calls.append(tenant_id)
        if len(calls) == 2:
            raise RuntimeError("crash")
        real(store, connection, tenant_id, report)

    monkeypatch.setattr(module, "_copy_memberships", crash_on_second)
    with pytest.raises(RuntimeError):
        backfill_identity(store)
    assert not reconcile_identity(store).ok
    monkeypatch.setattr(module, "_copy_memberships", real)
    resumed = backfill_identity(store).as_dict()
    assert resumed["tenants"]["created"] == 0 and resumed["users"]["created"] == 0  # kept from the first run
    assert resumed["memberships"]["added"] == 2  # only acme_eu's were missing
    assert reconcile_identity(store).ok


def test_the_backfill_keeps_people_apart_and_reports_addresses_it_withholds(
    database: PostgresDatabase, services: Any
) -> None:
    plain = IdentityStore(database)
    first = plain.login(ISSUER, "a", email="same@example.com")
    second = plain.login(OTHER, "b", email="same@example.com")
    odd = plain.login(ISSUER, "c", email="not-an-address")
    store = _store(database, services, "mirror")
    report = backfill_identity(store).as_dict()
    assert report["users"]["created"] == 3
    assert report["users_without_email"] == sorted([second.id, odd.id])
    assert first.id not in report["users_without_email"]
    assert reconcile_identity(store).ok


def test_reconcile_finds_drift(database: PostgresDatabase, services: Any) -> None:
    seeded = _seed(IdentityStore(database))
    store = _store(database, services, "mirror")
    backfill_identity(store)
    postgres_support.owner_of(database)._write("DELETE FROM axentra_memberships WHERE role = 'operator'", ())
    postgres_support.owner_of(database)._write(
        "UPDATE axentra_sessions SET revoked_at = now() WHERE id IN (SELECT id FROM axentra_sessions LIMIT 1)", ()
    )
    postgres_support.owner_of(database)._write(
        "UPDATE axentra_users SET name = 'Somebody else' WHERE email = 'admin@example.com'", ()
    )
    IdentityStore(database).create_tenant("late", "Created while the mirror was off")
    report = reconcile_identity(store).as_dict()
    assert not report["ok"]
    assert set(report["differences"]) == {"memberships", "sessions", "tenants", "users"}
    operator = seeded["people"]["operator"].id
    assert f"acme/{operator}: only in Nodestro (operator)" in report["differences"]["memberships"]
    assert report["differences"]["tenants"] == ["late: not linked"]
    assert any("revoked in Axentra" in item for item in report["differences"]["sessions"])
    assert identity_readiness(store)  # the quick check refuses axentra mode too
    repaired = backfill_identity(store).as_dict()
    assert repaired["memberships"]["added"] == 1 and repaired["sessions"]["ended"] == 1
    assert reconcile_identity(store).ok


# ---- a database role under row-level security --------------------------------------------------------------------


def test_everything_works_under_row_level_security(database: PostgresDatabase) -> None:
    """Axentra's tables FORCE row-level security; the tests' superuser bypasses it. A plain role must work too:
    every Axentra read and write of the adapter sets Axentra's scope."""
    import secrets

    from identity_support import axentra_services
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    schema = database.test_schema  # type: ignore[attr-defined]
    owner = axentra_services(database)  # migrates as the superuser
    owner.close()
    role, password = f"nodestro_rls_{secrets.token_hex(4)}", secrets.token_urlsafe(16)
    with psycopg.connect(postgres_support.DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        for statement in (
            "GRANT USAGE ON SCHEMA {schema} TO {role}",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {role}",
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role}",
        ):
            admin.execute(sql.SQL(statement).format(schema=sql.Identifier(schema), role=sql.Identifier(role)))
    parts = conninfo_to_dict(postgres_support.DSN)
    parts.update(user=role, password=password)
    dsn = make_conninfo("", **parts)
    plain_db = PostgresDatabase(dsn, schema=schema)
    import base64

    from nodestro_runtime.axentra_platform import AxentraServices

    ring = "k1:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    services = AxentraServices(
        AxentraConfig(enabled=True, keyring=ring),
        database_url=dsn,
        public_origin="http://localhost:8000",
        schema=schema,
    )
    try:
        services.start()  # refuses superuser/BYPASSRLS roles: this one is subject to RLS
        seeded = _seed(IdentityStore(plain_db))
        plain_db._write("INSERT INTO nodestro_tenants (id, name) VALUES ('acme-eu', 'Dash')", ())
        store = _store(plain_db, services, "mirror")
        report = backfill_identity(store).as_dict()
        assert report["slug_changes"] == {"acme_eu": "acme-eu-2", "b": "b-0"}  # the collision is seen under RLS
        assert reconcile_identity(store).ok
        axentra = _store(plain_db, services, "axentra")
        axentra.prepare()
        assert axentra.session(seeded["tokens"]["editor"]).role == "editor"  # type: ignore[union-attr]
        assert [t.id for t in axentra.tenants()] == ["acme", "acme-eu", "acme_eu", "b"]
        user = axentra.login(ISSUER, "late", email="late@example.com")
        axentra.set_role("b", user.id, "owner")
        axentra.remove_member("acme", seeded["people"]["viewer"].id)
        assert reconcile_identity(axentra).ok
    finally:
        services.close()
        plain_db.close()
        with psycopg.connect(postgres_support.DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


# ---- suspended tenants (report risk 7) ------------------------------------------------------------------------


def _suspend_in_axentra(store: AxentraIdentityStore, tenant_id: str, status: str = "suspended") -> None:
    from axentra.core.context import Principal

    axentra_tenant = store.link.tenant(tenant_id)
    assert axentra_tenant is not None
    store.platform.tenancy.update_tenant(Principal.system("test"), store.product_id, axentra_tenant, status=status)


def test_a_tenant_suspended_in_axentra_is_closed_in_axentra_mode(database: PostgresDatabase, services: Any) -> None:
    plain = IdentityStore(database)
    seeded = _seed(plain)
    axentra = _store(database, services, "axentra")
    backfill_identity(axentra)
    owner = seeded["people"]["owner"]
    _suspend_in_axentra(axentra, "acme")
    for name in ("owner", "admin", "editor", "operator", "viewer"):
        with pytest.raises(NodestroError) as refused:
            axentra.session(seeded["tokens"][name])
        assert refused.value.code == "identity.tenant_suspended", name
    with pytest.raises(NodestroError) as refused:
        axentra.create_session(owner.id, "acme")  # login and tenant switch
    assert refused.value.code == "identity.tenant_suspended"
    assert not axentra.tenant_active("acme") and axentra.tenant_active("acme_eu")
    assert {t.id: t.status for t in axentra.tenants()}["acme"] == "suspended"
    assert {m.tenant_id: m.tenant_status for m in axentra.memberships(owner.id)} == {
        "acme": "suspended",
        "acme_eu": "active",
    }
    assert axentra.session(seeded["tokens"]["owner-eu"]) is not None  # other tenants keep working
    # The difference is reported, and a backfill never resumes the tenant.
    assert (
        "acme: active in Nodestro, suspended in Axentra"
        in reconcile_identity(axentra).as_dict()["differences"]["tenants"]
    )
    report = backfill_identity(axentra).as_dict()
    assert report["suspended_in_axentra_only"] == ["acme"]
    assert not axentra.tenant_active("acme")
    # Resuming in Axentra alone restores access (Nodestro's side was active all along) ...
    _suspend_in_axentra(axentra, "acme", "active")
    assert axentra.session(seeded["tokens"]["owner"]) is not None
    # ... while a suspension made with Nodestro's command closes the tenant on both sides and in every mode.
    axentra.set_tenant_status("acme", "suspended")
    assert plain.tenant("acme").status == "suspended"  # type: ignore[union-attr]
    with pytest.raises(NodestroError):
        plain.session(seeded["tokens"]["owner"])
    _suspend_in_axentra(axentra, "acme", "active")  # an Axentra-side resume does not override Nodestro's suspension
    with pytest.raises(NodestroError):
        axentra.session(seeded["tokens"]["owner"])
    axentra.set_tenant_status("acme", "active")
    assert axentra.session(seeded["tokens"]["owner"]) is not None
    assert reconcile_identity(axentra).ok


def test_mirror_mode_follows_nodestros_status_and_writes_it_to_axentra(
    database: PostgresDatabase, mirror: AxentraIdentityStore
) -> None:
    seeded = _seed(mirror)
    mirror.set_tenant_status("acme", "suspended")
    with mirror._ax(read_only=True) as tx:
        status = tx.scalar("SELECT status FROM axentra_tenants WHERE id = %s", (mirror.link.tenant("acme"),))
    assert status == "suspended"
    with pytest.raises(NodestroError):
        mirror.session(seeded["tokens"]["owner"])
    mirror.set_tenant_status("acme", "active")
    assert mirror.session(seeded["tokens"]["owner"]) is not None
    assert reconcile_identity(mirror).ok


def test_the_backfill_copies_a_suspension(database: PostgresDatabase, services: Any) -> None:
    plain = IdentityStore(database)
    seeded = _seed(plain)
    plain.set_tenant_status("acme", "suspended")
    axentra = _store(database, services, "axentra")
    report = backfill_identity(axentra).as_dict()
    assert report["tenants"]["suspended"] == 1
    axentra.prepare()
    with pytest.raises(NodestroError):
        axentra.session(seeded["tokens"]["viewer"])
    assert reconcile_identity(axentra).ok


# ---- access definitions (report risk 10) and permissions in axentra mode ----------------------------------------


def test_the_access_definitions_are_pinned_per_revision() -> None:
    from nodestro_runtime.axentra_identity import ACCESS_REVISION, access_definitions

    # Changing Nodestro's permissions or roles needs a new revision (and its hash here): Axentra applies a newer
    # revision once, and an older process never downgrades it.
    assert max(REVISION_HASHES) == ACCESS_REVISION
    assert access_definitions().content_hash == REVISION_HASHES[ACCESS_REVISION]


REVISION_HASHES = {1: "880e977447ccf6b784b0ec845bd6f56f522fc3e3c53e1c63b1ae32d3239a5441"}


def test_every_start_applies_the_definitions_once(database: PostgresDatabase, services: Any) -> None:
    from nodestro_runtime.axentra_identity import ACCESS_REVISION

    def fresh() -> AxentraIdentityStore:  # a process that starts
        return AxentraIdentityStore(database, AxentraLink(services, database), mode="mirror")

    first = fresh().apply_access()
    again = fresh().apply_access()
    assert (first.outcome, again.outcome) == ("applied", "unchanged")
    assert first.roles["created"] == ["editor", "operator"]
    assert sorted(first.permissions["added"]) == sorted(name for name, _text in PRODUCT_PERMISSIONS.values())
    stored = services.platform.access_definitions.current(services.product_id)
    assert stored.revision == ACCESS_REVISION and stored.roles == ("editor", "operator")


def test_an_older_process_never_downgrades_and_a_conflict_stops_the_start(
    database: PostgresDatabase, services: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nodestro_runtime.axentra_identity as module

    store = _store(database, services, "mirror")
    # A newer release applied revision 2 with an extra permission.
    newer = module.access_definitions()
    from axentra.access.definitions import AccessDefinitions
    from axentra.access.product_permissions import ProductPermission

    extra = ProductPermission("nodestro.workspace:audit", "tenant", "Read the audit trail")
    services.platform.access_definitions.apply(
        services.product_id, AccessDefinitions(2, (*newer.permissions, extra), newer.roles)
    )
    assert store.apply_access().outcome == "stale"  # this (older) process starts: nothing is undone
    names = {r.permission.name for r in services.platform.permissions.records(services.product_id)}
    assert "nodestro.workspace:audit" in names
    # Two builds that disagree about one revision: the start is refused.
    monkeypatch.setattr(module, "ACCESS_REVISION", 2)
    with pytest.raises(NodestroError) as refused:
        store.prepare()
    assert refused.value.code == "identity.access_conflict"


def test_axentra_role_definitions_decide_the_permissions_in_axentra_mode(
    database: PostgresDatabase, services: Any
) -> None:
    from axentra.access.definitions import AccessDefinitions, RoleDefinition

    from nodestro_runtime.axentra_identity import access_definitions
    from nodestro_runtime.identity_store import ROLE_GRANTS

    plain = IdentityStore(database)
    seeded = _seed(plain)
    axentra = _store(database, services, "axentra")
    mirror = _store(database, services, "mirror")
    backfill_identity(axentra)
    for role in ("owner", "admin", "editor", "operator", "viewer"):
        found = axentra.session(seeded["tokens"][role])
        assert found is not None and found.permissions == tuple(sorted(ROLE_GRANTS[role])), role  # type: ignore[arg-type]
        assert mirror.session(seeded["tokens"][role]).permissions is None  # type: ignore[union-attr]
    # A role change takes effect with the next request.
    operator = seeded["people"]["operator"]
    axentra.set_role("acme", operator.id, "editor")
    assert axentra.session(seeded["tokens"]["operator"]).permissions == ("edit", "read", "run")  # type: ignore[union-attr]
    axentra.set_role("acme", operator.id, "viewer")
    assert axentra.session(seeded["tokens"]["operator"]).permissions == ("read",)  # type: ignore[union-attr]
    # A newer revision of the definitions takes effect at once, in every process.
    current = access_definitions()
    roles = tuple(
        RoleDefinition(r.key, r.name, r.scope, tuple(p for p in r.permissions if p != "nodestro.workspace:run"))
        if r.key == "editor"
        else r
        for r in current.roles
    )
    services.platform.access_definitions.apply(services.product_id, AccessDefinitions(2, current.permissions, roles))
    assert axentra.session(seeded["tokens"]["editor"]).permissions == ("edit", "read")  # type: ignore[union-attr]
    # Removal ends access at once.
    axentra.remove_member("acme", seeded["people"]["editor"].id)
    assert axentra.session(seeded["tokens"]["editor"]) is None
