"""The immutable snapshot of a published workflow version (a release).

A release bundle holds everything a reproducible run needs and nothing secret:

- the workflow in the published version, with every sub-workflow pinned to an exact version
  and those versions themselves (``subworkflows``);
- the exact code of every own or generated tool (the approved registry record, with its hash);
  built-in tools are part of the Nodestro version named in ``runtime``;
- the approved Python libraries per workflow (the lock, never the approvals);
- references to data that is *not* versioned: knowledge files (name, size, SHA-256) and
  persistent memory spaces. They are live data of the installation; the bundle only names
  them, so later changes to them are visible, never hidden;
- the request and result schemas and the runtime versions it was published with;
- the MCP tools its agents use, frozen at their approved definitions (server id and slug, hash, classification);
  never a server address or a token.

API keys, the Anthropic key and settings are never part of a bundle. The bundle is used by
Nodestro's direct API (:mod:`nodestro_api.publishing`) and is the ``workflow/release.json`` of an
exported project (:mod:`nodestro_api.standalone`); both run it with the same engine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from orkexa.dynamic import ToolRecord
from pydantic import BaseModel, Field

from .sandbox_policy import TrustClass
from .schemas import Workflow, now
from .tool_provenance import MemoryProvenance

FORMAT = "nodestro-release"
FORMAT_VERSION = 1


class McpToolRef(BaseModel):
    """An MCP tool frozen in a release bundle (WP-9): the approved definition the release was published with.

    It names the server by id and slug only: no address, no token, no credentials. A release run uses the tool only
    while the tenant still has the same server with this tool approved at exactly this hash and classification
    (``mcp.release_tool_changed`` otherwise). Defined here, not in :mod:`nodestro_runtime.mcp`, so a runtime copy
    without the MCP client (an exported project) can still read bundles."""

    tool_name: str
    server_id: str
    server_slug: str
    name: str
    hash: str
    access: Literal["read", "write", "destructive"]
    #: The approved definition (name, title, description, schemas, annotations), for review and comparison.
    definition: dict[str, Any] = Field(default_factory=dict)


class SubworkflowSnapshot(BaseModel):
    workflow_id: str
    version: int
    workflow: Workflow


class PythonLock(BaseModel):
    """Approved third-party libraries of one workflow's tools (``uv pip compile`` output)."""

    workflow_id: str
    requirements: list[str] = Field(default_factory=list)
    lock: str = ""
    lock_sha: str = ""
    python_version: str = ""


class KnowledgeRef(BaseModel):
    """A knowledge file an agent uses: referenced live data, not part of the version."""

    id: str
    name: str
    media_type: str = ""
    size: int = 0
    sha256: str = ""
    #: The agents that use it (labels) and how.
    used_by: list[str] = Field(default_factory=list)
    modes: list[str] = Field(default_factory=list)


class MemoryRef(BaseModel):
    """A persistent memory space an agent reads and writes: live data shared by all runs."""

    space: str
    used_by: list[str] = Field(default_factory=list)


#: The components a release's ``runtime`` records; entries of any other component are not kept.
RUNTIME_KEYS = ("nodestro", "orkexa")


def current_runtime(runtime: dict[str, str]) -> dict[str, str]:
    """``runtime`` with only the components of :data:`RUNTIME_KEYS`."""
    return {key: value for key, value in runtime.items() if key in RUNTIME_KEYS}


