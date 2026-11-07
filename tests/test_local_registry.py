"""Capabilities of a model outside the catalog, read from the Ollama registry before its download (RT-062).

The registry is a stand-in that serves manifests, configs and templates shaped like the real ones (taken from
``registry.ollama.ai`` on 2026-11-07); ``test_live_registry`` reads the real registry when
``NODESTRO_LIVE_REGISTRY=1``.
"""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

import pytest
from test_local_models import FAKE_OLLAMA

from nodestro_runtime import local_registry
from nodestro_runtime.local_readiness import ModelNeeds, diagnose
from nodestro_runtime.local_registry import RegistryError, read_facts, registry_facts, registry_path
from nodestro_runtime.schemas import SettingsIn
from nodestro_runtime.workspace import Workspace

TOOLS_TEMPLATE = b"{{- if .Tools }}<tools>{{- range .Tools }}{{ .Function }}{{ end }}</tools>{{ end }}{{ .Prompt }}"
PLAIN_TEMPLATE = b"<start_of_turn>user\n{{ .Prompt }}<end_of_turn>\n<start_of_turn>model\n"


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


class FakeRegistry:
    """Serves ``repository:tag`` models: {kinds of layers, config, template}; records every URL it was asked for."""

    def __init__(self) -> None:
        self.models: dict[str, dict[str, Any]] = {}
        self.blobs: dict[str, bytes] = {}
        self.urls: list[str] = []
        self.fail: Exception | None = None

    def add(
        self, path: str, *, template: bytes | None, config: dict[str, Any] | None = None, projector: bool = False
    ) -> None:
        config_body = json.dumps(config or {"model_format": "gguf", "model_family": "x"}).encode()
        self.blobs[_digest(config_body)] = config_body
        layers: list[dict[str, Any]] = [
            {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:" + "a" * 64, "size": 10**9}
        ]
        if projector:
            layers.append(
                {"mediaType": "application/vnd.ollama.image.projector", "digest": "sha256:" + "b" * 64, "size": 10**8}
            )
        if template is not None:
            self.blobs[_digest(template)] = template
            layers.append(
                {
                    "mediaType": "application/vnd.ollama.image.template",
                    "digest": _digest(template),
                    "size": len(template),
                }
            )
        self.models[path] = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {
                "mediaType": "application/vnd.docker.container.image.v1+json",
                "digest": _digest(config_body),
                "size": len(config_body),
            },
            "layers": layers,
        }

    def get(self, url: str, accept: str, limit: int) -> bytes:
        self.urls.append(url)
        if self.fail is not None:
            raise self.fail
        prefix = "https://registry.ollama.ai/v2/"
        assert url.startswith(prefix), url
        rest = url.removeprefix(prefix)
        if "/manifests/" in rest:
            repository, tag = rest.split("/manifests/")
            assert "manifest.v2+json" in accept
            found = self.models.get(f"{repository}:{tag}")
            if found is None:
                raise RegistryError("HTTPError: HTTP Error 404: Not Found")
            return json.dumps(found).encode()
        digest = rest.split("/blobs/")[1]
        assert not digest.endswith("a" * 64) and not digest.endswith("b" * 64), "never the weights or the projector"
        return self.blobs[digest]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    monkeypatch.setattr(local_registry, "get", fake.get)
    local_registry.clear_cache()
    yield fake
    local_registry.clear_cache()


def test_names_of_the_public_registry_only() -> None:
    assert registry_path("gemma3:4b") == ("library/gemma3", "4b")
    assert registry_path("gemma3") == ("library/gemma3", "latest")
    assert registry_path("someone/model:q4") == ("someone/model", "q4")
    assert registry_path("registry.ollama.ai/library/gemma3:4b") == ("library/gemma3", "4b")
    for other in ("hf.co/someone/model:Q4_K_M", "a/b/c/d", "bad name", "../x", "qwen3:480b-cloud"):
        assert registry_path(other) is None, other


