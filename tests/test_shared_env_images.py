"""Removing an environment never removes a container image someone else uses (backend audit A-07).

Environment images are content-addressed (``nodestro-env:<hash of the lock>``): environments with the same lock share
one image. On a server the Docker daemon serves every tenant, so a tenant's removal never removes an image (the
operator prunes unused ones); on the desktop an image is removed only when no other environment uses it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import postgres_support
import pytest

from nodestro_runtime.python_envs import InstallState, PythonEnvironment, PythonEnvironments
from nodestro_runtime.storage import DocumentStore, SQLiteStore
from nodestro_runtime.workspace import Workspace

LOCK = "six==1.16.0 --hash=sha256:aaaa\n"


class _Images:
    def __init__(self) -> None:
        self.removed: list[str] = []

    def tag(self, lock: str, python_version: str) -> str:
        return f"nodestro-env:{hash((lock, python_version)) & 0xFFFFFFFF:08x}"

    def remove(self, tag: str) -> None:
        self.removed.append(tag)


def _environments(store: DocumentStore, root: Path, images: _Images, **options: Any) -> PythonEnvironments:
    return PythonEnvironments(
        store,
        root,
        tools=None,
        sandbox_kind=lambda: "docker",
        images=images,
        **options,  # type: ignore[arg-type]
    )


def _add(python: PythonEnvironments, scope: str, lock: str = LOCK) -> None:
    kind, ref = scope.split(":", 1)
    env = PythonEnvironment(scope=scope, kind=kind, ref=ref, lock=lock, python_version="3.12")  # type: ignore[arg-type]
    env.install = InstallState(status="installed")
    python._put(env)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    opened = SQLiteStore(tmp_path / "nodestro.db")
    yield opened
    opened.close()


def test_an_image_another_environment_uses_stays(store: SQLiteStore, tmp_path: Path) -> None:
    images = _Images()
    python = _environments(store, tmp_path, images)
    _add(python, "workflow:a")
    _add(python, "workflow:b")
    _add(python, "tool:c", lock="other==1.0 --hash=sha256:bbbb\n")
    python.remove_environment("workflow:a")
    assert images.removed == []  # workflow:b still runs in it
    python.forget("workflow:b")
    assert images.removed == [images.tag(LOCK, "3.12")]  # the last user is gone
    python.forget("tool:c")
    assert images.removed[-1] == images.tag("other==1.0 --hash=sha256:bbbb\n", "3.12")


def test_a_server_never_removes_an_image(tmp_path: Path) -> None:
    postgres_support.require_postgres()
    pytest.importorskip("psycopg")
    from nodestro_runtime.postgres_tables import tenant_storage
    from nodestro_runtime.storage_postgres import PostgresDatabase

    schema = postgres_support.new_schema()
    database = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        database.migrate()
        alpha = Workspace(tmp_path / "alpha", integrations=[], storage=tenant_storage(database, "alpha"))
        try:
            assert alpha.python.remove_images is False
            # The same lock in two tenants: one image on the shared daemon.
            images = _Images()
            first = _environments(database.store("alpha"), tmp_path / "a", images, remove_images=False)
            second = _environments(database.store("beta"), tmp_path / "b", images, remove_images=False)
            _add(first, "workflow:a")
            _add(second, "workflow:b")
            first.remove_environment("workflow:a")
            first.forget("workflow:a")
            assert images.removed == []  # before the fix: the image of tenant beta's environment was removed
            assert second.get("workflow:b") is not None
        finally:
            alpha.close()
    finally:
        database.close()
        postgres_support.drop_schema(schema)


def test_the_desktop_removes_images(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "desktop", integrations=[])
    try:
        assert workspace.python.remove_images is True
    finally:
        workspace.close()
