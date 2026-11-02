"""Input file bytes in Axentra's storage (``NODESTRO_FILES_BACKEND=axentra``): ids, isolation, policy, quotas,
integrity, migration with the volume as fallback, verification, guarded source removal and the rollback export.

Needs ``NODESTRO_TEST_POSTGRES`` for the database cases and the ``axentra`` package (Python 3.11+). The S3 case runs
against moto's S3 server when ``moto[server]`` is installed.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import timedelta
import hashlib
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Any

import postgres_support
import pytest

from nodestro_runtime.file_formats import FileFormatError
from nodestro_runtime.input_files import InputFiles
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.server_settings import ServerSettings

ENV = {
    "NODESTRO_DATABASE_URL": "postgresql://nodestro@db/nodestro",
    "NODESTRO_PUBLIC_ORIGIN": "https://studio.example",
    "NODESTRO_OIDC_ISSUER": "https://id.example",
    "NODESTRO_OIDC_CLIENT_ID": "studio",
    "NODESTRO_OIDC_CLIENT_SECRET": "s3cret",
    "NODESTRO_SECRET_KEY": base64.b64encode(b"k" * 32).decode(),
}


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


# ---- settings (no database) ------------------------------------------------------------------------------


def test_desktop_code_paths_never_import_axentra() -> None:
    code = (
        "import sys\n"
        "import nodestro_runtime.input_files, nodestro_runtime.workspace, nodestro_runtime.file_delivery\n"
        "assert not [m for m in sys.modules if m == 'axentra' or m.startswith('axentra.')], 'axentra imported'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_files_backend_is_volume_by_default_and_needs_axentra() -> None:
    pytest.importorskip("axentra")
    config = ServerSettings.from_env(ENV).axentra
    assert config.files_backend == "volume" and not config.files_source_removal
    assert config.files_source_retention_days == 30 and "storage" not in config.modules
    with pytest.raises(NodestroError) as caught:
        ServerSettings.from_env({**ENV, "NODESTRO_FILES_BACKEND": "axentra"})
    assert [item["field"] for item in caught.value.params["problems"]] == ["NODESTRO_FILES_BACKEND"]
    with pytest.raises(NodestroError) as caught:
        ServerSettings.from_env(
            {
                **ENV,
                "NODESTRO_AXENTRA": "on",
                "NODESTRO_AXENTRA_KEYRING": f"k1:{_key()}",
                "NODESTRO_FILES_BACKEND": "s3",
                "NODESTRO_FILES_SOURCE_REMOVAL": "perhaps",
                "NODESTRO_FILES_SOURCE_RETENTION_DAYS": "-1",
            }
        )
    fields = [item["field"] for item in caught.value.params["problems"]]
    assert fields == [
        "NODESTRO_FILES_BACKEND",
        "NODESTRO_FILES_SOURCE_REMOVAL",
        "NODESTRO_FILES_SOURCE_RETENTION_DAYS",
    ]
    on = ServerSettings.from_env(
        {
            **ENV,
            "NODESTRO_AXENTRA": "on",
            "NODESTRO_AXENTRA_KEYRING": f"k1:{_key()}",
            "NODESTRO_FILES_BACKEND": "axentra",
            "NODESTRO_FILES_SOURCE_REMOVAL": "1",
            "NODESTRO_FILES_SOURCE_RETENTION_DAYS": "7",
        }
    ).axentra
    assert on.files_backend == "axentra" and on.files_source_removal and on.files_source_retention_days == 7
    assert "storage" in on.modules


# ---- PostgreSQL ------------------------------------------------------------------------------------------


@pytest.fixture
def database() -> Iterator[Any]:
    pytest.importorskip("axentra", reason="Axentra needs Python 3.11+ and nodestro-runtime[postgres]")
    pytest.importorskip("psycopg")
    from identity_support import create_tenants

    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    opened.test_schema = schema  # type: ignore[attr-defined]
    create_tenants(opened, [("alpha", "Alpha"), ("beta", "Beta")])
    yield opened
    opened.close()
    postgres_support.drop_schema(schema)


def open_services(database: Any, tmp_path: Path, storage: str, **config: Any) -> Any:
    from nodestro_runtime.axentra_platform import AxentraConfig, AxentraServices

    config_file = tmp_path / "axentra.toml"
    config_file.write_text(f"[storage]\n{storage}\n")
    services = AxentraServices(
        AxentraConfig(
            enabled=True,
            keyring=f"k1:{_key()}",
            allow_superuser=True,
            config_file=str(config_file),
            files_backend="axentra",
            **config,
        ),
        database_url=postgres_support.DSN,
        public_origin="http://localhost:8000",
        schema=database.test_schema,
    )
    services.migrate()
    services.start()
    return services


@pytest.fixture
def files(database: Any, tmp_path: Path) -> Iterator[Any]:
    from nodestro_runtime.axentra_storage import open_files

    services = open_services(database, tmp_path, f'backend = "local"\nlocal_path = "{tmp_path / "objects"}"')
    try:
        yield open_files(services, database)
    finally:
        services.close()


def _inputs(files: Any, data_root: Path, tenant: str) -> InputFiles:
    root = data_root / "tenants" / tenant / "inputs"
    return InputFiles(files.database.store(tenant), root, blobs=files.blobs(tenant, root))


def _volume_inputs(database: Any, data_root: Path, tenant: str) -> InputFiles:
    return InputFiles(database.store(tenant), data_root / "tenants" / tenant / "inputs")


def _axentra_rows(database: Any) -> list[tuple[Any, ...]]:
    """Axentra's file rows as the owner sees them: a check of the test, not a product read (those go through Axentra's
    tenant scope); the runtime role sees no row without that scope (row-level security)."""
    return postgres_support.owner_of(database)._query(
        "SELECT id, tenant_id, status, sha256, size, stored_inert FROM axentra_files ORDER BY id"
    )


def test_uploads_go_to_axentra_with_stable_ids_and_one_object_per_content(files: Any, tmp_path: Path) -> None:
    inputs = _inputs(files, tmp_path / "data", "alpha")
    first = inputs.add(b"a,b\n1,2\n", name="Belege/2024/table.csv")
    second = inputs.add(b"a,b\n1,2\n", name="copy.csv")
    assert first.id != second.id and first.blob == second.blob == hashlib.sha256(b"a,b\n1,2\n").hexdigest()[:16]
    assert inputs.data(first.id) == b"a,b\n1,2\n" and inputs.extracted(second.id).chars > 0
    rows = _axentra_rows(files.database)
    assert len(rows) == 3  # the content once, plus the cached extraction of each name
    assert all(row[2] == "active" for row in rows)
    content = [row for row in rows if row[3] == first.sha256]
    assert len(content) == 1 and content[0][1] == files.link.tenant("alpha")
    assert not (tmp_path / "data" / "tenants" / "alpha" / "inputs" / "blobs" / first.blob).exists()  # not on the volume
    # A delete removes the object only when no other input file uses the content.
    inputs.delete(first.id, force=True)
    assert inputs.data(second.id) == b"a,b\n1,2\n"
    inputs.delete(second.id, force=True)
    assert {row[2] for row in _axentra_rows(files.database)} == {"deleted"}
    assert files.lookup("alpha", first.blob) is None


def test_tenants_never_see_each_others_bytes(files: Any, tmp_path: Path) -> None:
    alpha = _inputs(files, tmp_path / "data", "alpha")
    beta = _inputs(files, tmp_path / "data", "beta")
    item = alpha.add(b"alpha secret", name="a.txt")
    assert item.blob not in beta.blobs
    with pytest.raises(KeyError):
        beta.blobs.get_bytes(item.blob)
    tenants = {row[1] for row in _axentra_rows(files.database)}
    assert tenants == {files.link.tenant("alpha")} and files.link.tenant("alpha") != files.axentra_tenant("beta")


def test_a_mapping_copied_to_another_tenant_never_reads_its_object(files: Any, tmp_path: Path) -> None:
    from nodestro_runtime.axentra_storage import COLLECTION

    item = _inputs(files, tmp_path / "data", "alpha").add(b"alpha only", name="a.txt")
    forged = files.lookup("alpha", item.blob)
    files.database.store("beta").put(COLLECTION, item.blob, forged)
    with pytest.raises(NodestroError) as caught:
        _inputs(files, tmp_path / "data", "beta").blobs.get_bytes(item.blob)
    assert caught.value.code == "input.integrity"


def test_blocked_types_are_kept_inert_and_downloaded_as_opaque_bytes(files: Any, tmp_path: Path) -> None:
    inputs = _inputs(files, tmp_path / "data", "alpha")
    page = inputs.add(b"<html><script>alert(1)</script></html>", name="page.html", media_type="text/html")
    script = inputs.add(b"#!/bin/sh\necho hi\n", name="run.sh")
    plain = inputs.add(b"just text", name="note.txt")
    assert inputs.download_type(page.id) == "application/octet-stream"
    assert inputs.download_type(script.id) == "application/octet-stream"
    assert inputs.download_type(plain.id) == plain.media_type
    assert inputs.data(page.id).startswith(b"<html>")  # agents still read the content as text
    assert "<script>" in inputs.extracted(page.id).text
    # An operator who sets the product to refuse gets a clear upload problem; nothing is stored.
    product = files.services.product_id
    files.storage.set_policy(
        files.principal,
        product,
        blocked_types="refuse",
        blocked_extensions=[],
        blocked_content_types=[],
        inert_extensions=[],
    )
    assert files.ensure_policy() == "refuse"  # an explicit product policy is respected
    before = len(_axentra_rows(files.database))
    with pytest.raises(FileFormatError) as caught:
        inputs.add(b"echo other\n", name="other.sh")
    assert caught.value.code == "input.blocked_type"
    assert len(_axentra_rows(files.database)) == before and len(inputs.all()) == 3


def test_quotas_and_the_size_limit_refuse_an_upload_without_leaving_anything(files: Any, tmp_path: Path) -> None:
    inputs = _inputs(files, tmp_path / "data", "alpha")
    files.storage.set_quota(
        files.principal, files.services.product_id, files.axentra_tenant("alpha"), quota_bytes=64, quota_files=None
    )
    with pytest.raises(FileFormatError) as caught:
        inputs.add(b"x" * 200, name="big.txt")
    assert caught.value.code == "input.quota_exceeded" and inputs.all() == []
    files.storage.cfg.max_file_bytes = 10
    with pytest.raises(FileFormatError) as caught:
        inputs.add(b"y" * 20, name="big.txt")
    assert caught.value.code == "input.too_large"
    assert all(row[2] != "active" for row in _axentra_rows(files.database))


def test_a_changed_object_is_never_served(files: Any, tmp_path: Path) -> None:
    inputs = _inputs(files, tmp_path / "data", "alpha")
    item = inputs.add(b"original content", name="a.txt")
    mapping = files.lookup("alpha", item.blob)
    path = next((tmp_path / "objects").rglob(mapping.axentra_file))
    path.write_bytes(b"tampered content")
    with pytest.raises(NodestroError) as caught:
        inputs.data(item.id)
    assert caught.value.code == "input.integrity"
    path.unlink()
    with pytest.raises(NodestroError) as caught:
        inputs.data(item.id)
    assert caught.value.code == "input.content_missing"


def test_empty_files_need_no_object(files: Any, tmp_path: Path) -> None:
    inputs = _inputs(files, tmp_path / "data", "alpha")
    blob = inputs.blobs.put(b"", name="empty.txt", media_type="text/plain")
    assert inputs.blobs.get_bytes(blob) == b"" and files.lookup("alpha", blob).axentra_file is None
    inputs.blobs.delete(blob)
    assert blob not in inputs.blobs


def test_a_tenant_workspace_reads_its_inputs_through_axentra(files: Any, tmp_path: Path) -> None:
    from dataclasses import replace

    from nodestro_runtime.file_delivery import plan
    from nodestro_runtime.postgres_tables import tenant_storage
    from nodestro_runtime.workspace import Workspace

    storage = replace(tenant_storage(files.database, "alpha"), input_blobs=lambda root: files.blobs("alpha", root))
    workspace = Workspace(tmp_path / "data" / "tenants" / "alpha", integrations=[], storage=storage)
    try:
        assert type(workspace.inputs.blobs).__name__ == "AxentraBlobs"
        item = workspace.inputs.add(b"# Title\n\nSome text for the agent.\n", name="brief.md")
        assert workspace.inputs.extracted(item.id).text.startswith("# Title")
        assert callable(plan)  # delivery decides from the stored metadata, which is unchanged
    finally:
        workspace.close()


# ---- migration ----------------------------------------------------------------------------------------


def test_migration_copies_verifies_and_never_deletes_the_source(files: Any, tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    old = _volume_inputs(files.database, data_root, "alpha")
    first = old.add(b"first,file\n1,2\n", name="first.csv")
    second = old.add(b"second file", name="second.txt")
    other = _volume_inputs(files.database, data_root, "beta").add(b"beta file", name="b.txt")
    volume = data_root / "tenants" / "alpha" / "inputs" / "blobs"
    before = sorted(path.name for path in volume.iterdir())
    # After the switch the old files are read from the volume (fallback) until they are migrated.
    inputs = _inputs(files, data_root, "alpha")
    assert inputs.data(first.id) == b"first,file\n1,2\n" and inputs.extracted(second.id).chars > 0
    planned = files.migrate(data_root, dry_run=True)
    assert planned.pending == 6 and planned.copied == 0 and _axentra_rows(files.database) == []
    alpha_only = files.migrate(data_root, tenant="alpha")
    assert (alpha_only.tenants, alpha_only.copied, alpha_only.failed) == (1, 4, [])
    report = files.migrate(data_root)
    assert (report.copied, report.already, report.failed) == (2, 4, [])
    assert sorted(path.name for path in volume.iterdir()) == before  # the volume files stay
    again = files.migrate(data_root)
    assert (again.copied, again.already) == (0, 6)  # idempotent
    assert inputs.data(first.id) == b"first,file\n1,2\n" and files.lookup("alpha", first.blob).origin == "migration"
    assert _inputs(files, data_root, "beta").data(other.id) == b"beta file"
    checked = files.verify(data_root)
    assert checked.as_dict()["ok"] and checked.verified == 6 and checked.pending == 0 and checked.orphans == []
    assert files.lookup("alpha", first.blob).verified_at is not None


def test_a_damaged_volume_file_is_reported_and_not_copied(files: Any, tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    item = _volume_inputs(files.database, data_root, "alpha").add(b"good content", name="a.txt")
    (data_root / "tenants" / "alpha" / "inputs" / "blobs" / item.blob).write_bytes(b"bad content")
    report = files.migrate(data_root, tenant="alpha")
    assert [entry["blob"] for entry in report.failed] == [item.blob] and report.copied == 1  # the extraction
    assert files.lookup("alpha", item.blob) is None


def test_a_file_deleted_during_its_copy_is_not_resurrected(files: Any, tmp_path: Path) -> None:
    data = b"deleted meanwhile"
    blob = hashlib.sha256(data).hexdigest()[:16]
    item = files.upload("alpha", data, blob=blob, name="x.txt", media_type="text/plain", origin="migration")
    with pytest.raises(KeyError):
        files.record("alpha", item, source=tmp_path / "missing")
    assert files.lookup("alpha", blob) is None
    assert [row[2] for row in _axentra_rows(files.database)] == ["deleted"]


def test_source_removal_is_guarded(files: Any, tmp_path: Path) -> None:
    from nodestro_runtime.axentra_storage import AxentraFiles

    data_root = tmp_path / "data"
    old = _volume_inputs(files.database, data_root, "alpha")
    item = old.add(b"keep me safe", name="a.txt")
    blob_path = data_root / "tenants" / "alpha" / "inputs" / "blobs" / item.blob
    files.migrate(data_root)
    with pytest.raises(NodestroError) as caught:
        files.remove_sources(data_root)  # default off
    assert caught.value.code == "files.source_removal_off"
    guarded = AxentraFiles(files.services, files.database, retention_days=30, source_removal=True)
    unverified = guarded.remove_sources(data_root, now=None)
    assert unverified.removed == 0 and unverified.kept == 2 and blob_path.exists()  # not verified yet
    guarded.verify(data_root)
    early = guarded.remove_sources(data_root)
    assert early.removed == 0 and early.kept == 2 and blob_path.exists()  # fallback period not over
    from nodestro_runtime.schemas import now

    later = now() + timedelta(days=31)
    planned = guarded.remove_sources(data_root, dry_run=True, now=later)
    assert planned.removed == 2 and blob_path.exists()
    done = guarded.remove_sources(data_root, now=later)
    assert done.removed == 2 and done.failed == [] and not blob_path.exists()
    assert files.lookup("alpha", item.blob).source_removed_at is not None
    assert _inputs(files, data_root, "alpha").data(item.id) == b"keep me safe"


def test_source_removal_refuses_a_damaged_copy(files: Any, tmp_path: Path) -> None:
    from nodestro_runtime.axentra_storage import AxentraFiles
    from nodestro_runtime.schemas import now

    data_root = tmp_path / "data"
    item = _volume_inputs(files.database, data_root, "alpha").add(b"precious", name="a.txt")
    files.migrate(data_root)
    files.verify(data_root)
    mapping = files.lookup("alpha", item.blob)
    next((tmp_path / "objects").rglob(mapping.axentra_file)).write_bytes(b"broken!!")
    guarded = AxentraFiles(files.services, files.database, retention_days=0, source_removal=True)
    report = guarded.remove_sources(data_root, now=now() + timedelta(days=1))
    assert [entry["reason"] for entry in report.failed] == ["input.integrity"]
    assert (data_root / "tenants" / "alpha" / "inputs" / "blobs" / item.blob).exists()


def test_verify_reports_missing_objects_and_orphans(files: Any, tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    inputs = _inputs(files, data_root, "alpha")
    item = inputs.add(b"will vanish", name="a.txt")
    next((tmp_path / "objects").rglob(files.lookup("alpha", item.blob).axentra_file)).unlink()
    stray = files.storage.upload(
        files.principal,
        files.services.product_id,
        files.axentra_tenant("alpha"),
        b"stray",
        name="stray.txt",
        metadata={"nodestro_blob": "0" * 16},
    )
    from nodestro_runtime.schemas import now

    report = files.verify(data_root, now=now() + timedelta(hours=2))
    assert [(entry["blob"], entry["reason"]) for entry in report.failed] == [(item.blob, "input.content_missing")]
    assert [entry["file"] for entry in report.orphans] == [stray.id]


def _later() -> Any:
    from nodestro_runtime.schemas import now

    return now() + timedelta(hours=2)


def test_an_object_of_a_cut_off_copy_fails_verify_and_cleanup_removes_it(files: Any, tmp_path: Path) -> None:
    """``files migrate`` killed between the upload and the mapping leaves an Axentra object nothing uses."""
    data_root = tmp_path / "data"
    item = _volume_inputs(files.database, data_root, "alpha").add(b"copied when killed", name="a.txt")
    cut_off = files.upload(
        "alpha", b"copied when killed", blob=item.blob, name="a.txt", media_type="text/plain", origin="migration"
    )
    fresh = files.verify(data_root).as_dict()  # may still be a copy in flight
    assert fresh["ok"] and fresh["orphans"] == [] and fresh["recent"] == 1
    assert files.cleanup().removed == 0
    late = files.verify(data_root, now=_later()).as_dict()
    assert not late["ok"] and [entry["file"] for entry in late["orphans"]] == [cut_off.axentra_file]
    planned = files.cleanup(dry_run=True, now=_later())
    assert planned.removed == 0 and [entry["file"] for entry in planned.orphans] == [cut_off.axentra_file]
    assert files.cleanup(now=_later()).removed == 1
    assert {row[0]: row[2] for row in _axentra_rows(files.database)}[cut_off.axentra_file] == "deleted"
    assert files.verify(data_root, now=_later()).as_dict()["ok"]
    assert files.migrate(data_root).copied == 2  # the copy itself is simply made again
    assert files.cleanup(now=_later()).removed == 0


def test_a_delete_made_while_the_backend_was_volume_is_removed_from_axentra(files: Any, tmp_path: Path) -> None:
    """Security review SR-09: export, switch back to ``volume``, a user deletes a file, switch forward again."""
    data_root = tmp_path / "data"
    kept = _inputs(files, data_root, "alpha").add(b"stays", name="keep.txt")
    gone = _inputs(files, data_root, "alpha").add(b"personal data the user deleted", name="gone.txt")
    files.export_to_volume(data_root)
    rolled_back = _volume_inputs(files.database, data_root, "alpha")
    rolled_back.delete(gone.id, force=True)  # the volume backend knows nothing about Axentra
    objects = {files.lookup("alpha", blob).axentra_file for blob in (gone.blob, gone.extract_blob)}
    assert None not in objects
    report = files.verify(data_root, now=_later()).as_dict()
    assert not report["ok"] and report["failed"] == [] and report["orphans"] == []
    assert sorted(entry["blob"] for entry in report["unreferenced"]) == sorted([gone.blob, gone.extract_blob])
    planned = files.cleanup(dry_run=True, now=_later())
    assert planned.removed == 0 and len(planned.unreferenced) == 2
    assert files.lookup("alpha", gone.blob) is not None
    done = files.cleanup(now=_later())
    assert (done.removed, done.kept, done.failed) == (2, 0, [])
    states = {row[0]: row[2] for row in _axentra_rows(files.database)}
    assert {states[file] for file in objects} == {"deleted"}
    assert files.lookup("alpha", gone.blob) is None and files.lookup("alpha", kept.blob) is not None
    assert files.verify(data_root, now=_later()).as_dict()["ok"]
    assert _inputs(files, data_root, "alpha").data(kept.id) == b"stays"


def test_cleanup_keeps_a_mapping_that_an_upload_just_reused(files: Any, tmp_path: Path) -> None:
    from nodestro_runtime.axentra_storage import COLLECTION

    data_root = tmp_path / "data"
    gone = _inputs(files, data_root, "alpha").add(b"uploaded again", name="a.txt")
    files.export_to_volume(data_root)
    _volume_inputs(files.database, data_root, "alpha").delete(gone.id, force=True)
    store = files.database.store("alpha")
    for blob in (gone.blob, gone.extract_blob):  # the leftovers are old by now
        old = files.lookup("alpha", blob)
        store.put(COLLECTION, blob, old.model_copy(update={"created_at": old.created_at - timedelta(hours=2)}))
    # The same content is uploaded again: its blob store reuses the mapping, the input file is written after that.
    assert _inputs(files, data_root, "alpha").blobs.put(b"uploaded again", name="a.txt", media_type="text/plain")
    report = files.cleanup()
    assert report.removed == 1 and report.recent == 1  # only the unused extraction goes
    assert files.lookup("alpha", gone.blob) is not None


def test_export_to_the_volume_makes_a_rollback_possible(files: Any, tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    item = _inputs(files, data_root, "alpha").add(b"made in axentra mode", name="a.txt")
    assert files.export_to_volume(data_root, dry_run=True).pending == 2
    report = files.export_to_volume(data_root)
    assert (report.copied, report.failed) == (2, [])
    assert files.export_to_volume(data_root).already == 2
    rolled_back = _volume_inputs(files.database, data_root, "alpha")
    assert rolled_back.data(item.id) == b"made in axentra mode" and rolled_back.extracted(item.id).chars > 0


# ---- S3 (moto's S3 server) ---------------------------------------------------------------------------------


@pytest.fixture
def s3_endpoint(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    pytest.importorskip("boto3")
    server_module = pytest.importorskip("moto.server")
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    server = server_module.ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    try:
        import boto3

        endpoint = f"http://127.0.0.1:{port}"
        boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1").create_bucket(Bucket="nodestro-files")
        yield endpoint
    finally:
        server.stop()


def test_the_s3_backend_stores_migrates_and_deletes(database: Any, tmp_path: Path, s3_endpoint: str) -> None:
    import boto3

    from nodestro_runtime.axentra_storage import open_files

    services = open_services(
        database,
        tmp_path,
        f'backend = "s3"\ns3_bucket = "nodestro-files"\ns3_endpoint_url = "{s3_endpoint}"\n'
        's3_region = "us-east-1"\ns3_prefix = "nodestro"',
    )
    try:
        files = open_files(services, database)
        data_root = tmp_path / "data"
        old = _volume_inputs(database, data_root, "alpha").add(b"from the volume", name="old.txt")
        inputs = _inputs(files, data_root, "alpha")
        new = inputs.add(b"straight to s3", name="new.txt")
        assert files.migrate(data_root).copied == 2
        client = boto3.client("s3", endpoint_url=s3_endpoint, region_name="us-east-1")
        keys = [item["Key"] for item in client.list_objects_v2(Bucket="nodestro-files").get("Contents", [])]
        assert len(keys) == 4 and all(key.startswith("nodestro/") for key in keys)
        assert inputs.data(old.id) == b"from the volume" and inputs.data(new.id) == b"straight to s3"
        assert files.verify(data_root).as_dict()["ok"]
        inputs.delete(new.id, force=True)
        keys = [item["Key"] for item in client.list_objects_v2(Bucket="nodestro-files").get("Contents", [])]
        assert len(keys) == 2
    finally:
        services.close()


def test_a_tenant_with_a_long_name_gets_its_axentra_tenant_for_files(files: Any, tmp_path: Path) -> None:
    from identity_support import create_tenants

    from nodestro_runtime.axentra_platform import NAME_LIMIT

    create_tenants(files.database, [("long", "N" * 300)])
    item = _inputs(files, tmp_path / "data", "long").add(b"a file of a tenant with a long name", name="a.txt")
    assert _inputs(files, tmp_path / "data", "long").data(item.id).startswith(b"a file")
    rows = postgres_support.owner_of(files.database)._query(
        "SELECT name FROM axentra_tenants WHERE id = %s", (files.link.tenant("long"),)
    )
    assert len(rows[0][0]) == NAME_LIMIT


def test_the_local_directory_exists_after_the_start_before_any_upload(files: Any, tmp_path: Path) -> None:
    """A fresh installation is backed up before its first upload: the backup archives the directory."""
    directory = tmp_path / "objects"
    assert directory.is_dir() and (directory.stat().st_mode & 0o777) == 0o700
    assert list(directory.iterdir()) == []