def test_a_template_without_tools_and_no_parser_lacks_tools(registry: FakeRegistry) -> None:
    registry.add("library/gemma3:4b", template=PLAIN_TEMPLATE, projector=True)
    facts = read_facts("gemma3:4b")
    assert facts.absent == {"tools"} and facts.present == {"vision"}
    assert registry.urls[0] == "https://registry.ollama.ai/v2/library/gemma3/manifests/4b"
    assert len(registry.urls) == 3  # manifest, config, template: never the model file


def test_a_template_with_tools_has_them(registry: FakeRegistry) -> None:
    registry.add("library/qwen3:0.6b", template=TOOLS_TEMPLATE)
    facts = read_facts("qwen3:0.6b")
    assert facts.present == {"tools"} and facts.absent == set()


def test_no_template_means_no_tools(registry: FakeRegistry) -> None:
    registry.add("library/old:1b", template=None)
    assert read_facts("old:1b").absent == {"tools"}


def test_a_built_in_parser_leaves_tools_unknown(registry: FakeRegistry) -> None:
    registry.add("library/qwen3.5:2b", template=None, config={"model_family": "qwen35", "parser": "qwen3.5"})
    facts = read_facts("qwen3.5:2b")
    assert facts.absent == set() and facts.present == set()
    assert len(registry.urls) == 2  # no template read: the parser decides


def test_vision_is_never_reported_missing(registry: FakeRegistry) -> None:
    registry.add("library/textonly:1b", template=TOOLS_TEMPLATE)  # no projector: vision may be in the model file
    assert "vision" not in read_facts("textonly:1b").absent


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda m: m.update(layers=None), "no layer list"),
        (lambda m: m.pop("config"), "no config"),
        (lambda m: m.update(layers=[]), "no model layer"),
        (lambda m: m["config"].update(size=10**6), "larger than"),
        (lambda m: m["config"].update(digest="sha256:" + "c" * 64), "does not match its digest"),
        (lambda m: m["config"].update(digest="md5:x"), "valid digest"),
    ],
)
def test_a_broken_manifest_is_an_error_not_a_guess(registry: FakeRegistry, change: Any, message: str) -> None:
    registry.add("library/broken:1b", template=PLAIN_TEMPLATE)
    change(registry.models["library/broken:1b"])
    registry.blobs.setdefault("sha256:" + "c" * 64, b"{}")
    with pytest.raises(RegistryError, match=message):
        read_facts("broken:1b")


def test_answers_that_are_not_json_objects_are_refused(registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_registry, "get", lambda url, accept, limit: b"[1, 2]")
    with pytest.raises(RegistryError, match="not a JSON object"):
        read_facts("x:1b")
    monkeypatch.setattr(local_registry, "get", lambda url, accept, limit: b"<html>")
    with pytest.raises(RegistryError, match="not JSON"):
        read_facts("x:1b")


def test_failures_are_unknown_and_cached_for_a_while(registry: FakeRegistry) -> None:
    registry.fail = RegistryError("URLError: no network")
    clock = [100.0]
    assert registry_facts("gemma3:4b", now=lambda: clock[0]) is None
    assert registry_facts("gemma3:4b", now=lambda: clock[0]) is None
    assert len(registry.urls) == 1  # the failure is kept
    registry.fail = None
    registry.add("library/gemma3:4b", template=PLAIN_TEMPLATE)
    clock[0] += local_registry.TTL_FAILED + 1
    found = registry_facts("gemma3:4b", now=lambda: clock[0])
    assert found is not None and found.absent == {"tools"}
    assert registry_facts("gemma3:4b", now=lambda: clock[0] + 60) is found  # kept for an hour
    assert registry_facts("hf.co/a/b:c") is None  # another host: not asked at all
    assert not any("hf.co" in url for url in registry.urls)


