"""What an agent will receive, shown before a run: prompt, filled placeholders and context.

The task is rendered with the same :func:`~nodestro_runtime.engine.render_task` a run uses. Each
predecessor's output comes from the latest finished run of the workflow when there is
one; a predecessor that has no result yet is marked as pending instead of guessed. The
result is split into segments, so the editor can highlight where each input lands.
Nothing is sent to the API, and credentials in request options are hidden.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from .engine import _PLACEHOLDER, as_text, node_label, render_task, start_fields
from .file_delivery import collect, plan
from .forms import check_values, prompt_values
from .graph import comment_sources, data_sources
from .problems import NodestroError
from .sanitize import redact_secrets
from .schemas import ExampleMessage, OutputConfig, Run, RunSummary, Workflow, WorkflowIn

if TYPE_CHECKING:
    from .workspace import Workspace

_MAX_PREVIEW = 6000
_RUN_INPUT_NAMES = {"input", "run.input", "workflow.input"}


class PromptSegment(BaseModel):
    #: ``text`` as written in the template, ``run_input``, an available ``input`` or a ``pending`` one.
    kind: Literal["text", "run_input", "input", "pending"]
    text: str = ""
    node_id: str | None = None
    label: str | None = None
    truncated: bool = False


class PreviewInput(BaseModel):
    node_id: str
    label: str
    #: ``available`` from a finished run, ``pending`` when that predecessor has no result yet.
    status: Literal["available", "pending"]
    #: Status of the predecessor in that run (``failed``, ``skipped`` ...) when it did not succeed.
    run_status: str | None = None
    chars: int = 0


class PlaceholderInfo(BaseModel):
    name: str
    #: ``run_input``, ``input`` (a predecessor), ``form`` (a form value), ``not_connected`` or
    #: ``unknown`` (stays as written).
    status: Literal["run_input", "input", "form", "not_connected", "unknown"]


class KnowledgeInfo(BaseModel):
    id: str
    name: str | None = None
    missing: bool = False
    #: Run files: how the agent gets the file (see :mod:`nodestro_runtime.file_delivery`).
    mode: str | None = None
    format: str | None = None
    source: str | None = None
    chars: int = 0
    notes: list[str] = Field(default_factory=list)


class ContextPreview(BaseModel):
    node_id: str
    agent_name: str
    model: str
    system_prompt: str
    task_template: str
    segments: list[PromptSegment]
    #: True when every input is known, i.e. the preview is exactly what the agent would get.
    complete: bool
    inputs: list[PreviewInput]
    placeholders: list[PlaceholderInfo]
    run_input: str
    run_input_source: Literal["given", "start_default", "run", "empty"]
    #: The input form's values the preview assumes (from the run, the given values or the defaults).
    form_values: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
    run_created_at: datetime | None = None
    examples: list[ExampleMessage] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    server_tools: list[str] = Field(default_factory=list)
    knowledge_mode: str = "on_demand"
    knowledge: list[KnowledgeInfo] = Field(default_factory=list)
    run_files: list[KnowledgeInfo] = Field(default_factory=list)
    memory_mode: str = "none"
    memory_space: str | None = None
    memory_files: int = 0
    output: OutputConfig = Field(default_factory=OutputConfig)
    quality: bool = False
    request_options: dict[str, Any] = Field(default_factory=dict)


class PreviewRequest(BaseModel):
    workflow: WorkflowIn
    node_id: str
    #: The saved workflow, whose latest run provides the predecessors' outputs.
    workflow_id: str | None = None
    #: The workflow input to assume; by default the start node's default input or the last run's.
    input: str | None = None
    #: Take predecessor outputs from this run instead of the latest one.
    run_id: str | None = None
    #: Values of the input form to assume.
    values: dict[str, Any] | None = None


def _sentinel(key: str) -> str:
    return f"\x00{key}\x00"


def latest_run(nodestro: Workspace, workflow_id: str) -> Run | None:
    """The newest finished, own (not imported) run of a workflow."""
    summaries = nodestro.store.list("runs", RunSummary, limit=500)
    for summary in sorted(summaries, key=lambda item: item.created_at, reverse=True):
        if summary.workflow_id == workflow_id and summary.imported_at is None and summary.finished_at is not None:
            return nodestro.store.get("runs", summary.id, Run)
    return None


def build_preview(
    nodestro: Workspace,
    workflow: Workflow,
    node_id: str,
    *,
    given_input: str | None,
    run: Run | None,
    given_values: dict[str, Any] | None = None,
) -> ContextPreview:
    nodes = {node.id: node for node in workflow.nodes}
    node = nodes.get(node_id)
    if node is None:
        raise KeyError(node_id)
    if node.agent is None:
        raise NodestroError("preview.not_agent", "Only agents have a context preview")
    config = node.agent
    labels = {item.id: node_label(item) for item in workflow.nodes}
    # Decision and approval nodes pass results through: the agent gets what arrives at them.
    # An approval passes its comment on as well (named after the approval).
    sources = [*data_sources(workflow, node_id), *comment_sources(workflow, node_id)]
    starts = [item for item in workflow.nodes if item.type == "start"]
    fields = start_fields(workflow)
    if given_values is not None:
        form_values, _errors = check_values(fields, given_values)
    elif run is not None:
        form_values = dict(run.values)
    else:
        form_values, _errors = check_values(fields, {})

    input_source: Literal["given", "start_default", "run", "empty"]
    if given_input is not None:
        run_input, input_source = given_input, "given"
    elif run is not None:
        run_input, input_source = run.input, "run"
    elif starts and starts[0].start and starts[0].start.default_input:
        run_input, input_source = starts[0].start.default_input, "start_default"
    else:
        run_input, input_source = "", "empty"

    inputs: list[PreviewInput] = []
    values: dict[str, tuple[str, PreviewInput]] = {}
    for index, source in enumerate(sources):
        item = nodes[source]
        state = run.nodes.get(source) if run is not None else None
        if item.type == "start":
            text: str | None = as_text(form_values) if form_values else run_input
        elif item.type == "approval":
            approval = state.approval if state is not None else None
            text = (
                as_text({"decision": approval.decision, "comment": approval.comment})
                if approval is not None and approval.comment
                else None
            )
        elif state is not None and state.status == "succeeded":
            text = as_text(state.output)
        else:
            text = None
        info = PreviewInput(
            node_id=source,
            label=labels[source],
            status="available" if text is not None else "pending",
            run_status=state.status if state is not None and text is None else None,
            chars=len(text or ""),
        )
        inputs.append(info)
        values[str(index)] = (text or "", info)

    rendered = render_task(
        config.task_template,
        run_input=_sentinel("R"),
        inputs=[(labels[source], _sentinel(str(index))) for index, source in enumerate(sources)],
        values=prompt_values(fields, form_values, nodestro.file_catalog()) if form_values else form_values,
        run_names={labels[item.id] for item in starts},
    )
    segments: list[PromptSegment] = []
    for position, part in enumerate(rendered.split("\x00")):
        if position % 2 == 0:
            if part:
                segments.append(PromptSegment(kind="text", text=part))
        elif part == "R":
            text = run_input
            segments.append(
                PromptSegment(kind="run_input", text=text[:_MAX_PREVIEW], truncated=len(text) > _MAX_PREVIEW)
            )
        else:
            text, info = values[part]
            if info.status == "pending":
                segments.append(PromptSegment(kind="pending", node_id=info.node_id, label=info.label))
            else:
                segments.append(
                    PromptSegment(
                        kind="input",
                        text=text[:_MAX_PREVIEW],
                        node_id=info.node_id,
                        label=info.label,
                        truncated=len(text) > _MAX_PREVIEW,
                    )
                )

    upstream = {labels[source].strip().lower() for source in sources}
    everyone = {label.strip().lower() for key, label in labels.items() if key != node_id}
    placeholders: list[PlaceholderInfo] = []
    field_names = {field.name.lower() for field in fields}
    for match in _PLACEHOLDER.finditer(config.task_template or ""):
        name = match.group(1).strip()
        lowered = name.lower()
        status: Literal["run_input", "input", "form", "not_connected", "unknown"]
        if lowered == "form" or (lowered.startswith("form.") and lowered[5:].split(".")[0] in field_names):
            status = "form"
        elif lowered in ("run.input", "workflow.input") or (lowered == "input" and not sources):
            status = "run_input"
        elif lowered == "input" or lowered in upstream:
            status = "input"
        elif lowered in everyone:
            status = "not_connected"
        else:
            status = "unknown"
        if all(item.name != name for item in placeholders):
            placeholders.append(PlaceholderInfo(name=name, status=status))

    def files(ids: list[str]) -> list[KnowledgeInfo]:
        found = []
        for file_id in ids:
            if file_id in nodestro.knowledge.store:
                found.append(KnowledgeInfo(id=file_id, name=nodestro.knowledge.store.info(file_id).name))
            else:
                found.append(KnowledgeInfo(id=file_id, missing=True))
        return found

    start_ids = {item.id for item in starts}
    feeds_from_start = not sources or any(source in start_ids for source in sources)
    run_file_ids = (run.file_ids if run is not None else []) or [
        file_id for item in starts for file_id in (item.start.file_ids if item.start else [])
    ]
    for value in form_values.values():
        if isinstance(value, list):
            run_file_ids += [str(item) for item in value if str(item) not in run_file_ids]

    def run_files(ids: list[str]) -> list[KnowledgeInfo]:
        # The same decision a run makes: content, document, image or overview plus the files tool.
        found = collect(ids, fields, form_values, nodestro.inputs, nodestro.knowledge)
        plans = {item.file_id: item for item in plan(found, nodestro.builder.capabilities(config.model))}
        known = {item.id for item in found}
        result = []
        for file_id in ids:
            planned = plans.get(file_id)
            if file_id not in known or planned is None:
                result.append(KnowledgeInfo(id=file_id, missing=True))
                continue
            result.append(
                KnowledgeInfo(
                    id=file_id,
                    name=planned.name,
                    mode=planned.mode,
                    format=planned.format,
                    source=planned.source,
                    chars=planned.chars,
                    notes=planned.notes,
                )
            )
        return result

    memory_files = 0
    if config.memory.mode == "persistent" and config.memory.space:
        try:
            memory_files = len(nodestro.memory.open(config.memory.space).entries())
        except NodestroError:
            memory_files = 0
    return ContextPreview(
        node_id=node_id,
        agent_name=config.name,
        model=config.model,
        system_prompt=config.system_prompt,
        task_template=config.task_template,
        segments=segments,
        complete=all(info.status == "available" for info in inputs),
        inputs=inputs,
        placeholders=placeholders,
        run_input=run_input[:_MAX_PREVIEW],
        run_input_source=input_source,
        form_values=form_values,
        run_id=run.id if run is not None else None,
        run_created_at=run.created_at if run is not None else None,
        examples=[message for message in config.messages if message.content.strip()],
        tools=list(config.tools),
        server_tools=list(config.server_tools),
        knowledge_mode=config.knowledge.mode,
        knowledge=files(config.knowledge.file_ids),
        run_files=run_files(run_file_ids) if feeds_from_start else [],
        memory_mode=config.memory.mode,
        memory_space=config.memory.space,
        memory_files=memory_files,
        output=config.output,
        quality=config.quality.enabled,
        request_options=redact_secrets(config.request_options),
    )
