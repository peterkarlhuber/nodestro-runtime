"""Pydantic models shared by the API, the storage and the execution engine.

Everything a user configures in the editor is described here. The models are
plain data: :mod:`nodestro_runtime.agent_builder` turns an :class:`AgentConfig` into an
``orkexa.Agent``, :mod:`nodestro_runtime.engine` runs a :class:`Workflow`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

from .providers import ProviderSettingsIn, ProviderStatus

Effort = Literal["low", "medium", "high", "xhigh", "max"]
ThinkingMode = Literal["adaptive", "between_tools", "disabled"]
ThinkingDisplay = Literal["summarized", "omitted", "updates"]
NodeType = Literal["start", "agent", "output", "condition", "approval", "subworkflow"]
#: ``paused``: the run waits for a human decision at an approval node.
RunStatus = Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"]
#: ``skipped``: deliberately not run, because its branch was not chosen (no error);
#: ``blocked``: not run, because a predecessor failed or was cancelled;
#: ``awaiting_approval``: an approval node waiting for a person.
NodeStatus = Literal[
    "idle", "waiting", "running", "succeeded", "failed", "skipped", "blocked", "awaiting_approval", "cancelled"
]
#: What started a run.
#: ``api``: a published workflow API (:mod:`nodestro_api.serving`); ``trigger_ref`` is the publication.
RunTrigger = Literal["manual", "schedule", "batch", "benchmark", "subworkflow", "resume", "api"]

#: Models are named ``provider:model`` (see :mod:`nodestro_runtime.providers`); a name without a provider
#: prefix is an Anthropic model.
DEFAULT_AGENT_MODEL = "anthropic:claude-sonnet-5-5"
DEFAULT_ASSISTANT_MODEL = "anthropic:claude-opus-5-5"

#: The interface languages. AI assistants write in the interface language unless told otherwise.
Language = Literal["de", "en", "es", "fr", "pt-BR", "it"]
LANGUAGES: tuple[str, ...] = ("de", "en", "es", "fr", "pt-BR", "it")


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def total_cost(costs: Iterable[float | None]) -> float | None:
    """The sum of costs in USD; unknown (``None``) as soon as one cost is unknown, never counted as zero."""
    total = 0.0
    for cost in costs:
        if cost is None:
            return None
        total += cost
    return round(total, 6)


class _Model(BaseModel):
    """Base of the shared models: unknown fields are ignored on purpose.

    Stored documents of older versions, exports and the studio's editor state carry fields these
    models do not (or no longer) know; rejecting them would make stored data unreadable. Invalid
    values of known fields are still rejected, and :mod:`nodestro_runtime.preflight` checks what a
    run needs.
    """

    model_config = ConfigDict(extra="ignore")


# ---- agent configuration --------------------------------------------------------


class ExampleMessage(_Model):
    """A message placed before the task, e.g. a few-shot example."""

    role: Literal["user", "assistant"]
    content: str


class KnowledgeConfig(_Model):
    """Which knowledge files an agent gets and how.

    ``context`` puts the files into the prompt, ``on_demand`` gives the agent the
    ``files`` tool to load them when needed, ``search`` gives it the ``search``
    tool over their text (RAG with citations).
    """

    file_ids: list[str] = Field(default_factory=list)
    mode: Literal["context", "on_demand", "search"] = "on_demand"
    citations: bool = False


class MemoryConfig(_Model):
    """``run`` keeps memory for one workflow run, ``persistent`` in a named space."""

    mode: Literal["none", "run", "persistent"] = "none"
    space: str | None = None


OutputFieldType = Literal["string", "number", "integer", "boolean", "string_list", "number_list"]


class OutputField(_Model):
    name: str
    type: OutputFieldType = "string"
    description: str = ""
    required: bool = True


class OutputConfig(_Model):
    """Plain text, or a structured result described by fields (a Pydantic model is built from them)."""

    mode: Literal["text", "structured"] = "text"
    name: str = "Result"
    fields: list[OutputField] = Field(default_factory=list)


class QualityCriterion(_Model):
    name: str
    description: str
    required: bool = True


class QualityConfig(_Model):
    """An LLM judge that checks the result and lets the agent revise it (``orkexa.refine``)."""

    enabled: bool = False
    criteria: list[QualityCriterion] = Field(default_factory=list)
    max_revisions: int = Field(default=1, ge=0, le=5)
    judge_model: str = DEFAULT_ASSISTANT_MODEL


ContentKind = Literal["extracted", "generated", "assumption"]


class KnowledgeProposalConfig(_Model):
    """An agent may propose its result (and parts of it) for knowledge; a person always decides."""

    enabled: bool = False
    #: The collection suggested for the proposals (the person can choose another one).
    collection: str = ""
    #: What the result is: information taken from the inputs, the agent's own conclusions, or assumptions.
    content_kind: ContentKind = "generated"


class ToolChoice(_Model):
    #: ``any``: the agent must call some tool (Orkexa's ``required``).
    type: Literal["auto", "none", "any", "tool"] = "auto"
    name: str | None = None


class AgentConfig(_Model):
    """Everything about one agent node; mirrors the arguments of ``orkexa.Agent``.

    ``model`` names the provider and the model (``provider:model``). ``thinking``,
    ``thinking_display``, ``cache_control``, ``cache_ttl``, ``fallbacks`` and ``server_tools``
    are options of the Anthropic API (``orkexa.AnthropicOptions``); for other providers
    they must keep their defaults (see :mod:`nodestro_runtime.agent_builder`).
    """

    name: str = "Agent"
    description: str = ""
    icon: str = "bot"
    model: str = DEFAULT_AGENT_MODEL
    system_prompt: str = "You are a helpful assistant."
    task_template: str = "{{input}}"
    messages: list[ExampleMessage] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    server_tools: list[str] = Field(default_factory=list)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    knowledge_proposals: KnowledgeProposalConfig = Field(default_factory=KnowledgeProposalConfig)
    effort: Effort | None = "medium"
    thinking: ThinkingMode | None = None
    thinking_display: ThinkingDisplay | None = None
    tool_choice: ToolChoice = Field(default_factory=ToolChoice)
    cache_control: bool = True
    cache_ttl: Literal["5m", "1h"] = "5m"
    max_tokens: int = Field(default=8000, ge=1, le=128_000)
    max_tool_rounds: int = Field(default=8, ge=0, le=50)
    max_parallel_tools: int = Field(default=1, ge=1, le=16)
    raise_tool_errors: bool = False
    fallbacks: list[str] = Field(default_factory=list)
    on_refusal: Literal["raise", "return"] = "raise"
    request_options: dict[str, Any] = Field(default_factory=dict)


# ---- workflows ------------------------------------------------------------------


class Position(_Model):
    x: float = 0
    y: float = 0


InputFieldType = Literal["text", "textarea", "number", "date", "boolean", "select", "file"]


class InputField(_Model):
    """One field of a workflow's input form (see :mod:`nodestro_runtime.forms`)."""

    #: Technical key, used in placeholders as ``{{form.<name>}}`` (letters, digits, underscores).
    name: str
    label: str = ""
    type: InputFieldType = "text"
    description: str = ""
    required: bool = False
    #: The preset value (text form: a date as ``YYYY-MM-DD``, a boolean as true/false).
    default: Any = None
    #: The choices of a ``select`` field.
    options: list[str] = Field(default_factory=list)
    #: File fields: accepted formats and/or groups (``pdf``, ``document`` ...; empty: every supported format,
    #: see :mod:`nodestro_runtime.file_formats`).
    accept: list[str] = Field(default_factory=list)
    #: File fields: several files (``False``: one file).
    multiple: bool = True
    #: File fields: most files (``None``: no own limit).
    max_files: int | None = Field(default=None, ge=1, le=100)
    #: File fields: largest file in MB (``None``: the default limit).
    max_size_mb: int | None = Field(default=None, ge=1, le=50)
    #: File fields: how the agents get the files. ``auto`` puts small files into the context and gives an
    #: overview plus the files tool for large ones and archives; ``inline`` puts the content into the
    #: context as far as it fits; ``on_demand`` gives only the overview and the files tool.
    delivery: Literal["auto", "inline", "on_demand"] = "auto"


