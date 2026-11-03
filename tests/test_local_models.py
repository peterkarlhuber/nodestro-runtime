"""Local models: the catalog, the task- and hardware-aware recommendation, the managed Ollama server of a workspace,
default models of new and existing installations, local prices and the strict offline mode."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
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
        "qwen3-embedding:0.6b",
        "qwen3-embedding:4b",
        "qwen3-embedding:8b",
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
    assert [info.model for info in local] == ["qwen3.5:4b", "qwen3.5:9b", "qwen3.6:35b-a3b", "qwen3.8:27b"]
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
    from orkexa.pricing import PricingBasis
    from orkexa.core.types import TokenUsage
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
