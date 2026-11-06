"""Local models: the catalog, the task- and hardware-aware recommendation, the managed Ollama server of a workspace,
default models of new and existing installations, local prices and the strict offline mode."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import re
import sqlite3
import sys
import textwrap
import time

from orkexa.pricing import pricing_basis
import pytest

from nodestro_runtime.catalog import models
from nodestro_runtime.local_models import (
    CATALOG,
    LOCAL_ASSISTANT_MODEL,
    LOCAL_DEFAULT_MODEL,
    TASKS,
    Hardware,
    catalog_model,
    recommend,
)
from nodestro_runtime.local_service import InstalledModel
from nodestro_runtime.preflight import preflight
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import DEFAULT_AGENT_MODEL, SettingsIn, Workflow
from nodestro_runtime.workspace import DATABASE, Workspace

GB = 1_000_000_000


def cpu(ram_gb: int) -> Hardware:
    return Hardware(system="linux", machine="x86_64", cpus=8, ram_bytes=ram_gb * GB)


# ---- catalog and recommendations --------------------------------------------------------------------------------


def test_the_catalog_holds_the_verified_models() -> None:
    names = [model.name for model in CATALOG]
    assert names == [
        "qwen3.5:4b",
        "qwen3.5:9b",
        "qwen3.6:35b-a3b",
        "qwen3.8:27b",
        "ministral-3:3b",
        "ministral-3:8b",
        "ministral-3:14b",
        "devstral-small-2:24b",
        "qwen3-embedding:0.6b",
        "qwen3-embedding:4b",
        "qwen3-embedding:8b",
        "mdenseon:307m",
    ]
    for model in CATALOG:
        assert len(model.digest) == 64 and model.download_bytes > 0
    assert catalog_model("ollama:qwen3.8:27b").min_ollama == "0.32.12"  # type: ignore[union-attr]
    assert catalog_model("llama3:8b") is None


def test_a_16_gb_computer_gets_small_and_medium_models() -> None:
    hardware = cpu(16)
    assert recommend("classify", hardware).model == "ollama:qwen3.5:4b"
    write = recommend("write", hardware)
    assert write.model == "ollama:qwen3.5:9b" and write.quality == "recommended" and not write.installed
    assert write.download_bytes == 6_600_000_000
    analysis = recommend("analysis", hardware)
    # 35B does not fit into 16 GB: the 9B model, with the reason.
    assert analysis.model == "ollama:qwen3.5:9b" and analysis.quality == "acceptable"
    assert "hardware_limit" in analysis.reasons and analysis.better == "ollama:qwen3.6:35b-a3b"
    assert recommend("embedding", hardware).model == "ollama:qwen3-embedding:0.6b"


def test_installed_models_win_and_limits_are_explained() -> None:
    result = recommend("write", cpu(16), ["qwen3.5:4b"])
    assert result.model == "ollama:qwen3.5:4b" and result.installed and result.download_bytes == 0
    assert result.quality == "limited" and result.better == "ollama:qwen3.5:9b"
    # The architect needs at least the 9B model: the installed 4B one is not suggested.
    assert recommend("architect", cpu(16), ["qwen3.5:4b"]).model == "ollama:qwen3.5:9b"


def test_large_models_follow_memory_and_gpu() -> None:
    assert recommend("analysis", cpu(64)).model == "ollama:qwen3.6:35b-a3b"  # a CPU: the MoE model, never dense 27B
    assert "no_gpu" in recommend("analysis", cpu(64)).reasons
    gpu = Hardware(system="linux", machine="x86_64", cpus=8, ram_bytes=32 * GB, gpu="nvidia", gpu_memory_bytes=24 * GB)
    assert recommend("analysis", gpu).model == "ollama:qwen3.8:27b"  # fits into the GPU memory
    tiny = cpu(4)
    assert recommend("write", tiny).quality == "insufficient" and recommend("write", tiny).model is None


def test_reported_capabilities_replace_the_listed_ones() -> None:
    # An installed model that does not report vision is not suggested for documents with images.
    result = recommend("document_vision", cpu(16), ["qwen3.5:9b"], capabilities={"qwen3.5:9b": ["completion", "tools"]})
    assert result.model == "ollama:qwen3.5:4b" and not result.installed
    assert set(TASKS) >= {"classify", "tool_agent", "orchestrator", "judge", "assistant", "embedding"}


def test_the_model_catalog_lists_local_models_without_prices() -> None:
    local = [info for info in models() if info.provider == "ollama"]
    assert [info.model for info in local] == [
        "qwen3.5:4b",
        "qwen3.5:9b",
        "qwen3.6:35b-a3b",
        "qwen3.8:27b",
        "ministral-3:3b",
        "ministral-3:8b",
        "ministral-3:14b",
        "devstral-small-2:24b",
    ]
    assert all(info.price_status == "local" and info.input_usd_per_mtok is None for info in local)


# ---- default models ---------------------------------------------------------------------------------------------


def test_a_new_installation_defaults_to_local_models(tmp_path: Path) -> None:
    with Workspace(tmp_path / "new", integrations=[]) as workspace:
        settings = workspace.settings()
        assert settings.default_model == LOCAL_DEFAULT_MODEL and settings.assistant_model == LOCAL_ASSISTANT_MODEL
        assert settings.default_provider == "ollama" and settings.model_defaults == "local"
        # A model the user chooses wins.
        assert workspace.update_settings(SettingsIn(default_model="anthropic:claude-haiku-5-5")).default_model == (
            "anthropic:claude-haiku-5-5"
        )


def test_an_existing_installation_keeps_its_cloud_defaults(tmp_path: Path) -> None:
    directory = tmp_path / "old"
    with Workspace(directory, integrations=[]) as workspace:
        workspace.update_settings(SettingsIn(default_budget_usd=3.0))
    # An installation of an earlier release: data, but no record of its default models yet.
    connection = sqlite3.connect(directory / DATABASE)
    connection.execute("DELETE FROM nodestro_documents WHERE collection = 'installation'")
    connection.commit()
    connection.close()
    with Workspace(directory, integrations=[]) as workspace:
        assert workspace.settings().default_model == DEFAULT_AGENT_MODEL
        assert workspace.settings().model_defaults == "kept"
    with Workspace(directory, integrations=[]) as workspace:  # and stays so on every later start
        assert workspace.settings().default_model == DEFAULT_AGENT_MODEL


# ---- the managed server -----------------------------------------------------------------------------------------

FAKE_OLLAMA = textwrap.dedent(
    """\
    #!{python}
    import http.server, json, os
    host, port = os.environ["OLLAMA_HOST"].rsplit(":", 1)
    seen = {{"no_cloud": os.environ.get("OLLAMA_NO_CLOUD"), "models": os.environ.get("OLLAMA_MODELS")}}
    TAGS = {{"models": [
        {{"name": "qwen3.5:4b", "model": "qwen3.5:4b", "size": 3300000000,
          "digest": "d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04", "details": {{}}}},
        {{"name": "qwen3-embedding:0.6b", "model": "qwen3-embedding:0.6b", "size": 640000000,
          "digest": "ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d", "details": {{}}}}]}}
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            if self.path == "/api/version": self.reply({{"version": "0.40.14", "seen": seen}})
            elif self.path == "/api/tags": self.reply(TAGS)
            else: self.send_error(404)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{{}}")
            if self.path == "/api/show":
                if body.get("model") not in [m["name"] for m in TAGS["models"]]:
                    self.send_response(404); self.send_header("Content-Length", "28"); self.end_headers()
                    self.wfile.write(b'{{"error": "model not found"}}')
                elif body.get("model", "").startswith("qwen3-embedding"):
                    self.reply({{"capabilities": ["embedding"], "model_info": {{"qwen3.embedding_length": 1024}}}})
                else:
                    self.reply({{"capabilities": ["completion", "tools", "vision", "thinking"], "model_info": {{}}}})
            else: self.send_error(404)
    http.server.ThreadingHTTPServer((host, int(port)), H).serve_forever()
    """
)


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Workspace]:
    if sys.platform == "win32":
        pytest.skip("the fake Ollama binary is a shebang script")
    binary = tmp_path / "bin" / "ollama"
    binary.parent.mkdir()
    binary.write_text(FAKE_OLLAMA.format(python=sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("NODESTRO_OLLAMA_BINARY", str(binary))
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        yield workspace
    finally:
        workspace.close()


def test_the_managed_server_provides_ollama_without_a_saved_url(managed: Workspace) -> None:
    ollama = next(item for item in managed.settings().providers if item.id == "ollama")
    assert ollama.configured and ollama.local_server and ollama.base_url is None
    status = managed.local_models.status()
    assert status.mode == "managed" and status.runtime_bundled and not status.running  # nothing started yet
    connection = managed.connection("ollama")  # an agent needs it: the server starts now
    assert connection.base_url is not None and connection.base_url.startswith("http://127.0.0.1:")
    assert connection.base_url.endswith("/v1")
    # The capabilities come from /api/show of the installed model, not from its name.
    kwargs = connection.agent_kwargs("ollama:qwen3.5:4b")
    assert kwargs["capabilities"]["tools"] == "supported" and kwargs["capabilities"]["image_input"] == "supported"
    assert "capabilities" not in connection.agent_kwargs("ollama:unknown-model:1b")
    status = managed.local_models.status()
    assert status.running and status.version == "0.40.14"
    # Ollama's 4096-token default is too small for agents: the managed server gets a larger context window.
    assert managed.local_models._managed is not None
    assert managed.local_models._managed.environment == {"OLLAMA_CONTEXT_LENGTH": "32768"}
    assert {model.name: model.verified for model in status.models} == {
        "qwen3.5:4b": True,
        "qwen3-embedding:0.6b": True,
    }
    assert {model.name: model.kind for model in status.models}["qwen3-embedding:0.6b"] == "embedding"
    assert managed.local_models.recommend("write").model == "ollama:qwen3.5:4b"  # installed, although limited
    managed.local_models.stop()
    assert not managed.local_models.status().running


def test_a_saved_base_url_means_the_users_own_server(managed: Workspace) -> None:
    managed.update_settings(SettingsIn(providers={"ollama": {"base_url": "http://127.0.0.1:9/v1"}}))
    assert managed.local_models.mode == "external"
    assert managed.connection("ollama").base_url == "http://127.0.0.1:9/v1"
    assert managed.local_models._managed is None  # the managed server was never started


def test_without_a_runtime_ollama_is_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NODESTRO_OLLAMA_BINARY", raising=False)
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        ollama = next(item for item in workspace.settings().providers if item.id == "ollama")
        assert not ollama.configured
        with pytest.raises(NodestroError) as error:
            workspace.connection("ollama")
        assert error.value.code == "local.runtime_missing"
        workflow = Workflow.model_validate(
            {
                "id": "w",
                "name": "Local",
                "nodes": [
                    {"id": "s", "type": "start"},
                    {"id": "a", "type": "agent", "agent": {"name": "A", "model": LOCAL_DEFAULT_MODEL, "task": "x"}},
                    {"id": "o", "type": "output"},
                ],
                "edges": [{"id": "e1", "source": "s", "target": "a"}, {"id": "e2", "source": "a", "target": "o"}],
            }
        )
        codes = {item.code for item in preflight(workspace, workflow).problems}
        assert "check.local_runtime_missing" in codes
        assert "check.model_unpriced" not in codes  # a local model has no external API price to miss
    # An injected client (tests, applications that embed the runtime) is the server: no runtime needed.
    fake = object()
    factory = lambda provider: fake if provider == "ollama" else None  # noqa: E731
    with Workspace(tmp_path / "injected", integrations=[], client_factory=factory) as workspace:
        ollama = next(item for item in workspace.settings().providers if item.id == "ollama")
        assert ollama.configured and not ollama.local_server
        codes = {item.code for item in preflight(workspace, workflow).problems}
        assert "check.local_runtime_missing" not in codes


def test_downloads_are_refused_in_the_strict_offline_mode(managed: Workspace) -> None:
    managed.update_settings(SettingsIn(strict_offline=True))
    with pytest.raises(NodestroError) as error:
        managed.local_models.pull("qwen3.5:9b")
    assert error.value.code == "local.offline_download"
    with pytest.raises(NodestroError):
        managed.local_models.install_runtime(download=True)


def test_the_strict_offline_mode_blocks_cloud_models_and_server_tools(managed: Workspace) -> None:
    managed.update_settings(SettingsIn(strict_offline=True))
    workflow = Workflow.model_validate(
        {
            "id": "w",
            "name": "Mixed",
            "nodes": [
                {"id": "s", "type": "start"},
                {
                    "id": "a",
                    "type": "agent",
                    "agent": {
                        "name": "A",
                        "model": LOCAL_DEFAULT_MODEL,
                        "task": "x",
                        "fallbacks": ["anthropic:claude-haiku-5-5"],
                        "server_tools": ["web_search"],
                    },
                },
                {"id": "o", "type": "output"},
            ],
            "edges": [{"id": "e1", "source": "s", "target": "a"}, {"id": "e2", "source": "a", "target": "o"}],
        }
    )
    codes = [item.code for item in preflight(managed, workflow).problems]
    assert "check.offline_cloud_model" in codes and "check.offline_server_tool" in codes


def test_the_strict_offline_mode_refuses_cloud_connections_before_any_request(tmp_path: Path) -> None:
    """Assistants, architects, run chat answers and orchestrator decisions connect through the workspace: in the
    strict offline mode a cloud model is refused there with the workflow check's code, not only in the check."""
    from nodestro_runtime.testing import ScriptedClient

    fake = ScriptedClient({})
    workspace = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    try:
        assert workspace.connect_model("anthropic:claude-haiku-5-5").provider == "anthropic"  # online: allowed
        workspace.update_settings(SettingsIn(strict_offline=True))
        for model in ("anthropic:claude-haiku-5-5", "openai:gpt-4o", "compatible:llama", "vllm:qwen"):
            with pytest.raises(NodestroError) as error:
                workspace.connect_model(model)
            assert error.value.code == "check.offline_cloud_model" and error.value.params["model"] == model
        with pytest.raises(NodestroError) as error:
            workspace.connection("openai")  # what agents and orchestrator decisions use
        assert error.value.code == "check.offline_cloud_model"
        assert workspace.connect_model(LOCAL_DEFAULT_MODEL).provider == "ollama"  # local models stay allowed
        assert not fake.messages.calls
        workspace.update_settings(SettingsIn(strict_offline=False))
        assert workspace.connect_model("openai:gpt-4o").provider == "openai"
    finally:
        workspace.close()


