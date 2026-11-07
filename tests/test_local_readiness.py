"""Missing local models: one diagnosis for every action (cause, allowed actions per mode, permission and the strict
offline mode, alternatives), downloads that attach instead of starting twice, and unattended runs that only report."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import sys
import threading
from typing import Any

import pytest
from test_local_models import FAKE_OLLAMA

from nodestro_runtime import local_registry
from nodestro_runtime.engine import provider_problem
from nodestro_runtime.local_readiness import (
    UNAVAILABLE,
    ModelNeeds,
    alternatives,
    check_override,
    diagnose,
    readiness,
    require_ready,
)
from nodestro_runtime.local_registry import RegistryError
from nodestro_runtime.local_service import InstalledModel
from nodestro_runtime.preflight import preflight
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import SettingsIn, Workflow
from nodestro_runtime.workspace import Workspace


@pytest.fixture(autouse=True)
def _no_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here reaches the Ollama registry, which downloads and readiness checks read (RT-062, RT-064)."""

    def refuse(url: str, accept: str, limit: int) -> bytes:
        raise RegistryError("the tests do not reach the registry")

    monkeypatch.setattr(local_registry, "get", refuse)
    local_registry.clear_cache()


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Workspace]:
    """A desktop workspace whose managed Ollama is a fake with ``qwen3.5:4b`` and an embedding model installed."""
    if sys.platform == "win32":
        pytest.skip("the fake Ollama binary is a shebang script")
    binary = tmp_path / "bin" / "ollama"
    binary.parent.mkdir()
    binary.write_text(FAKE_OLLAMA.format(python=sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("NODESTRO_OLLAMA_BINARY", str(binary))
    workspace = Workspace(tmp_path / "data", integrations=[])
    workspace.local_models._hardware = workspace.local_models._hardware_source().model_copy(
        update={"ram_bytes": 64 * 10**9}
    )
    try:
        yield workspace
    finally:
        workspace.close()


def _plenty(monkeypatch: pytest.MonkeyPatch, free: int | None = 10**12) -> None:
    """``free`` bytes in the models folder for the readiness check and for a download's own disk check, whatever
    the disk of the test machine has free."""
    monkeypatch.setattr("nodestro_runtime.local_readiness._free_bytes", lambda folder: free)
    monkeypatch.setattr("nodestro_runtime.local_service._free_bytes", lambda folder: free)


def test_an_installed_model_is_ready_and_a_cloud_model_is_not_checked(managed: Workspace) -> None:
    assert diagnose(managed, "ollama:qwen3.5:4b", action="architect", can_install=True) is None
    assert diagnose(managed, "anthropic:claude-sonnet-4-5", action="architect", can_install=True) is None


def test_a_missing_catalog_model_offers_its_download_with_size_and_space(
    managed: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plenty(monkeypatch)
    problem = diagnose(managed, "ollama:qwen3.5:9b", action="architect", can_install=True, switchable=True)
    assert problem is not None
    assert problem.cause == "model_missing" and problem.location == "this_computer" and problem.mode == "managed"
    assert problem.actions == ["download", "choose_model"] and problem.blocked is None
    assert problem.catalog and problem.download_bytes and problem.memory_bytes and problem.free_bytes == 10**12
    assert problem.fits is True and problem.model == "ollama:qwen3.5:9b" and problem.name == "qwen3.5:9b"


def test_too_little_space_is_its_own_cause_without_a_download(
    managed: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plenty(monkeypatch, free=1000)
    problem = diagnose(managed, "ollama:qwen3.5:9b", action="assistant", can_install=True)
    assert problem is not None and problem.cause == "disk_full"
    assert "download" not in problem.actions and problem.actions == ["retry", "open_settings"]


def test_hardware_that_is_too_small_is_named(managed: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    _plenty(monkeypatch)
    managed.local_models._hardware = managed.local_models._hardware.model_copy(update={"ram_bytes": 2 * 10**9})
    problem = diagnose(managed, "ollama:qwen3.5:9b", action="architect", can_install=True)
    assert problem is not None and problem.cause == "hardware_insufficient" and problem.fits is False
    assert problem.actions[0] == "download"  # still possible, with the warning


def test_without_install_permission_the_person_asks_an_administrator(
    managed: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plenty(monkeypatch)
    problem = diagnose(managed, "ollama:qwen3.5:9b", action="assistant", can_install=False, switchable=True)
    assert problem is not None and problem.cause == "model_missing" and problem.blocked == "permission"
    assert problem.actions == ["choose_model", "ask_admin"]


def test_the_strict_offline_mode_never_offers_a_download(managed: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    _plenty(monkeypatch)
    managed.update_settings(SettingsIn(strict_offline=True))
    problem = diagnose(managed, "ollama:qwen3.5:9b", action="architect", can_install=True)
    assert problem is not None and problem.offline and problem.blocked == "offline"
    assert "download" not in problem.actions


def test_invalid_names_and_cloud_models_are_not_downloads(managed: Workspace) -> None:
    for model in ("ollama:../etc", "ollama:qwen3:480b-cloud"):
        problem = diagnose(managed, model, action="architect", can_install=True)
        assert problem is not None and problem.cause == "model_name_invalid" and problem.actions == []


def test_an_installed_model_without_the_needed_capability(managed: Workspace) -> None:
    problem = diagnose(managed, "ollama:qwen3-embedding:0.6b", action="assistant", can_install=True, needs=ModelNeeds())
    assert problem is not None and problem.cause == "capability_missing"
    assert problem.missing_capabilities == ["completion"]
    index = diagnose(
        managed, "ollama:qwen3.5:4b", action="knowledge_index", can_install=True, needs=ModelNeeds(kind="embedding")
    )
    assert index is not None and index.missing_capabilities == ["embedding"]


def test_a_missing_runtime_offers_the_server_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NODESTRO_OLLAMA_BINARY", raising=False)
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        problem = diagnose(workspace, "ollama:qwen3.5:9b", action="architect", can_install=True)
        assert problem is not None and problem.cause == "runtime_missing" and problem.actions == ["setup_server"]
        workspace.update_settings(SettingsIn(strict_offline=True))
        offline = diagnose(workspace, "ollama:qwen3.5:9b", action="architect", can_install=True)
        assert offline is not None and offline.actions == ["open_settings"] and offline.blocked == "offline"


def test_an_external_server_that_does_not_answer(managed: Workspace) -> None:
    managed.update_settings(SettingsIn(providers={"ollama": {"base_url": "http://127.0.0.1:9/v1"}}))
    problem = diagnose(managed, "ollama:qwen3.5:9b", action="architect", can_install=True)
    assert problem is not None and problem.cause == "server_unreachable" and problem.mode == "external"
    assert problem.location == "this_computer" and problem.actions == ["retry", "open_settings"]
    assert managed.local_models._managed is None  # never started instead


def test_a_remote_server_is_named_by_its_host_and_never_this_computer(managed: Workspace) -> None:
    managed.update_settings(SettingsIn(providers={"ollama": {"base_url": "http://192.0.2.10:11434/v1"}}))
    managed.local_models._client_factory = lambda url: _Client(running=True, models=[])
    problem = diagnose(managed, "ollama:qwen3.5:9b", action="architect", can_install=True)
    assert problem is not None and problem.location == "remote" and problem.host == "192.0.2.10"
    assert problem.fits is None  # this computer's hardware says nothing about that server
    assert problem.actions == ["download"]


def test_a_nodestro_server_never_offers_a_download_only_the_operator(tmp_path: Path) -> None:
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        service = workspace.local_models
        service.profile = "server"
        problem = diagnose(workspace, "ollama:qwen3.5:9b", action="assistant", can_install=True, switchable=True)
        assert problem is not None and problem.cause == "server_missing" and problem.location == "server"
        assert problem.blocked == "operator" and "download" not in problem.actions
        assert problem.actions[-1] == "ask_admin"
        workspace.update_settings(SettingsIn(providers={"ollama": {"base_url": "http://10.0.0.5:11434/v1"}}))
        service._client_factory = lambda url: _Client(running=True, models=[])
        missing = diagnose(workspace, "ollama:qwen3.5:9b", action="assistant", can_install=True)
        assert missing is not None and missing.cause == "model_missing" and missing.location == "server"
        assert missing.actions == ["ask_admin"]


def test_a_running_download_is_followed_instead_of_offered_again(
    managed: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plenty(monkeypatch)
    release = threading.Event()
    service = managed.local_models
    service.start()
    real = service.client

    class Pulling:
        def __getattr__(self, name: str) -> Any:
            return getattr(real(), name)

        def pull(self, name: str, cancel: Any = None) -> Iterator[Any]:
            release.wait(10)
            return iter(())

    monkeypatch.setattr(service, "client", lambda: Pulling())
    first = service.pull("qwen3.5:9b", attach=True)
    second = service.pull("ollama:qwen3.5:9b", attach=True)  # a second click, another tab
    assert second.id == first.id
    with pytest.raises(NodestroError) as refused:
        service.pull("qwen3.5:9b")  # without attach the guard still refuses
    assert refused.value.code == "local.job_running"
    monkeypatch.setattr(service, "client", real)
    problem = diagnose(managed, "ollama:qwen3.5:9b", action="architect", can_install=True)
    assert problem is not None and problem.job is not None and problem.job.id == first.id
    release.set()


def test_alternatives_are_ready_and_capable_and_never_chosen_silently(managed: Workspace) -> None:
    found = alternatives(managed, ModelNeeds(capabilities=["tools"]), exclude="ollama:qwen3.5:9b")
    names = [item.model for item in found]
    assert "ollama:qwen3.5:4b" in names and "ollama:qwen3-embedding:0.6b" not in names
    assert all(item.local for item in found if item.provider == "ollama")
    embedding = alternatives(managed, ModelNeeds(kind="embedding"))
    assert [item.model for item in embedding] == ["ollama:qwen3-embedding:0.6b"]
    # A model chosen for one action must be one of them; the configured one always passes.
    assert check_override(managed, "ollama:qwen3.5:4b", "ollama:qwen3.5:9b") == "ollama:qwen3.5:4b"
    assert check_override(managed, "ollama:qwen3.5:9b", "ollama:qwen3.5:9b") == "ollama:qwen3.5:9b"
    with pytest.raises(NodestroError) as refused:
        check_override(managed, "ollama:any-model:70b", "ollama:qwen3.5:9b")
    assert refused.value.code == "model.choice_invalid"


def test_the_refusal_carries_the_problem_and_the_alternatives(
    managed: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plenty(monkeypatch)
    with pytest.raises(NodestroError) as refused:
        require_ready(managed, "ollama:qwen3.5:9b", action="architect", can_install=True, switchable=True)
    assert refused.value.code == UNAVAILABLE
    params = refused.value.params
    assert params["cause"] == "model_missing" and params["problem"]["actions"] == ["download", "choose_model"]
    assert "ollama:qwen3.5:4b" in [item["model"] for item in params["alternatives"]]
    ready = readiness(managed, "ollama:qwen3.5:4b", action="architect", can_install=True, switchable=True)
    assert ready.ready and ready.problem is None and ready.alternatives == []


def test_choose_model_is_left_out_when_nothing_else_is_ready(
    managed: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plenty(monkeypatch)
    monkeypatch.setattr("nodestro_runtime.local_readiness.alternatives", lambda *a, **k: [])
    found = readiness(managed, "ollama:qwen3.5:9b", action="architect", can_install=True, switchable=True)
    assert found.problem is not None and found.problem.actions == ["download"]


def _workflow(model: str) -> Workflow:
    return Workflow.model_validate(
        {
            "id": "w",
            "name": "Local",
            "nodes": [
                {"id": "s", "type": "start"},
                {"id": "a", "type": "agent", "agent": {"name": "a", "model": model, "task": "x"}},
                {"id": "o", "type": "output"},
            ],
            "edges": [{"id": "e1", "source": "s", "target": "a"}, {"id": "e2", "source": "a", "target": "o"}],
        }
    )


def test_unattended_runs_are_refused_from_the_model_folder_without_starting_or_downloading(
    managed: Workspace,
) -> None:
    service = managed.local_models
    assert service.ollama_url() is None
    problems = preflight(managed, _workflow("ollama:qwen3.5:9b")).problems
    assert [(item.code, item.params.get("model")) for item in problems if item.code.startswith("check.local")] == [
        ("check.local_model_missing", "ollama:qwen3.5:9b")
    ]
    assert service._managed is None and service.jobs() == []  # nothing started, nothing downloaded
    # A model in the folder passes the same check.
    folder = service.models_dir()
    manifest = folder / "manifests" / "registry.ollama.ai" / "library" / "qwen3.5" / "9b"
    manifest.parent.mkdir(parents=True)
    (folder / "blobs").mkdir()
    layer = folder / "blobs" / ("sha256-" + "a" * 64)
    layer.write_bytes(b"x")
    manifest.write_text(
        '{"schemaVersion": 2, "config": {"digest": "sha256:' + "a" * 64 + '", "size": 1}, "layers": []}'
    )
    problems = preflight(managed, _workflow("ollama:qwen3.5:9b")).problems
    assert not [item for item in problems if item.code == "check.local_model_missing"]


def test_a_404_of_the_local_model_server_is_a_missing_model_not_an_api_error() -> None:
    from orkexa import InvalidRequestError

    exc = InvalidRequestError(
        "ollama: NotFoundError: Error code: 404 - {'error': {'message': 'model \"qwen3.5:9b\" not found, try "
        "pulling it first'}}",
        provider="ollama",
        status_code=404,
    )
    found = provider_problem(exc, str(exc))
    assert found.code == "run.local_model_missing" and found.params["model"] == "qwen3.5:9b"
    other = InvalidRequestError("openai: NotFoundError", provider="openai", status_code=404)
    assert provider_problem(other, str(other)).code == "run.api_error"


class _Client:
    """An Ollama client double for servers that are not this computer's."""

    def __init__(self, *, running: bool, models: list[InstalledModel]) -> None:
        self.running_ = running
        self.models = models

    def is_running(self) -> bool:
        return self.running_

    def list_models(self) -> list[Any]:
        return []

    def show(self, name: str) -> Any:
        from orkexa.local import OllamaError

        raise OllamaError("model_missing", name)

    def running(self) -> list[Any]:
        return []