class StartConfig(_Model):
    default_input: str = ""
    file_ids: list[str] = Field(default_factory=list)
    #: An optional input form; empty keeps the plain free-text input.
    fields: list[InputField] = Field(default_factory=list)


ConditionOperator = Literal[
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "gt",
    "gte",
    "lt",
    "lte",
    "is_true",
    "is_false",
    "in",
    "not_in",
    "is_empty",
    "is_not_empty",
]


class ConditionRule(_Model):
    """One named branch of a decision node: taken when ``field`` compared with ``value`` holds."""

    #: The id of the branch's output port (the ``source_handle`` of its connections).
    id: str = Field(default_factory=lambda: new_id("rule"))
    label: str = ""
    #: Path into the evaluated result, e.g. ``category`` or ``customer.tier``; empty: the whole result.
    field: str = ""
    operator: ConditionOperator = "equals"
    #: A text, a number, or for ``in``/``not_in`` the categories (a list of texts).
    value: Any = None
    case_sensitive: bool = False


class ConditionConfig(_Model):
    """A deterministic decision on an earlier result; no AI request is made."""

    #: The node whose result is evaluated; empty: the single incoming node (passed through as it is).
    source: str | None = None
    rules: list[ConditionRule] = Field(default_factory=list)
    #: ``first``: only the first rule that holds; ``all``: every rule that holds opens its branch.
    mode: Literal["first", "all"] = "first"
    default_label: str = ""
    #: Missing field, wrong type or unreadable result: take the default branch, or fail the node.
    on_invalid: Literal["default", "fail"] = "default"