def test_the_model_folder_can_move(managed: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "ssd" / "models"
    assert managed.local_models.set_models_dir(str(target), transfer="use") is None
    assert managed.local_models.models_dir() == target and target.is_dir()
    with pytest.raises(NodestroError) as error:
        managed.local_models.set_models_dir("relative/path", transfer="use")
    assert error.value.code == "local.path_relative"
    job = managed.local_models.set_models_dir(str(tmp_path / "other"), transfer="copy")
    assert job is not None
    deadline = time.monotonic() + 10
    while managed.local_models.job(job.id).state == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert managed.local_models.job(job.id).state == "done"
    assert managed.local_models.models_dir() == tmp_path / "other"


def test_strict_price_mode_admits_local_requests(managed: Workspace) -> None:
    managed.update_settings(SettingsIn(price_mode="strict"))
    tracker = managed.tracker(1.0)
    lookup = pricing_basis(None, resolver=tracker.price_resolver).lookup("ollama", "qwen3.5:4b")
    assert lookup.price is not None and lookup.price.source == "local"
    tracker.check_request(agent="a", provider="ollama", model="qwen3.5:4b", lookup=lookup)  # no UnpricedModelError


def test_nothing_starts_or_downloads_when_a_workspace_opens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NODESTRO_OLLAMA_BINARY", raising=False)
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        assert workspace.local_models._managed is None
        assert not (tmp_path / "data" / "local-models").exists()


def test_local_usage_is_counted_but_never_shown_as_a_free_total() -> None:
    from orkexa.core.types import TokenUsage
    from orkexa.pricing import PricingBasis
    from orkexa.usage import record_token_usage, summarize_usage

    from nodestro_runtime.engine import add_usage, usage_of

    records = [
        record_token_usage(
            TokenUsage(input_tokens=100, output_tokens=20),
            agent_name="a",
            model="qwen3.5:4b",
            provider="ollama",
            custom_pricing=PricingBasis(),
        )
    ]
    usage = usage_of(summarize_usage(records), records)
    assert usage.local_requests == 1 and usage.requests == 1 and usage.cost_complete
    assert usage.estimated_cost_usd == 0 and usage.pricing_sources == ["local"]
    assert add_usage(usage, usage).local_requests == 2


def test_preferences_decide_only_between_equally_suited_models() -> None:
    from nodestro_runtime.local_models import CATALOG, SelectionPreferences, recommend

    laptop = Hardware(system="Linux", machine="x86_64", cpus=8, ram_bytes=16 * GB, gpu="none")
    plain = recommend("write", laptop)
    # Without a preference Qwen stays the standard; the European models are alternatives.
    assert plain.model == "ollama:qwen3.5:9b" and "preferred_region" not in plain.reasons
    assert all(not recommend(task, laptop).model.startswith(("ollama:ministral", "ollama:devstral")) for task in TASKS)
    europe = recommend("write", laptop, preferences=SelectionPreferences(region="eu"))
    assert europe.model == "ollama:ministral-3:8b" and europe.quality == plain.quality == "recommended"
    assert "preferred_region" in europe.reasons and "ollama:qwen3.5:9b" in europe.alternatives
    # The preference suggests an equally suited download over an installed model, never a weaker model.
    small = recommend("classify", laptop, ["qwen3.5:4b"], preferences=SelectionPreferences(region="eu"))
    assert (
        small.model == "ollama:ministral-3:3b" and not small.installed and small.alternatives[0] == "ollama:qwen3.5:4b"
    )
    # Devstral is a coding model: never suggested for other work, and only on a GPU that holds it.
    workstation = Hardware(
        system="Linux", machine="x86_64", cpus=16, ram_bytes=64 * GB, gpu="nvidia", gpu_memory_bytes=24 * GB
    )
    eu = SelectionPreferences(region="eu")
    assert recommend("coding", workstation, preferences=eu).model == "ollama:devstral-small-2:24b"
    assert recommend("analysis", workstation, preferences=eu).model != "ollama:devstral-small-2:24b"
    assert recommend("coding", workstation).model != "ollama:devstral-small-2:24b"  # Qwen without the preference
    assert recommend("coding", laptop, preferences=eu).model != "ollama:devstral-small-2:24b"
    # A region without models of the needed suitability: Qwen, and the reasons say so.
    vision = recommend("embedding", laptop, preferences=SelectionPreferences(region="us"))
    assert vision.model == "ollama:qwen3-embedding:0.6b" and "preferred_region_unavailable" in vision.reasons
    assert "preferred_region" in recommend("write", laptop, preferences=SelectionPreferences(region="cn")).reasons
    # Languages: every catalog model documents the six studio languages; an undocumented one is named, not hidden.
    assert all({"de", "fr", "pt"} <= set(model.languages) for model in CATALOG)
    french = recommend("write", laptop, preferences=SelectionPreferences(languages=["fr", "pt-BR"]))
    assert french.model == plain.model and "languages_unverified" not in french.reasons
    rare = recommend("write", laptop, preferences=SelectionPreferences(languages=["eu"]))  # Basque
    assert rare.model == plain.model and "languages_unverified" in rare.reasons


def test_the_check_names_missing_and_unsuitable_local_models(
    managed: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def agent(node_id: str, model: str, tools: list[str] | None = None) -> dict[str, object]:
        config = {"name": node_id, "model": model, "task": "x", "tools": tools or []}
        return {"id": node_id, "type": "agent", "agent": config}

    workflow = Workflow.model_validate(
        {
            "id": "w",
            "name": "Local",
            "nodes": [
                {"id": "s", "type": "start"},
                agent("ok", "ollama:qwen3.5:4b", ["calculator"]),
                agent("missing", "ollama:qwen3.5:9b"),
                agent("embed", "ollama:qwen3-embedding:0.6b"),
                agent("plain", "ollama:plain-model", ["calculator"]),
                {"id": "o", "type": "output"},
            ],
            "edges": [
                {"id": f"e{i}", "source": "s", "target": target}
                for i, target in enumerate(["ok", "missing", "embed", "plain"])
            ]
            + [{"id": "eo", "source": "ok", "target": "o"}],
        }
    )
    # While no local server runs, nothing starts: the managed server's (empty) model folder tells what is missing.
    found = {(item.code, item.params.get("model")) for item in preflight(managed, workflow).problems}
    assert {item for item in found if item[0].startswith("check.local_model")} == {
        ("check.local_model_missing", model)
        for model in ("ollama:qwen3.5:4b", "ollama:qwen3.5:9b", "ollama:qwen3-embedding:0.6b", "ollama:plain-model")
    }
    assert managed.local_models._managed is None
    managed.local_models.start()
    real = managed.local_models.installed
    plain = InstalledModel(name="plain-model:latest", size=1, digest="0" * 64, kind="chat", capabilities=["completion"])
    monkeypatch.setattr(managed.local_models, "installed", lambda client=None: [*real(client), plain])
    issues = {(item.code, item.node_id, item.params.get("model")) for item in preflight(managed, workflow).problems}
    local = {issue for issue in issues if issue[0].startswith("check.local_model")}
    assert local == {
        ("check.local_model_missing", None, "ollama:qwen3.5:9b"),
        ("check.local_model_not_chat", None, "ollama:qwen3-embedding:0.6b"),
        ("check.local_model_no_tools", "plain", "ollama:plain-model"),
    }


def test_local_agents_validate_with_what_the_server_reports(managed: Workspace) -> None:
    from nodestro_runtime.agent_builder import _effort
    from nodestro_runtime.providers import Connection
    from nodestro_runtime.schemas import AgentConfig

    # Persistent memory needs tool calls and the default effort is medium: the check knows both from /api/show.
    config = AgentConfig.model_validate(
        {"name": "A", "model": LOCAL_DEFAULT_MODEL, "task": "x", "memory": {"mode": "persistent", "space": "s"}}
    )
    assert config.effort == "medium"
    # Before the server runs, the verified catalog stands in for /api/show; the check starts nothing.
    assert [issue.code for issue in managed.builder.validate(config) if issue.level == "error"] == []
    assert managed.local_models._managed is None
    managed.local_models.start()
    assert [issue.code for issue in managed.builder.validate(config) if issue.level == "error"] == []
    # A computer with a GPU: the effort is sent as it is (the CPU saving mode has its own tests).
    managed.local_models._hardware = Hardware(
        system="linux", machine="x86_64", cpus=8, ram_bytes=32 * 1024**3, gpu="nvidia", gpu_memory_bytes=24 * 1024**3
    )
    connection = managed.connection("ollama")
    assert connection.agent_kwargs(LOCAL_DEFAULT_MODEL)["capabilities"]["reasoning"] == "supported"
    assert connection.options().send_reasoning_effort  # a thinking model gets the effort as reasoning_effort
    assert not connection.reasoning_saving
    assert _effort(config, connection, LOCAL_DEFAULT_MODEL) == "medium"
    # A model that cannot reason has no effort to set: left out, never a refused agent.
    plain = Connection(provider="ollama", client=object(), reported=lambda _model: {"reasoning": "unsupported"})
    assert _effort(config, plain, "ollama:plain:1b") is None
    assert _effort(config, Connection(provider="anthropic", client=object()), DEFAULT_AGENT_MODEL) == "medium"


def test_a_local_model_at_effort_low_does_not_think() -> None:
    from nodestro_runtime.agent_builder import _effort, _options
    from nodestro_runtime.providers import Connection
    from nodestro_runtime.schemas import AgentConfig

    thinking = Connection(provider="ollama", client=object(), reported=lambda _model: {"reasoning": "supported"})
    low = AgentConfig.model_validate({"name": "A", "model": LOCAL_DEFAULT_MODEL, "task": "x", "effort": "low"})
    # Ollama's thinking models have no graded effort: low turns thinking off instead of thinking without a limit.
    assert _effort(low, thinking, LOCAL_DEFAULT_MODEL) is None
    assert _options(low, "ollama", thinking, LOCAL_DEFAULT_MODEL).request_options == {"reasoning_effort": "none"}
    medium = low.model_copy(update={"effort": "medium"})
    assert _effort(medium, thinking, LOCAL_DEFAULT_MODEL) == "medium"
    assert _options(medium, "ollama", thinking, LOCAL_DEFAULT_MODEL).request_options == {}
    # The agent's own request option wins, and a model that cannot reason gets nothing.
    own = low.model_copy(update={"request_options": {"reasoning_effort": "high"}})
    assert _options(own, "ollama", thinking, LOCAL_DEFAULT_MODEL).request_options == {"reasoning_effort": "high"}
    plain = Connection(provider="ollama", client=object(), reported=lambda _model: {"reasoning": "unsupported"})
    assert _options(low, "ollama", plain, "ollama:plain:1b").request_options == {}


def test_a_refused_ollama_address_leaves_capabilities_unknown(tmp_path: Path) -> None:
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        # An address with credentials, stored before such addresses were refused: no exception escapes the check.
        workspace.store.put_raw("settings", "app", {"providers": {"ollama": {"base_url": "http://u:pw@h/v1"}}})
        assert workspace.local_models.capability_overrides("llama3.1:8b") == {}


def test_the_european_preference_is_a_stored_setting(tmp_path: Path) -> None:
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        assert not workspace.settings().prefer_european_models
        assert workspace.local_models.preferences().region is None
        assert workspace.update_settings(SettingsIn(prefer_european_models=True)).prefer_european_models
        assert workspace.local_models.preferences().region == "eu"
        # The preference changes only the local suggestion: the cloud defaults and the strict offline mode stay.
        settings = workspace.settings()
        assert not settings.strict_offline
        choice = workspace.local_models.recommend("write")
        assert choice.model.startswith("ollama:")
        assert {"preferred_region", "preferred_region_unavailable"} & set(choice.reasons)


def test_the_context_window_can_be_set_within_bounds() -> None:
    from nodestro_runtime.local_service import LOCAL_CONTEXT_LENGTH, _context_length

    assert _context_length(None) == LOCAL_CONTEXT_LENGTH == 32768
    assert _context_length("8192") == 8192
    assert _context_length("1024") == _context_length("1000000") == _context_length("big") == LOCAL_CONTEXT_LENGTH


def test_mdenseon_is_built_and_suggested_only_with_the_european_preference(managed: Workspace) -> None:
    from nodestro_runtime.local_models import CATALOG, SelectionPreferences, recommend

    entry = next(model for model in CATALOG if model.name == "mdenseon:307m")
    assert entry.source == "build" and entry.alternative and entry.region == "eu" and entry.dimensions == 768
    laptop = Hardware(system="Linux", machine="x86_64", cpus=8, ram_bytes=16 * GB, gpu="none")
    assert recommend("embedding", laptop).model == "ollama:qwen3-embedding:0.6b"
    europe = recommend("embedding", laptop, preferences=SelectionPreferences(region="eu"))
    assert europe.model == "ollama:mdenseon:307m" and "preferred_region" in europe.reasons
    # An installed index model stays: another embedding model means reindexing everything.
    kept = recommend("embedding", laptop, ["qwen3-embedding:0.6b"], preferences=SelectionPreferences(region="eu"))
    assert kept.model == "ollama:qwen3-embedding:0.6b" and "ollama:mdenseon:307m" in kept.alternatives
    # A download of a built model fetches its pinned inputs, which the strict offline mode refuses; an offline build
    # needs both files.
    with pytest.raises(NodestroError) as error:
        managed.local_models.build("mdenseon:307m")
    assert error.value.code == "local.archive_or_download"
    with pytest.raises(NodestroError) as error:
        managed.local_models.build("qwen3.5:4b", download=True)
    assert error.value.code == "local.not_buildable"
    managed.update_settings(SettingsIn(strict_offline=True))
    with pytest.raises(NodestroError) as error:
        managed.local_models.pull("mdenseon:307m")
    assert error.value.code == "local.offline_download"


def test_the_catalog_license_is_an_id_and_its_source_a_code() -> None:
    """UX round 3 B4: the license mixed the SPDX id with an English note, shown untranslated in every language."""
    for entry in CATALOG:
        assert re.fullmatch(r"[A-Za-z0-9.+-]+", entry.license), entry.name
    sources = {entry.name: entry.license_source for entry in CATALOG}
    assert sources["devstral-small-2:24b"] == "upstream" and sources["qwen3.5:4b"] == "ollama"


def _child_environment(workspace: Workspace) -> dict[str, str]:
    server = workspace.local_models._managed
    assert server is not None and server._process is not None
    raw = Path(f"/proc/{server._process.pid}/environ").read_bytes()
    return dict(item.split("=", 1) for item in raw.decode().split("\0") if "=" in item)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the server's environment from /proc")
def test_switching_the_strict_offline_mode_restarts_the_managed_server(
    managed: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RT-059: the running managed server takes the new mode (no proxy route when offline) without a restart of
    Nodestro; while a run uses it, the restart waits."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    managed.connection("ollama")  # starts the server
    server = managed.local_models._managed
    assert server is not None and server.offline is False
    first = server._process.pid
    assert _child_environment(managed).get("HTTPS_PROXY") == "http://proxy.example:3128"
    # A run is executing: the switch is stored, the server keeps running until nothing uses it.
    busy = [True]
    managed.local_models.idle = lambda: not busy[0]
    managed.update_settings(SettingsIn(strict_offline=True))
    assert server.offline is False and server._process.pid == first
    busy[0] = False
    status = managed.local_models.status()  # the next look at the server restarts it
    assert status.offline and status.running
    assert server.offline is True and server._process.pid != first
    assert "HTTPS_PROXY" not in _child_environment(managed)
    # Switched off again while idle: restarted at once, with the proxy route.
    second = server._process.pid
    managed.update_settings(SettingsIn(strict_offline=False))
    assert server.offline is False and server._process.pid != second
    assert _child_environment(managed).get("HTTPS_PROXY") == "http://proxy.example:3128"


def test_a_stopped_managed_server_takes_the_mode_at_its_next_start(managed: Workspace) -> None:
    managed.connection("ollama")
    managed.local_models.stop()
    server = managed.local_models._managed
    assert server is not None and server.base_url is None
    managed.update_settings(SettingsIn(strict_offline=True))
    assert server.offline is True and server.base_url is None  # not started by the switch
    managed.local_models.start()
    assert server.offline is True and server.base_url is not None