class ReleaseBundle(BaseModel):
    format: str = FORMAT
    format_version: int = FORMAT_VERSION
    workflow_id: str
    #: The saved version of the workflow that was published.
    workflow_version: int | None = None
    workflow: Workflow
    subworkflows: list[SubworkflowSnapshot] = Field(default_factory=list)
    #: Own and generated tools: the approved registry records (name, version, code, hash, schema).
    tools: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: The trust class of each own tool when it was published (ADR-0006). Only ``user-authored`` tools may run
    #: without an isolating sandbox; a tool missing here (e.g. in a bundle published before this field existed)
    #: is ``untrusted``. Nodestro's own API ignores this claim and asks the studio's provenance instead
    #: (a restored database can carry any claim); an exported project uses it.
    tool_trust: dict[str, TrustClass] = Field(default_factory=dict)
    builtin_tools: list[str] = Field(default_factory=list)
    server_tools: list[str] = Field(default_factory=list)
    #: Tools of connected MCP servers, frozen at the definition they were approved with (WP-9): server id and slug,
    #: hash, classification, definition; never an address or a token. A run uses them only while the tenant's
    #: approval still matches (:class:`~nodestro_runtime.mcp.pinned.PinnedMcp`).
    mcp_tools: dict[str, McpToolRef] = Field(default_factory=dict)
    models: list[str] = Field(default_factory=list)
    python: list[PythonLock] = Field(default_factory=list)
    knowledge: list[KnowledgeRef] = Field(default_factory=list)
    memory: list[MemoryRef] = Field(default_factory=list)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    #: Versions of the shared runtime it was published with: ``{"nodestro": ..., "orkexa": ...}``.
    runtime: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: now().isoformat())

    def digest(self) -> str:
        """SHA-256 of the content (without the creation time): equal bundles, equal digest.

        An empty ``tool_trust`` or ``mcp_tools`` is left out, so bundles published before they existed keep their
        digest."""
        exclude = {"created_at"}
        if not self.tool_trust:
            exclude.add("tool_trust")
        if not self.mcp_tools:
            exclude.add("mcp_tools")
        data = self.model_dump(mode="json", exclude=exclude)
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def all_workflows(self) -> list[Workflow]:
        return [self.workflow, *(item.workflow for item in self.subworkflows)]

    def approvals(self) -> int:
        return sum(node.type == "approval" for workflow in self.all_workflows() for node in workflow.nodes)

    def resolver(self) -> Callable[[str, int | None], tuple[Workflow, int | None]]:
        """Sub-workflows from the bundle only: a release never reads the studio's current state."""
        pinned = {(item.workflow_id, item.version): item for item in self.subworkflows}

        def resolve(workflow_id: str, version: int | None) -> tuple[Workflow, int | None]:
            if version is None:
                candidates = sorted(
                    (item for item in self.subworkflows if item.workflow_id == workflow_id), key=lambda i: i.version
                )
                if not candidates:
                    raise KeyError(workflow_id)
                found = candidates[-1]
            else:
                found_or_none = pinned.get((workflow_id, version))
                if found_or_none is None:
                    raise KeyError(f"{workflow_id}@{version}")
                found = found_or_none
            return found.workflow.model_copy(deep=True), found.version

        return resolve

    def provenance(self) -> MemoryProvenance:
        """The recorded ``user-authored`` tool versions of the bundle, for a :class:`ToolLibrary` of its tools."""
        provenance = MemoryProvenance()
        for name, trust in self.tool_trust.items():
            record = self.tools.get(name)
            if trust == "user-authored" and record is not None:
                provenance.record_authored(name, int(record.get("version") or 1))
        return provenance

    def tool_marker(self, name: str) -> str:
        record = self.tools.get(name)
        pinned = self.mcp_tools.get(name)
        if pinned is not None:
            return f"{name}:mcp:{pinned.hash[:12]}"
        if record is None:
            return f"builtin:{name}" if name in self.builtin_tools else f"{name}:missing"
        return f"{name}:v{record.get('version')}:{str(record.get('sha256') or '')[:12]}"

    def tool_sources(self, workflow: Workflow) -> list[tuple[str, str, str]]:
        """``(name, code, tests)`` of the bundle's tools that ``workflow``'s agents use."""
        names = dict.fromkeys(name for node in workflow.nodes if node.agent is not None for name in node.agent.tools)
        return [
            (name, str(self.tools[name].get("code") or ""), str(self.tools[name].get("tests") or ""))
            for name in names
            if name in self.tools
        ]


def tool_record(record: ToolRecord) -> dict[str, Any]:
    data = asdict(record)
    data["status"] = "approved"
    return data


def materialize_tools(bundle: ReleaseBundle, directory: Path) -> Path:
    """Write the bundle's tools as a tool registry directory (one JSON file per tool version)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, record in bundle.tools.items():
        target = directory / name / f"v{record.get('version', 1)}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(record, indent=2, ensure_ascii=False)
        if not target.is_file() or target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8")
    return directory