class ApprovalConfig(_Model):
    """A pause for a person who approves or rejects the results that arrive here."""

    instructions: str = ""
    #: Ids of the nodes whose results the person reviews; empty: everything that arrives.
    show: list[str] = Field(default_factory=list)
    allow_comment: bool = True
    require_comment_on_reject: bool = False
    #: After a rejection: follow the ``rejected`` branch (if connected), or end the run as cancelled.
    on_reject: Literal["branch", "stop"] = "branch"


class SubworkflowConfig(_Model):
    """Another saved workflow, run as one step of this one (same engine, own child run)."""

    workflow_id: str = ""
    #: The pinned version of that workflow; ``None`` takes its latest version when the run starts.
    version: int | None = None
    #: What the sub-workflow gets as its input text (placeholders as in an agent's task).
    input_template: str = "{{input}}"
    #: Values for the sub-workflow's input form: field name -> template.
    values: dict[str, str] = Field(default_factory=dict)


ReportFormat = Literal["md", "json", "csv", "html", "pdf", "docx", "xlsx"]


class ResultFileSpec(_Model):
    """A file the result node writes at the end of a run (see :mod:`nodestro_runtime.reports`)."""

    format: ReportFormat = "md"
    #: The file name without extension; the workflow name when empty.
    name: str = ""
    #: Replace credentials and personal data by placeholders (like a sanitized export).
    sanitize: bool = False


class OutputNodeConfig(_Model):
    files: list[ResultFileSpec] = Field(default_factory=list)


class Node(_Model):
    id: str = Field(default_factory=lambda: new_id("node"))
    type: NodeType = "agent"
    position: Position = Field(default_factory=Position)
    label: str = ""
    agent: AgentConfig | None = None
    start: StartConfig | None = None
    condition: ConditionConfig | None = None
    approval: ApprovalConfig | None = None
    subworkflow: SubworkflowConfig | None = None
    output: OutputNodeConfig | None = None


#: The most iterations a loop may allow (the first run included).
MAX_LOOP_ITERATIONS = 20


class EdgeLoop(_Model):
    """Makes a connection a loop: following it runs the nodes from its target to its source again.

    A loop leaves a decision or approval node through one of its branches and leads back to a node
    before it. ``max_iterations`` is mandatory and counts the first run; ``on_exhausted`` says what
    happens when the branch is taken once more: ``fail`` ends the run as failed
    (``run.loop_exhausted``), ``continue`` ignores the loop, so the node's other open branches decide.
    """

    max_iterations: int = Field(ge=2, le=MAX_LOOP_ITERATIONS)
    on_exhausted: Literal["fail", "continue"] = "fail"


class Edge(_Model):
    id: str = Field(default_factory=lambda: new_id("edge"))
    source: str
    target: str
    #: The output port of a branching source: a rule id or ``default`` of a decision node,
    #: ``approved`` or ``rejected`` of an approval node; ``None`` for the single output of other nodes.
    source_handle: str | None = None
    #: Set for a loop connection (see :class:`EdgeLoop`); ``None`` for an ordinary one.
    loop: EdgeLoop | None = None


