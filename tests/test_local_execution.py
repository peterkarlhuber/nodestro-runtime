"""The hardware-aware execution of local models (RT-050/RT-051): the CPU saving mode, context window, advice, speed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orkexa.local import LocalModel, ModelDetails
import pytest

from nodestro_runtime.agent_builder import _effort, _options, reasoning_reduction
from nodestro_runtime.local_models import LOCAL_DEFAULT_MODEL, Hardware
from nodestro_runtime.local_service import LOCAL_CONTEXT_LENGTH, LocalModelService
from nodestro_runtime.providers import Connection
from nodestro_runtime.schemas import AgentConfig, SettingsIn
from nodestro_runtime.workspace import Workspace

GB = 1024**3
CPU = Hardware(system="linux", machine="x86_64", cpus=4, ram_bytes=16 * GB)
GPU = Hardware(system="linux", machine="x86_64", cpus=8, ram_bytes=32 * GB, gpu="nvidia", gpu_memory_bytes=24 * GB)
THINKING = {"reasoning": "supported"}


def _agent(**fields: Any) -> AgentConfig:
    return AgentConfig.model_validate({"name": "A", "model": LOCAL_DEFAULT_MODEL, "task": "x", **fields})


def _connection(*, saving: bool, reported: dict[str, str] | None = None) -> Connection:
    return Connection(
        provider="ollama",
        client=object(),
        reported=lambda _model: reported if reported is not None else THINKING,
        reasoning_saving=saving,
    )


# ---- the CPU saving mode -----------------------------------------------------------------------------------------


def test_the_cpu_saving_mode_answers_effort_medium_without_thinking() -> None:
    medium = _agent()
    assert medium.effort == "medium"
    saving = _connection(saving=True)
    assert reasoning_reduction(medium, saving, LOCAL_DEFAULT_MODEL) == "cpu_saving"
    assert _effort(medium, saving, LOCAL_DEFAULT_MODEL) is None
    assert _options(medium, "ollama", saving, LOCAL_DEFAULT_MODEL).request_options == {"reasoning_effort": "none"}
    # Without the mode (a GPU, or the setting "full") the effort goes out as it is.
    full = _connection(saving=False)
    assert reasoning_reduction(medium, full, LOCAL_DEFAULT_MODEL) is None
    assert _effort(medium, full, LOCAL_DEFAULT_MODEL) == "medium"


def test_an_explicitly_thorough_agent_is_never_reduced() -> None:
    saving = _connection(saving=True)
    high = _agent(effort="high")
    assert reasoning_reduction(high, saving, LOCAL_DEFAULT_MODEL) is None
    assert _effort(high, saving, LOCAL_DEFAULT_MODEL) == "high"
    assert _options(high, "ollama", saving, LOCAL_DEFAULT_MODEL).request_options == {}
    # The agent's own reasoning option wins over the mode.
    own = _agent(request_options={"reasoning_effort": "medium"})
    assert reasoning_reduction(own, saving, LOCAL_DEFAULT_MODEL) is None
    assert _options(own, "ollama", saving, LOCAL_DEFAULT_MODEL).request_options == {"reasoning_effort": "medium"}


def test_low_effort_and_models_that_do_not_think_are_no_reduction() -> None:
    saving = _connection(saving=True)
    # Effort low never thinks (what low means): no reduction to report.
    assert reasoning_reduction(_agent(effort="low"), saving, LOCAL_DEFAULT_MODEL) is None
    plain = _connection(saving=True, reported={"reasoning": "unsupported"})
    assert reasoning_reduction(_agent(), plain, "ollama:plain:1b") is None
    # Cloud providers are never touched.
    cloud = Connection(provider="anthropic", client=object(), reasoning_saving=True)
    config = AgentConfig.model_validate({"name": "A", "model": "anthropic:claude-haiku-5-5", "task": "x"})
    assert reasoning_reduction(config, cloud, "anthropic:claude-haiku-5-5") is None


def test_the_workspace_saves_only_without_a_gpu_and_the_setting_turns_it_off(tmp_path: Path) -> None:
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        service = workspace.local_models
        assert service.mode == "managed" and service.ollama_url() is None
        service._hardware = CPU
        assert workspace.settings().local_reasoning == "auto"
        assert workspace.reasoning_saving()
        assert workspace.update_settings(SettingsIn(local_reasoning="full")).local_reasoning == "full"
        assert not workspace.reasoning_saving()
        workspace.update_settings(SettingsIn(local_reasoning="auto"))
        service._hardware = GPU
        assert not workspace.reasoning_saving()


def test_an_external_server_on_another_computer_is_never_assumed_slow(tmp_path: Path) -> None:
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        workspace.store.put_raw("settings", "app", {"providers": {"ollama": {"base_url": "http://10.1.2.3:11434/v1"}}})
        service = workspace.local_models
        service._hardware = CPU
        service._client_factory = lambda url: FakeOllama(url)  # nothing loaded: /api/ps tells nothing
        assert service.accelerated() is None
        assert not workspace.reasoning_saving()


# ---- status, context window and advice ---------------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], Any] = {}

    def get_raw(self, collection: str, key: str) -> Any:
        return self.data.get((collection, key))

    def put_raw(self, collection: str, key: str, value: Any) -> None:
        self.data[(collection, key)] = value


class _Http:
    def __init__(self, owner: FakeOllama) -> None:
        self.owner = owner

    def request(self, method: str, path: str, body: Any = None, **_: Any) -> Any:
        assert (method, path) == ("POST", "/api/generate")
        self.owner.generated.append(body)
        self.owner.loaded = [
            {"name": body["model"], "size": 4_000_000_000, "size_vram": self.owner.vram, "context_length": 32768}
        ]
        return {
            "eval_count": 96,
            "eval_duration": 20_000_000_000,
            "prompt_eval_count": 40,
            "prompt_eval_duration": 2_000_000_000,
        }


class FakeOllama:
    """The parts of ``orkexa.local.OllamaClient`` the service uses."""

    def __init__(self, url: str = "http://127.0.0.1:11434", *, loaded: list[dict[str, Any]] | None = None) -> None:
        self.url = url
        self.loaded = loaded or []
        self.vram = 0
        self.generated: list[dict[str, Any]] = []
        self.http = _Http(self)

    def version(self, **_: Any) -> str:
        return "0.40.14"

    def is_running(self) -> bool:
        return True

    def list_models(self) -> list[LocalModel]:
        return [
            LocalModel(name="qwen3.5:4b", digest="d" * 64, size=3_300_000_000),
            LocalModel(name="huge:70b", digest="e" * 64, size=40 * GB),
            LocalModel(name="qwen3-embedding:0.6b", digest="f" * 64, size=640_000_000),
        ]

    def show(self, name: str) -> ModelDetails:
        if name.startswith("qwen3-embedding"):
            return ModelDetails(name=name, capabilities=("embedding",))
        return ModelDetails(name=name, capabilities=("completion", "tools", "thinking"))

    def running(self) -> list[dict[str, Any]]:
        return list(self.loaded)


def _service(tmp_path: Path, client: FakeOllama, *, url: str | None, hardware: Hardware = CPU) -> LocalModelService:
    return LocalModelService(
        tmp_path,
        "desktop",
        store=_Store(),
        offline=lambda: False,
        external_url=lambda: url,
        hardware=lambda: hardware,
        environ={},
        client_factory=lambda _url: client,
    )


def test_an_external_server_reports_its_context_window_from_a_loaded_model(tmp_path: Path) -> None:
    client = FakeOllama(
        loaded=[
            {"name": "qwen3-embedding:0.6b", "size": 600, "size_vram": 0, "context_length": 512},
            {"name": "qwen3.5:4b", "size": 4000, "size_vram": 0, "context_length": 4096},
        ]
    )
    service = _service(tmp_path, client, url="http://127.0.0.1:11434/v1")
    status = service.status()
    assert status.mode == "external" and status.running
    # The embedding model's own small window does not count; the chat model's does.
    assert (status.context_length, status.context_source) == (4096, "server")
    assert status.accelerated is False  # nothing of the model is in GPU memory
    codes = {hint.code: hint for hint in service.advice(status, reasoning_saving=True)}
    small = codes["local.context_small"]
    assert small.severity == "warning" and small.value == 4096 and small.recommended == LOCAL_CONTEXT_LENGTH
    assert "local.cpu_only" in codes and "local.reasoning_saving" in codes
    too_large = codes["local.model_too_large"]
    assert too_large.model == "huge:70b"


def test_an_unmeasured_external_server_says_its_window_is_unknown(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeOllama(), url="http://192.168.1.20:11434/v1")
    status = service.status()
    assert (status.context_length, status.context_source) == (None, "unknown")
    assert status.accelerated is None  # another computer: its hardware is not this one's
    codes = [hint.code for hint in service.advice(status)]
    assert "local.context_unknown" in codes and "local.cpu_only" not in codes


def test_a_model_mostly_in_gpu_memory_counts_as_accelerated(tmp_path: Path) -> None:
    client = FakeOllama(loaded=[{"name": "qwen3.5:4b", "size": 4000, "size_vram": 3000, "context_length": 32768}])
    service = _service(tmp_path, client, url="http://192.168.1.20:11434/v1")
    status = service.status()
    assert status.accelerated is True and status.context_length == 32768
    assert [hint.code for hint in service.advice(status, reasoning_saving=True)] == ["local.model_too_large"]


def test_the_managed_server_has_its_configured_window(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeOllama(), url=None, hardware=GPU)
    status = service.status()
    assert status.mode == "managed" and not status.running
    assert (status.context_length, status.context_source) == (LOCAL_CONTEXT_LENGTH, "configured")
    assert status.accelerated is True
    assert service.context_window() == LOCAL_CONTEXT_LENGTH


def test_little_memory_is_a_warning(tmp_path: Path) -> None:
    small = Hardware(system="linux", machine="x86_64", cpus=2, ram_bytes=4 * GB)
    service = _service(tmp_path, FakeOllama(), url=None, hardware=small)
    codes = {hint.code: hint for hint in service.advice(service.status())}
    assert codes["local.memory_low"].value == 4 * GB and codes["local.cpu_only"].value == 2


# ---- measured speed ----------------------------------------------------------------------------------------------


def _wait(service: LocalModelService, job_id: str) -> Any:
    import time

    for _ in range(200):
        job = service.job(job_id)
        if job.state != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("the job did not finish")


def test_the_speed_is_measured_never_estimated(tmp_path: Path) -> None:
    client = FakeOllama()
    service = _service(tmp_path, client, url="http://127.0.0.1:11434/v1")
    assert all(model.speed is None for model in service.status().models)
    job = _wait(service, service.benchmark("qwen3.5:4b").id)
    assert job.state == "done" and job.kind == "benchmark" and job.message == "4.8 tokens/s"
    # A thinking model is measured without its thinking phase, with a short fixed request.
    assert client.generated[0]["think"] is False and client.generated[0]["options"]["num_predict"] == 96
    speed = next(model for model in service.status().models if model.name == "qwen3.5:4b").speed
    assert speed is not None
    assert speed.tokens_per_second == 4.8 and speed.prompt_tokens_per_second == 20.0
    assert speed.processor == "cpu" and speed.context_length == 32768
    hints = [hint for hint in service.advice(service.status()) if hint.code == "local.model_slow"]
    assert [(hint.model, hint.value) for hint in hints] == [("qwen3.5:4b", 48)]


def test_only_installed_chat_models_can_be_measured(tmp_path: Path) -> None:
    from nodestro_runtime.problems import NodestroError

    service = _service(tmp_path, FakeOllama(), url="http://127.0.0.1:11434/v1")
    with pytest.raises(NodestroError) as error:
        service.benchmark("qwen3-embedding:0.6b")
    assert error.value.code == "local.not_chat_model"


# ---- the run records a reduction -----------------------------------------------------------------------------------


async def test_a_run_records_a_reduced_reasoning_on_the_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from nodestro_runtime.agent_builder import AgentBuilder
    from nodestro_runtime.schemas import RunIn, WorkflowIn
    from nodestro_runtime.testing import ScriptedClient, text

    original = AgentBuilder.build

    def build(self: AgentBuilder, config: AgentConfig, **kwargs: Any) -> Any:
        # A scripted client stands in for the model; the step is marked as the CPU saving mode would mark it.
        return replace(original(self, config, **kwargs), reasoning_reduced="cpu_saving")

    monkeypatch.setattr(AgentBuilder, "build", build)
    fake = ScriptedClient({"A": [text("ok")]})
    flow = WorkflowIn.model_validate(
        {
            "name": "Saving",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "A"}},
                {"id": "out", "type": "output"},
            ],
            "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
        }
    )
    with Workspace(tmp_path / "data", client_factory=lambda _p: fake, integrations=[]) as workspace:
        saved = workspace.save_workflow(flow)
        started = await workspace.run(saved, RunIn(input="x"))
        done = await workspace.wait(started.id, timeout=20)
        assert done.status == "succeeded", done.error
        assert done.nodes["a"].reasoning_reduced == "cpu_saving"
        assert done.nodes["start"].reasoning_reduced is None


def test_the_agent_test_reports_a_reduced_reasoning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from nodestro_runtime.agent_builder import AgentBuilder
    from nodestro_runtime.agent_test import AgentTestIn
    from nodestro_runtime.testing import ScriptedClient, text

    original = AgentBuilder.build
    monkeypatch.setattr(
        AgentBuilder,
        "build",
        lambda self, config, **kwargs: replace(original(self, config, **kwargs), reasoning_reduced="cpu_saving"),
    )
    fake = ScriptedClient({"A": [text("ok")]})
    with Workspace(tmp_path / "data", client_factory=lambda _p: fake, integrations=[]) as workspace:
        result, _tracker = workspace.tester.run(AgentTestIn(agent=AgentConfig(name="A", system_prompt="A"), input="x"))
        assert result.status == "succeeded" and result.reasoning_reduced == "cpu_saving"


def test_the_workflow_check_names_a_small_window_and_the_saving_mode(tmp_path: Path) -> None:
    from nodestro_runtime.preflight import preflight
    from nodestro_runtime.schemas import Workflow

    workflow = Workflow.model_validate(
        {
            "id": "wf",
            "name": "Local",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "model": "ollama:qwen3.5:4b"}},
                {
                    "id": "b",
                    "type": "agent",
                    "label": "B",
                    "agent": {"name": "B", "model": "ollama:qwen3.5:4b", "effort": "high"},
                },
                {"id": "out", "type": "output"},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "a"},
                {"id": "e2", "source": "a", "target": "b"},
                {"id": "e3", "source": "b", "target": "out"},
            ],
        }
    )
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        workspace.store.put_raw("settings", "app", {"providers": {"ollama": {"base_url": "http://127.0.0.1:11434/v1"}}})
        client = FakeOllama(loaded=[{"name": "qwen3.5:4b", "size": 4000, "size_vram": 0, "context_length": 4096}])
        workspace.local_models._client_factory = lambda _url: client
        workspace.local_models._hardware = CPU
        issues = {issue.code: issue for issue in preflight(workspace, workflow).problems}
        small = issues["check.local_context_small"]
        assert small.level == "warning" and small.params == {"tokens": 4096, "recommended": LOCAL_CONTEXT_LENGTH}
        limits = [issue for issue in preflight(workspace, workflow).problems if issue.code == "check.local_max_tokens"]
        assert {issue.node_id for issue in limits} == {"a", "b"}  # 8,000 answer tokens do not fit 4,096
        saving = issues["check.local_reasoning_saving"]
        assert saving.level == "info" and saving.params == {"nodes": "a"}  # effort high stays as it is
        workspace.update_settings(SettingsIn(local_reasoning="full"))
        assert "check.local_reasoning_saving" not in {issue.code for issue in preflight(workspace, workflow).problems}
