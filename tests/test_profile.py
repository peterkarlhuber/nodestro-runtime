"""Operating profiles: the desktop default, the environment, and refusing what cannot start yet."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.profile import (
    ENV_DATA_DIR,
    ENV_PROFILE,
    PROFILE_KINDS,
    SERVER_REQUIREMENTS,
    RuntimeProfile,
)
from nodestro_runtime.workspace import DATABASE, LOCK_FILE, Workspace


def _fields(error: pytest.ExceptionInfo[NodestroError]) -> list[str]:
    return [item["field"] for item in error.value.params["problems"]]


# ---- the profile itself -------------------------------------------------------------------


def test_desktop_profile_keeps_the_data_directory_as_given(tmp_path: Path) -> None:
    profile = RuntimeProfile.desktop(tmp_path / "data")
    assert profile.kind == "desktop"
    assert profile.data_dir == tmp_path / "data"
    assert RuntimeProfile.desktop(str(tmp_path / "data")) == profile
    assert RuntimeProfile.desktop("~/somewhere").data_dir == Path("~/somewhere").expanduser()
    # Relative paths are not resolved, exactly as Workspace(data_dir) always kept them.
    assert RuntimeProfile.desktop("relative/data").data_dir == Path("relative/data")
    assert RuntimeProfile.desktop("").data_dir == Path("")


def test_profiles_are_immutable(tmp_path: Path) -> None:
    profile = RuntimeProfile.desktop(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.kind = "server"  # type: ignore[misc]


def test_profile_values_are_checked_when_built(tmp_path: Path) -> None:
    with pytest.raises(NodestroError) as error:
        RuntimeProfile(kind="cloud", data_dir=tmp_path)  # type: ignore[arg-type]
    assert error.value.code == "profile.invalid"
    assert _fields(error) == ["kind"]
    with pytest.raises(NodestroError) as error:
        RuntimeProfile(kind="laptop", data_dir=42)  # type: ignore[arg-type]
    assert _fields(error) == ["kind", "data_dir"]
    with pytest.raises(NodestroError) as error:
        RuntimeProfile.desktop("data\x00dir")
    assert error.value.code == "profile.invalid"


def test_the_server_profile_starts_only_with_its_settings(tmp_path: Path) -> None:
    from nodestro_runtime.server_settings import ServerSettings

    desktop = RuntimeProfile.desktop(tmp_path)
    assert desktop.validate() is desktop
    server = RuntimeProfile(kind="server", data_dir=tmp_path)
    with pytest.raises(NodestroError) as error:
        server.validate()
    assert error.value.code == "profile.invalid"
    assert _fields(error) == ["kind"]
    assert "nodestro-server" in str(error.value)
    assert all(item.replace("_", " ") in str(error.value) for item in SERVER_REQUIREMENTS)
    settings = ServerSettings(
        database_url="host=db",
        public_origin="https://nodestro.example.com",
        oidc_issuer="https://login.example.com",
        oidc_client_id="nodestro",
        oidc_client_secret="secret",
        secret_key=b"k" * 32,
    )
    assert server.validate(settings) is server
    assert PROFILE_KINDS == ("desktop", "server", "local-container")


# ---- the environment ----------------------------------------------------------------------


def test_an_empty_environment_means_the_desktop_profile_on_the_usual_directory() -> None:
    profile = RuntimeProfile.from_env({})
    assert profile == RuntimeProfile.desktop(Path("~/.nodestro").expanduser())
    # Set but empty or blank: the default, as if the variable were missing.
    assert RuntimeProfile.from_env({ENV_PROFILE: "", ENV_DATA_DIR: "  "}) == profile


def test_the_environment_chooses_profile_and_data_directory(tmp_path: Path) -> None:
    profile = RuntimeProfile.from_env({ENV_PROFILE: " Desktop ", ENV_DATA_DIR: f" {tmp_path} "})
    assert profile == RuntimeProfile.desktop(tmp_path)
    server = RuntimeProfile.from_env({ENV_PROFILE: "SERVER", ENV_DATA_DIR: str(tmp_path)})
    assert server.kind == "server"
    assert server.data_dir == tmp_path


def test_invalid_environment_values_are_refused_all_at_once() -> None:
    with pytest.raises(NodestroError) as error:
        RuntimeProfile.from_env({ENV_PROFILE: "production"})
    assert error.value.code == "profile.invalid"
    assert error.value.params["problems"] == [
        {
            "field": ENV_PROFILE,
            "value": "production",
            "reason": "NODESTRO_PROFILE='production' is not a profile (choose desktop, server or local-container)",
        }
    ]
    with pytest.raises(NodestroError) as error:
        RuntimeProfile.from_env({ENV_PROFILE: "desktop,server", ENV_DATA_DIR: "data\x00dir"})
    assert _fields(error) == [ENV_PROFILE, ENV_DATA_DIR]


def test_from_env_reads_the_process_environment_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_PROFILE, "desktop")
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path))
    assert RuntimeProfile.from_env() == RuntimeProfile.desktop(tmp_path)
    monkeypatch.setenv(ENV_PROFILE, "nonsense")
    with pytest.raises(NodestroError, match="NODESTRO_PROFILE"):
        RuntimeProfile.from_env()


def test_a_data_directory_given_separately_must_be_the_profiles(tmp_path: Path) -> None:
    profile = RuntimeProfile.desktop(tmp_path / "data")
    assert profile.matches(tmp_path / "data") is profile
    assert profile.matches(f"{tmp_path}/./data/") is profile
    assert profile.matches(tmp_path / "other" / ".." / "data") is profile
    with pytest.raises(NodestroError) as error:
        profile.matches(tmp_path / "elsewhere")
    assert error.value.code == "profile.invalid"
    assert _fields(error) == ["data_dir"]


# ---- the workspace ------------------------------------------------------------------------


def test_existing_workspace_calls_open_the_desktop_profile(tmp_path: Path) -> None:
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        assert workspace.profile == RuntimeProfile.desktop(tmp_path / "data")
        assert workspace.data_dir == tmp_path / "data"
        assert (tmp_path / "data" / DATABASE).is_file()
    with Workspace(str(tmp_path / "data"), integrations=[]) as workspace:
        assert workspace.data_dir == tmp_path / "data"


def test_workspace_calls_without_a_profile_ignore_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only an explicit profile is taken from the environment; ``Workspace(data_dir)`` stays the desktop."""
    monkeypatch.setenv(ENV_PROFILE, "server")
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "from-env"))
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        assert workspace.profile.kind == "desktop"
        assert workspace.data_dir == tmp_path / "data"
    assert not (tmp_path / "from-env").exists()