class WorkflowIn(_Model):
    #: The default name is part of the published API schema (OpenAPI) and of stored workflows; it stays German.
    name: str = "Neuer Workflow"
    description: str = ""
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    budget_usd: float | None = Field(default=None, ge=0)


class Workflow(WorkflowIn):
    id: str = Field(default_factory=lambda: new_id("wf"))
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class WorkflowSummary(_Model):
    id: str
    name: str
    description: str
    agents: int
    updated_at: datetime
    last_run_status: RunStatus | None = None


class AgentTemplateIn(_Model):
    agent: AgentConfig


class AgentTemplate(AgentTemplateIn):
    id: str = Field(default_factory=lambda: new_id("tpl"))
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


# ---- runs -----------------------------------------------------------------------


class Usage(_Model):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    thinking_tokens: int = 0
    web_search_requests: int = 0
    total_tokens: int = 0
    #: The complete cost; ``None`` when part of it is unknown (see ``known_cost_usd``), never 0 for "unknown".
    estimated_cost_usd: float | None = 0.0
    # Price references (PRICING-1). Usage stored before they existed has the defaults: ``cost_complete`` is then
    # ``None`` ("not recorded"), and the cost is read from ``estimated_cost_usd`` alone.
    #: The part of the cost that is known: a lower bound when ``cost_complete`` is ``False``.
    known_cost_usd: float | None = None
    #: Whether every request was fully priced; ``None`` for usage recorded before price references existed.
    cost_complete: bool | None = None
    #: Requests whose cost is unknown or incomplete.
    unpriced_requests: int = 0
    #: Requests priced with a price that was stale (not checked for ``warn_after``) at the time.
    stale_price_requests: int = 0
    #: Cost components that could not be priced (``input:openai:gpt-x``, ``fee:web_search_call``, ...).
    missing_cost_components: list[str] = Field(default_factory=list)
    #: The price snapshot versions the requests were priced with.
    pricing_versions: list[str] = Field(default_factory=list)
    #: The sources of the prices used (``builtin``, ``litellm``, an override's source).
    pricing_sources: list[str] = Field(default_factory=list)
    #: The lowest quality among the prices used (``verified``, ``contract``, ``unverified``, ``estimated``).
    pricing_quality: str | None = None


class ToolCallRecord(_Model):
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: str | None = None
    is_error: bool = False
    started_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    #: Where the tool came from, for tools outside Nodestro (MCP: ``source``, ``server_id``, ``server_name``,
    #: ``qualified_name``, ``definition_hash``, ``access``, ``external_cost`` = ``unknown``); empty otherwise.
    provenance: dict[str, Any] = Field(default_factory=dict)


class EvaluationRecord(_Model):
    passed: bool
    attempts: int
    feedback: str


class DecisionRecord(_Model):
    """How a decision node decided: the value it saw and the branches it opened."""

    #: The opened output ports (rule ids and/or ``default``).
    branches: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    #: The value the rules were checked against (shortened).
    value: Any = None
    #: Why the default branch was taken, e.g. ``condition.field_missing``; ``None`` when a rule held.
    fallback_code: str | None = None
    fallback_params: dict[str, Any] = Field(default_factory=dict)
    #: Rule id -> True/False/None (``None``: not evaluable).
    results: dict[str, bool | None] = Field(default_factory=dict)


class ApprovalRecord(_Model):
    """A person's decision at an approval node; nothing is ever approved automatically."""

    decision: Literal["approved", "rejected"]
    comment: str = ""
    decided_at: datetime = Field(default_factory=now)
    #: Who decided: ``local`` (Nodestro has no user accounts: the user of this installation) or
    #: ``api:<key id>`` for a decision sent to a published workflow API.
    decided_by: str = "local"
    #: Set when the decision was taken over from an earlier run when continuing it.
    reused_from: str | None = None


class ProvidedFile(_Model):
    """A file an agent received, and how (shown in the trace)."""

    file_id: str
    name: str
    format: str | None = None
    #: ``input``: uploaded for this run; ``knowledge``: a knowledge file given to the run.
    source: Literal["input", "knowledge"] = "input"
    #: ``text`` (content as text), ``document`` (PDF as document), ``image``, ``ocr_text`` (text recognised in
    #: an image, not the image itself), ``overview`` (structure only, content via the files tool) or
    #: ``unavailable`` (e.g. an image for a model without image input).
    mode: Literal["text", "document", "image", "ocr_text", "overview", "unavailable"]
    chars: int = 0
    #: Codes such as ``input.context_limit`` or ``input.model_no_images``.
    notes: list[str] = Field(default_factory=list)


