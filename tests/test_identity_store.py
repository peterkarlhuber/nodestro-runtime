"""Tenants, users, roles, sessions and bootstrap tokens in PostgreSQL (needs ``NODESTRO_TEST_POSTGRES``)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
import hashlib
import threading

import postgres_support
import pytest

from nodestro_runtime.problems import NodestroError

pytest.importorskip("psycopg")

from nodestro_runtime.identity_store import IdentityStore
from nodestro_runtime.storage_postgres import PostgresDatabase

ISSUER = "https://login.example"


@pytest.fixture
def database() -> Iterator[PostgresDatabase]:
    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    yield opened
    opened.close()
    postgres_support.drop_schema(schema)


@pytest.fixture
def identity(database: PostgresDatabase) -> IdentityStore:
    return IdentityStore(database)


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def test_nothing_exists_by_default(identity: IdentityStore, database: PostgresDatabase) -> None:
    assert identity.tenants() == []
    for table in ("nodestro_users", "nodestro_memberships", "nodestro_sessions", "nodestro_bootstrap_tokens"):
        assert database._query(f"SELECT count(*) FROM {table}")[0][0] == 0


def test_tenants_are_created_once_with_valid_ids(identity: IdentityStore) -> None:
    assert identity.create_tenant("acme", "ACME Inc.").name == "ACME Inc."
    with pytest.raises(NodestroError) as caught:
        identity.create_tenant("acme", "again")
    assert caught.value.code == "identity.tenant_exists"
    for bad in ("", "ACME", "a b", "x" * 64):
        with pytest.raises(NodestroError):
            identity.create_tenant(bad, "bad")
    assert [tenant.id for tenant in identity.tenants()] == ["acme"]


def test_a_login_finds_the_same_user_again(identity: IdentityStore) -> None:
    first = identity.login(ISSUER, "sub-1", email="a@example.com", name="A")
    again = identity.login(ISSUER, "sub-1", email="new@example.com", name="A")
    other_issuer = identity.login("https://other.example", "sub-1")
    assert again.id == first.id
    assert other_issuer.id != first.id
    assert identity.user(first.id).email == "new@example.com"  # type: ignore[union-attr]
    assert identity.memberships(first.id) == []  # a login alone grants nothing
    with pytest.raises(NodestroError):
        identity.login(ISSUER, "")


def test_sessions_need_a_membership_and_follow_the_role(identity: IdentityStore, database: PostgresDatabase) -> None:
    identity.create_tenant("acme", "ACME")
    user = identity.login(ISSUER, "sub-1")
    with pytest.raises(NodestroError) as caught:
        identity.create_session(user.id, "acme")
    assert caught.value.code == "identity.not_a_member"
    identity.set_role("acme", user.id, "owner")
    other = identity.login(ISSUER, "sub-2")
    identity.set_role("acme", other.id, "viewer")
    token = identity.create_session(other.id, "acme")
    session = identity.session(token)
    assert session is not None and (session.user_id, session.tenant_id, session.role) == (other.id, "acme", "viewer")
    # Only the hash is stored.
    rows = database._query("SELECT token_hash FROM nodestro_sessions")
    assert [bytes(row[0]) for row in rows] == [_hash(token)]
    assert token.encode() not in b"".join(bytes(row[0]) for row in rows)
    # A changed role applies to the running session; a removed membership ends it.
    identity.set_role("acme", other.id, "editor")
    assert identity.session(token).role == "editor"  # type: ignore[union-attr]
    identity.remove_member("acme", other.id)
    assert identity.session(token) is None


def test_sessions_end(identity: IdentityStore) -> None:
    identity.create_tenant("acme", "ACME")
    user = identity.login(ISSUER, "sub-1")
    identity.set_role("acme", user.id, "owner")
    token = identity.create_session(user.id, "acme")
    assert identity.session("") is None
    assert identity.session("x" * 300) is None
    assert identity.session(token + "x") is None
    identity.end_session(token)
    assert identity.session(token) is None
    second = identity.create_session(user.id, "acme")
    third = identity.create_session(user.id, "acme")
    assert identity.end_sessions(user.id) == 2
    assert identity.session(second) is None and identity.session(third) is None


def test_sessions_expire_and_time_out(database: PostgresDatabase) -> None:
    identity = IdentityStore(database, session_lifetime=timedelta(seconds=-1))
    identity.create_tenant("acme", "ACME")
    user = identity.login(ISSUER, "sub-1")
    identity.set_role("acme", user.id, "owner")
    assert identity.session(identity.create_session(user.id, "acme")) is None
    idle = IdentityStore(database, idle_timeout=timedelta(seconds=-1))
    assert idle.session(idle.create_session(user.id, "acme")) is None
    assert idle.cleanup() >= 2


def test_a_tenant_keeps_an_owner(identity: IdentityStore) -> None:
    identity.create_tenant("acme", "ACME")
    owner = identity.login(ISSUER, "owner")
    identity.set_role("acme", owner.id, "owner")
    for change in (
        lambda: identity.set_role("acme", owner.id, "admin"),
        lambda: identity.remove_member("acme", owner.id),
    ):
        with pytest.raises(NodestroError) as caught:
            change()
        assert caught.value.code == "identity.last_owner"
    second = identity.login(ISSUER, "second")
    identity.set_role("acme", second.id, "owner")
    identity.set_role("acme", owner.id, "admin")
    assert identity.role("acme", owner.id) == "admin"


def test_login_states_are_taken_once(identity: IdentityStore) -> None:
    state = identity.create_login_state(nonce="n", code_verifier="v", return_to="/runs", bootstrap_token="boot")
    taken = identity.take_login_state(state)
    assert taken is not None and (taken.nonce, taken.code_verifier, taken.return_to) == ("n", "v", "/runs")
    assert taken.bootstrap_hash == _hash("boot")
    assert identity.take_login_state(state) is None
    assert identity.take_login_state("unknown") is None


def test_expired_login_states_are_refused(database: PostgresDatabase) -> None:
    identity = IdentityStore(database, login_lifetime=timedelta(seconds=-1))
    state = identity.create_login_state(nonce="n", code_verifier="v", return_to="/")
    assert identity.take_login_state(state) is None


def test_a_bootstrap_token_binds_the_first_owner_once(identity: IdentityStore) -> None:
    with pytest.raises(NodestroError):
        identity.create_bootstrap_token("missing")
    identity.create_tenant("acme", "ACME")
    token = identity.create_bootstrap_token("acme")
    first = identity.login(ISSUER, "first")
    assert identity.use_bootstrap(_hash(token), first.id) == "acme"
    assert identity.role("acme", first.id) == "owner"
    second = identity.login(ISSUER, "second")
    with pytest.raises(NodestroError) as caught:
        identity.use_bootstrap(_hash(token), second.id)
    assert caught.value.code == "identity.bootstrap_invalid"
    assert identity.role("acme", second.id) is None
    with pytest.raises(NodestroError) as caught:
        identity.create_bootstrap_token("acme")
    assert caught.value.code == "identity.tenant_has_owner"


def test_a_bootstrap_token_is_refused_once_an_owner_exists(identity: IdentityStore) -> None:
    identity.create_tenant("acme", "ACME")
    early, late = identity.create_bootstrap_token("acme"), identity.create_bootstrap_token("acme")
    first = identity.login(ISSUER, "first")
    identity.use_bootstrap(_hash(early), first.id)
    intruder = identity.login(ISSUER, "intruder")
    with pytest.raises(NodestroError):
        identity.use_bootstrap(_hash(late), intruder.id)
    assert identity.role("acme", intruder.id) is None


def test_expired_bootstrap_tokens_are_refused(identity: IdentityStore) -> None:
    identity.create_tenant("acme", "ACME")
    token = identity.create_bootstrap_token("acme", lifetime=timedelta(seconds=-1))
    with pytest.raises(NodestroError):
        identity.use_bootstrap(_hash(token), identity.login(ISSUER, "x").id)


def test_two_users_racing_for_one_token_get_one_owner(identity: IdentityStore, database: PostgresDatabase) -> None:
    identity.create_tenant("acme", "ACME")
    token = identity.create_bootstrap_token("acme")
    users = [identity.login(ISSUER, f"user-{number}") for number in range(4)]
    wins: list[str] = []
    start = threading.Barrier(len(users))

    def claim(user_id: str) -> None:
        start.wait(5)
        try:
            identity.use_bootstrap(_hash(token), user_id)
            wins.append(user_id)
        except NodestroError:
            pass

    threads = [threading.Thread(target=claim, args=(user.id,)) for user in users]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(wins) == 1
    assert [member.user_id for member in identity.members("acme")] == wins