def test_a_workspace_opens_from_a_profile(tmp_path: Path) -> None:
    profile = RuntimeProfile.desktop(tmp_path / "data")
    with Workspace(profile=profile, integrations=[]) as workspace:
        assert workspace.profile is profile
        assert workspace.data_dir == tmp_path / "data"
    with Workspace(tmp_path / "data" / ".", profile=profile, integrations=[]) as workspace:
        assert workspace.profile is profile


def test_a_workspace_needs_a_data_directory_or_a_profile() -> None:
    with pytest.raises(TypeError):
        Workspace()


def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
    }


def test_a_contradicting_data_directory_is_refused_before_anything_is_created(tmp_path: Path) -> None:
    profile = RuntimeProfile.desktop(tmp_path / "data")
    with pytest.raises(NodestroError) as error:
        Workspace(tmp_path / "other", profile=profile, integrations=[])
    assert error.value.code == "profile.invalid"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "make_profile",
    [
        lambda data: RuntimeProfile(kind="server", data_dir=data),
        lambda data: RuntimeProfile.from_env({ENV_PROFILE: "server", ENV_DATA_DIR: str(data)}),
    ],
    ids=["explicit", "environment"],
)
def test_a_server_workspace_without_tenant_storage_is_refused_without_side_effects(
    tmp_path: Path, make_profile: object
) -> None:
    profile = make_profile(tmp_path / "new")  # type: ignore[operator]
    with pytest.raises(NodestroError) as error:
        Workspace(profile=profile, integrations=[])
    assert error.value.code == "profile.invalid" and _fields(error) == ["storage"]
    assert not (tmp_path / "new").exists()

    # On an existing desktop data directory nothing is touched either: no lock, no database change.
    existing = tmp_path / "existing"
    Workspace(existing, integrations=[]).close()
    before = _snapshot(existing)
    with pytest.raises(NodestroError) as error:
        Workspace(existing, profile=make_profile(existing), integrations=[])  # type: ignore[operator]
    assert error.value.code == "profile.invalid"
    assert _snapshot(existing) == before
    # The refused attempt held no lock: the desktop opens the directory right away.
    with Workspace(existing, integrations=[]) as workspace:
        assert workspace.profile.kind == "desktop"
    assert {LOCK_FILE, DATABASE} <= set(os.listdir(existing))


# ---- the local container (Nodestro Local Docker) ------------------------------------------


@pytest.mark.parametrize("value", ["local-container", "LOCAL-CONTAINER", " local_container "])
def test_the_environment_chooses_the_local_container(value: str, tmp_path: Path) -> None:
    profile = RuntimeProfile.from_env({ENV_PROFILE: value, ENV_DATA_DIR: str(tmp_path)})
    assert profile.kind == "local-container"
    assert profile.validate() is profile


def test_a_local_container_workspace_is_one_person_on_sqlite_with_containers_only(tmp_path: Path) -> None:
    from nodestro_runtime.container_images import RuntimeImageSandbox
    from nodestro_runtime.workspace import LOCAL_TENANT

    profile = RuntimeProfile(kind="local-container", data_dir=tmp_path / "data")
    with Workspace(profile=profile, integrations=[]) as workspace:
        assert workspace.tenant_id == LOCAL_TENANT
        assert (tmp_path / "data" / DATABASE).is_file() and (tmp_path / "data" / LOCK_FILE).exists()
        assert workspace.sandbox_policy.profile == "local-container"
        # Tool code runs in containers of the runtime image (no bind mounts: the daemon is the host's).
        assert isinstance(workspace.tools.registry.sandbox, RuntimeImageSandbox)
        assert workspace.python.containers()
        settings = workspace.settings()
        assert settings.sandbox == "docker"
        assert settings.builtin_sandbox.available is False and settings.builtin_sandbox.reason == "server"
        assert "Local Docker" in settings.builtin_sandbox.detail
        with pytest.raises(NodestroError) as error:
            from nodestro_runtime.schemas import SettingsIn

            workspace.update_settings(SettingsIn(sandbox="subprocess"))
        assert error.value.code == "sandbox.unavailable" and "Local Docker" in str(error.value)