class KnowledgeProposal(_Model):
    """Content an agent proposed for knowledge; stored there only after a person confirmed it."""

    id: str = Field(default_factory=lambda: new_id("prop"))
    title: str
    content: str
    content_kind: ContentKind = "generated"
    #: ``result``: the agent's whole result; ``tool``: proposed by the agent with ``propose_knowledge``.
    origin: Literal["result", "tool"] = "result"
    collection: str = ""
    created_at: datetime = Field(default_factory=now)
    #: The knowledge file it became, once a person adopted it.
    adopted_file_id: str | None = None


class NodeRun(_Model):
    status: NodeStatus = "waiting"
    prompt: str | None = None
    output: Any = None
    text: str = ""
    thinking: str = ""
    #: The technical error text (English) and its code/params, which the UI shows in its own language.
    error: str | None = None
    error_code: str | None = None
    error_params: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    evaluation: EvaluationRecord | None = None
    memory: dict[str, str] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Decision nodes: what was decided.
    decision: DecisionRecord | None = None
    #: Approval nodes: the person's decision (``None`` while waiting).
    approval: ApprovalRecord | None = None
    #: Sub-workflow nodes: the child run and the version of the sub-workflow it ran.
    child_run_id: str | None = None
    child_version: int | None = None
    #: The result was taken over from this earlier run instead of executing the node again.
    reused_from: str | None = None
    #: Hash of everything the node's result depends on (configuration, inputs' sources, tools);
    #: continuing a run reuses a result only when it is unchanged.
    fingerprint: str | None = None
    #: Agents: the files the agent received and how.
    files: list[ProvidedFile] = Field(default_factory=list)
    #: Agents: content proposed for knowledge (nothing is stored without a person's confirmation).
    proposals: list[KnowledgeProposal] = Field(default_factory=list)
    #: Inside loops: the iteration of every loop around the node, outermost first, counted from 0
    #: (empty outside loops). The state above is the node's latest execution.
    iteration: list[int] = Field(default_factory=list)
    #: Inside loops: the node's earlier executions, oldest first (each without its own ``earlier``).
    earlier: list[NodeRun] = Field(default_factory=list)


class SanitizeReport(_Model):
    """What a sanitized trace export removed; travels in the exported file as ``sanitized``."""

    at: datetime = Field(default_factory=now)
    personal_data: bool
    contents: bool
    #: Number of replacements per category (api_key, token, email, content, ...).
    replaced: dict[str, int] = Field(default_factory=dict)


class RunIn(_Model):
    input: str = ""
    file_ids: list[str] = Field(default_factory=list)
    #: Values of the workflow's input form (field name -> value); empty without a form.
    values: dict[str, Any] = Field(default_factory=dict)