def test_the_real_getter_refuses_large_answers_and_plain_http_redirects() -> None:
    handler = local_registry._NoInsecureRedirect()
    with pytest.raises(RegistryError, match="non-HTTPS"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://example.invalid/blob")
    with pytest.raises(RegistryError):
        local_registry._http_get("https://registry.invalid/v2/x", "*/*", 10)


# ---- the readiness check ---------------------------------------------------------------------------------------


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Workspace]:
    if sys.platform == "win32":
        pytest.skip("the fake Ollama binary is a shebang script")
    binary = tmp_path / "bin" / "ollama"
    binary.parent.mkdir()
    binary.write_text(FAKE_OLLAMA.format(python=sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("NODESTRO_OLLAMA_BINARY", str(binary))
    monkeypatch.setattr("nodestro_runtime.local_readiness._free_bytes", lambda folder: 10**12)
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        yield workspace
    finally:
        workspace.close()


def test_a_model_outside_the_catalog_that_lacks_tools_is_reported_before_the_download(
    managed: Workspace, registry: FakeRegistry
) -> None:
    registry.add("library/gemma3:4b", template=PLAIN_TEMPLATE, projector=True)
    problem = diagnose(
        managed, "ollama:gemma3:4b", action="assistant", can_install=True, needs=ModelNeeds(capabilities=["tools"])
    )
    assert problem is not None and problem.cause == "capability_missing"
    assert problem.missing_capabilities == ["tools"] and problem.from_registry
    assert "download" not in problem.actions
    # Image input is not proven missing (and here even present): only what is needed and certainly missing counts.
    vision = diagnose(
        managed,
        "ollama:gemma3:4b",
        action="agent_test",
        can_install=True,
        needs=ModelNeeds(capabilities=["image_input"]),
    )
    assert vision is not None and vision.cause == "model_missing" and not vision.from_registry


def test_a_model_with_tools_or_an_unknown_answer_is_offered_for_download(
    managed: Workspace, registry: FakeRegistry
) -> None:
    registry.add("library/qwen3:0.6b", template=TOOLS_TEMPLATE)
    needs = ModelNeeds(capabilities=["tools"])
    found = diagnose(managed, "ollama:qwen3:0.6b", action="assistant", can_install=True, needs=needs)
    assert found is not None and found.cause == "model_missing" and "download" in found.actions
    unknown = diagnose(managed, "ollama:not-there:1b", action="assistant", can_install=True, needs=needs)  # 404
    assert unknown is not None and unknown.cause == "model_missing" and "download" in unknown.actions


def test_no_registry_read_without_a_need_offline_or_for_catalog_and_installed_models(
    managed: Workspace, registry: FakeRegistry
) -> None:
    registry.add("library/gemma3:4b", template=PLAIN_TEMPLATE)
    tools = ModelNeeds(capabilities=["tools"])
    assert diagnose(managed, "ollama:gemma3:4b", action="run", can_install=True).cause == "model_missing"  # type: ignore[union-attr]
    assert diagnose(managed, "ollama:qwen3.5:4b", action="assistant", can_install=True, needs=tools) is None
    assert diagnose(managed, "ollama:qwen3.5:9b", action="assistant", can_install=True, needs=tools).cause == (  # type: ignore[union-attr]
        "model_missing"
    )
    assert registry.urls == []
    managed.update_settings(SettingsIn(strict_offline=True))
    offline = diagnose(managed, "ollama:gemma3:4b", action="assistant", can_install=True, needs=tools)
    assert offline is not None and offline.cause == "model_missing" and offline.blocked == "offline"
    assert registry.urls == []  # the strict offline mode reads nothing


@pytest.mark.skipif(os.environ.get("NODESTRO_LIVE_REGISTRY") != "1", reason="reads registry.ollama.ai (opt-in)")
def test_live_registry() -> None:
    local_registry.clear_cache()
    gemma = read_facts("gemma3:4b")
    assert gemma.absent == {"tools"} and "vision" in gemma.present
    assert read_facts("qwen3:0.6b").present == {"tools"}
    assert read_facts("qwen3.5:2b").absent == set()  # built-in parser