class Run(_Model):
    id: str = Field(default_factory=lambda: new_id("run"))
    workflow_id: str
    workflow_name: str
    workflow: Workflow
    input: str = ""
    file_ids: list[str] = Field(default_factory=list)
    #: The values of the input form as entered (validated, typed).
    values: dict[str, Any] = Field(default_factory=dict)
    #: With a form: the free text entered next to it (``input`` combines both).
    text_input: str | None = None
    status: RunStatus = "queued"
    error: str | None = None
    error_code: str | None = None
    error_params: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    nodes: dict[str, NodeRun] = Field(default_factory=dict)
    #: The ``orkexa.flow`` state of the run (invocations, loop counters, stop); ``None`` for runs of earlier
    #: versions, whose state is derived from ``nodes`` (:mod:`nodestro_runtime.flow_driver`).
    flow_state: dict[str, Any] | None = None
    usage: Usage = Field(default_factory=Usage)
    created_at: datetime = Field(default_factory=now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Set for runs imported from an exported trace: they are shown, never executed.
    imported_at: datetime | None = None
    source_run_id: str | None = None
    #: Set when the trace came from a sanitized export (see :mod:`nodestro_runtime.sanitize`).
    sanitized: SanitizeReport | None = None
    trigger: RunTrigger = "manual"
    #: The schedule, batch or test series that started the run.
    trigger_ref: str | None = None
    #: A continued run: the failed run whose results it reused (that run stays as it was).
    resumed_from: str | None = None
    #: A sub-workflow's run: the run and node it belongs to (its cost is counted there).
    parent_run_id: str | None = None
    parent_node_id: str | None = None
    #: The saved version of the workflow this run executed, when known.
    workflow_version: int | None = None
    #: A run of a published API: the immutable release it executed (see :mod:`nodestro_api.publishing`).
    release_id: str | None = None
    release_number: int | None = None
    #: The API key that started it (its id, never the key itself).
    api_key_id: str | None = None


class RunSummary(_Model):
    id: str
    workflow_id: str
    workflow_name: str
    status: RunStatus
    input: str
    usage: Usage
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    error_code: str | None = None
    error_params: dict[str, Any] = Field(default_factory=dict)
    imported_at: datetime | None = None
    source_run_id: str | None = None
    sanitized: SanitizeReport | None = None
    trigger: RunTrigger = "manual"
    trigger_ref: str | None = None
    resumed_from: str | None = None
    parent_run_id: str | None = None
    workflow_version: int | None = None
    release_id: str | None = None
    release_number: int | None = None


class RunEvent(_Model):
    """One live event of a run, sent to the browser via Server-Sent Events."""

    seq: int = 0
    run_id: str
    type: str
    node_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=now)


# ---- settings -------------------------------------------------------------------


class SettingsIn(_Model):
    #: Changes of the AI providers' settings (keys, endpoints, declared capabilities), by provider id.
    providers: dict[str, ProviderSettingsIn] | None = None
    #: Anthropic's key as a single field (same as ``providers.anthropic.api_key``).
    api_key: str | None = None
    default_model: str | None = None
    assistant_model: str | None = None
    default_budget_usd: float | None = Field(default=None, ge=0)
    sandbox: Literal["subprocess", "docker"] | None = None
    #: The interface language the user chose; ``None`` follows the browser.
    language: Language | None = None
    #: Cost limit of one Workflow Architect job in USD (``None``: no limit).
    architect_budget_usd: float | None = Field(default=None, ge=0)
    #: How often the Workflow Architect may ask the AI to correct a configuration or a tool.
    architect_max_repairs: int | None = Field(default=None, ge=0, le=5)
    #: Recognise scanned PDFs with local OCR when they are uploaded.
    ocr_enabled: bool | None = None
    #: ``standard`` or ``strict``: whether requests without a known (or with a long unchecked) price are refused.
    price_mode: Literal["standard", "strict"] | None = None


class BuiltinSandboxStatus(_Model):
    """Whether the built-in Linux sandbox isolates AI-generated code on this computer (desktop profile only)."""

    available: bool = False
    #: Why not: ``platform``, ``untested-system``, ``landlock``, ``seccomp``, ``root``, ``paths``, ``verify``,
    #: ``failed``, or ``server`` (a server or Nodestro Local Docker: tool code runs in containers only); empty when
    #: available.
    reason: str = ""
    #: The technical detail in English (for support), e.g. "Landlock not enabled in this kernel".
    detail: str = ""
    #: The distribution, e.g. "ubuntu 24.04".
    system: str = ""


class Settings(_Model):
    #: Every AI provider with its configuration state (never the keys themselves).
    providers: list[ProviderStatus] = Field(default_factory=list)
    #: The provider of new agents: the provider of ``default_model``.
    default_provider: str = "anthropic"
    default_model: str = DEFAULT_AGENT_MODEL
    assistant_model: str = DEFAULT_ASSISTANT_MODEL
    default_budget_usd: float | None = 2.0
    sandbox: Literal["subprocess", "docker"] = "subprocess"
    docker_available: bool = False
    #: The built-in Linux sandbox: with it, AI-generated code runs isolated without Docker.
    builtin_sandbox: BuiltinSandboxStatus = Field(default_factory=BuiltinSandboxStatus)
    language: Language | None = None
    architect_budget_usd: float | None = 2.0
    architect_max_repairs: int = 2
    ocr_enabled: bool = False
    #: Whether tesseract and pdftoppm are installed.
    ocr_available: bool = False
    #: How unknown and stale prices are treated (PRICING-1): ``standard`` warns, ``strict`` refuses.
    price_mode: Literal["standard", "strict"] = "standard"
