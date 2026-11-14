"""Pydantic models shared by the API, the storage and the execution engine.

Everything a user configures in the editor is described here. The models are
plain data: :mod:`nodestro_runtime.agent_builder` turns an :class:`AgentConfig` into an
``orkexa.Agent``, :mod:`nodestro_runtime.engine` runs a :class:`Workflow`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .providers import ProviderSettingsIn, ProviderStatus
from .tool_policy import ToolBinding

Effort = Literal["low", "medium", "high", "xhigh", "max"]
ThinkingMode = Literal["adaptive", "between_tools", "disabled"]
ThinkingDisplay = Literal["summarized", "omitted", "updates"]
NodeType = Literal["start", "agent", "output", "condition", "approval", "subworkflow", "orchestrator"]
#: ``paused``: the run waits for a human decision: at an approval node, at a tool call (M6b), or about its budget
#: (BUDGET-1; then ``Run.budget.pending_request_id`` is set and nodes wait as ``awaiting_budget``).
RunStatus = Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"]
#: ``skipped``: deliberately not run, because its branch was not chosen (no error);
#: ``blocked``: not run, because a predecessor failed or was cancelled;
#: ``awaiting_approval``: an approval node waiting for a person;
#: ``awaiting_budget``: stopped before a model request until a person raises the run's budget or ends the run
#: (:mod:`nodestro_runtime.run_budget`).
NodeStatus = Literal[
    "idle",
    "waiting",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "blocked",
    "awaiting_approval",
    "awaiting_budget",
    "cancelled",
]
#: Node statuses that wait for a person: the node is suspended and continues after the decision.
WAITING_NODE_STATUSES: frozenset[str] = frozenset({"awaiting_approval", "awaiting_budget"})
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
    return datetime.now(UTC)


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

    ``retrieval`` is how ``search`` ranks: ``default`` follows the workspace setting, ``fts`` full text,
    ``vector`` semantic search (needs the semantic index), ``hybrid`` both.
    """

    file_ids: list[str] = Field(default_factory=list)
    mode: Literal["context", "on_demand", "search"] = "on_demand"
    citations: bool = False
    retrieval: Literal["default", "fts", "vector", "hybrid"] = "default"


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
    #: Where a number must come from (agents' results only; a contract ignores it). ``model``: the agent's answer.
    #: ``tool``: a key figure that later steps rely on; it must be the result of a tool call of this step or an
    #: earlier one (:mod:`nodestro_runtime.value_checks`), otherwise the step fails with ``run.value_unverified``.
    #: ``ledger`` (a ``string`` or ``string_list`` field): every value must name a source of the run's source ledger,
    #: by id (``S3``) or URL (:mod:`nodestro_runtime.source_ledger`), otherwise the step fails with
    #: ``output.unknown_source`` after one correction. ``ledger_cited`` (text): every statement of the field cites
    #: the sources it rests on as ``[S1]`` or ``[S1, S3]``, ids of the ledger, checked the same way.
    #: ``ledger_list`` and ``ledger_range`` (text) are filled by the runtime after the step, never by the model (they
    #: are not part of the answer format the model gets): the cited sources with id, title, address and retrieval
    #: time, and the time range in which they were retrieved (:func:`nodestro_runtime.source_ledger.fill_from_ledger`).
    source: Literal["model", "tool", "ledger", "ledger_cited", "ledger_list", "ledger_range"] = "model"


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


#: The highest ``max_tokens`` a configuration may hold (a typo guard; the model's own maximum is checked separately).
MAX_TOKENS_BOUND = 1_000_000


class ToolSettings(_Model):
    """How an agent binds one built-in tool (NAL-A1, ADR-0001): the service connection it uses and its binding
    parameters (validated against the tool's ``params_schema``). Never visible to the model."""

    connection_id: str | None = Field(default=None, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class TemplateRef(_Model):
    """An agent template in one of its versions (NAL-B1): where an agent of a workflow was copied from."""

    template_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)


#: How an assigned skill reaches the model (NAL-B3): ``always`` puts its instructions into the system prompt,
#: ``on_demand`` only its name and description; the model loads the instructions with the ``load_skill`` tool.
SkillMode = Literal["always", "on_demand"]
#: The most skills one agent may have.
MAX_AGENT_SKILLS = 20


class SkillBinding(_Model):
    """A skill assigned to an agent in one fixed version (NAL-B3). A newer version is never used automatically."""

    skill_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    mode: SkillMode = "on_demand"


class AgentConfig(_Model):
    """Everything about one agent node; mirrors the arguments of ``orkexa.Agent``.

    ``model`` names the provider and the model (``provider:model``). ``thinking``,
    ``thinking_display``, ``thinking_budget``, ``cache_control``, ``cache_ttl``, ``fallbacks`` and
    ``server_tools`` depend on what the model supports (:mod:`nodestro_runtime.model_profile`); a model that
    cannot use one refuses a value other than its default (see :mod:`nodestro_runtime.agent_builder`).

    ``max_tokens`` is the most output tokens per model response (reasoning counts against it); it may not exceed
    the model's documented or declared maximum (``GET /api/models/profile``). The upper bound here only catches
    typos: no model accepts more.
    """

    name: str = "Agent"
    description: str = ""
    icon: str = "bot"
    model: str = DEFAULT_AGENT_MODEL
    system_prompt: str = "You are a helpful assistant."
    task_template: str = "{{input}}"
    messages: list[ExampleMessage] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    #: When the agent asks before calling a tool (S-2), by tool name; a tool without an entry is a new binding
    #: (``auto``). Only applies while per-call approvals are switched on.
    tool_policies: dict[str, ToolBinding] = Field(default_factory=dict)
    #: Connection and parameters of built-in tools, by tool name (only tools with a spec; see ``tool_spec``).
    tool_settings: dict[str, ToolSettings] = Field(default_factory=dict)
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
    max_tokens: int = Field(default=8000, ge=1, le=MAX_TOKENS_BOUND)
    #: A thinking token budget for models that take one instead of an effort (Gemini 2.5); ``None``: the provider's
    #: default. Sent as ``thinking_config.thinking_budget``.
    thinking_budget: int | None = Field(default=None, ge=0, le=MAX_TOKENS_BOUND)
    max_tool_rounds: int = Field(default=8, ge=0, le=50)
    max_parallel_tools: int = Field(default=1, ge=1, le=16)
    raise_tool_errors: bool = False
    fallbacks: list[str] = Field(default_factory=list)
    on_refusal: Literal["raise", "return"] = "raise"
    request_options: dict[str, Any] = Field(default_factory=dict)
    #: The task profile of the agent (a key of :data:`nodestro_runtime.local_models.TASKS`, e.g. ``tool_agent``):
    #: what kind of work it does, so a model can be suggested for it. ``None``: not stated.
    task: str | None = Field(default=None, max_length=40)
    #: The agent template version this agent was made from (NAL-B1); history, never followed automatically.
    origin: TemplateRef | None = None
    #: Skills of the skill library in fixed versions (NAL-B3). They add instructions, never tools or permissions.
    skills: list[SkillBinding] = Field(default_factory=list, max_length=MAX_AGENT_SKILLS)


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


#: Most teams in the pool of one orchestrator, and most decisions it may make.
MAX_POOL = 20
MAX_DECISIONS = 50
#: Most teams an orchestrator decision may start at the same time.
MAX_PARALLEL_TEAMS = 5


class OrchestratorMember(_Model):
    """A team in an orchestrator's pool: a saved workflow, run as a sub-workflow when chosen."""

    workflow_id: str = ""
    #: The pinned version; ``None`` takes the latest version when the run starts.
    version: int | None = None
    #: Filled in when the team was selected by capability (``OrchestratorConfig.select``): the matching tags.
    matched: list[str] = Field(default_factory=list)


def _clean_tags(value: list[str]) -> list[str]:
    """Capability tags trimmed, without empty ones and unique ignoring case."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = " ".join(item.split())
        if not tag:
            continue
        if len(tag) > MAX_CAPABILITY_LENGTH:
            raise ValueError(f"a capability has at most {MAX_CAPABILITY_LENGTH} characters")
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            cleaned.append(tag)
    return cleaned


class OrchestratorSelect(_Model):
    """Pool selection by capability (AO M5): saved workflows whose capabilities match join the pool.

    The selection is made when a run (or a published release) pins its workflows, so it is fixed and
    visible in the run's snapshot: every selected member names the tags it matched. Teams are ordered
    by the number of matching tags, then by name; no score is invented. Workflows with errors and the
    workflow itself are left out. Teams with approval steps are offered: the orchestrator waits while
    one of them waits for a person (AO M6b).
    """

    capabilities: list[str] = Field(default_factory=list, max_length=10)
    #: At most this many selected teams (the explicit members come first and count towards ``MAX_POOL``).
    max_teams: int = Field(default=5, ge=1, le=MAX_POOL)
    #: Set in a pinned snapshot once the selection was made: pinning it again (a team started by a run, a
    #: published release) keeps the selected members and never adds teams saved later.
    done: bool = False

    @field_validator("capabilities")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        return _clean_tags(value)


class OrchestratorConfig(_Model):
    """An AI model that delegates the task to teams of its pool until it can answer (AO M4).

    It only selects: it creates no agents and no tools, and has no tools of its own. Each chosen team
    runs as a linked child run with its own budget, limits and tool approvals; the orchestrator's
    decisions, reasons and the teams' results are recorded (``NodeRun.orchestration``).
    """

    members: list[OrchestratorMember] = Field(default_factory=list, max_length=MAX_POOL)
    #: What the orchestrator should do; placeholders as in an agent's task.
    task_template: str = "{{input}}"
    #: Extra instructions for the orchestrator (how to split work, what a good answer is).
    instructions: str = ""
    model: str = "anthropic:claude-opus-5-5"
    max_decisions: int = Field(default=8, ge=1, le=MAX_DECISIONS)
    #: How often one team may be chosen; ``None``: only ``max_decisions`` limits it.
    max_per_member: int | None = Field(default=None, ge=1, le=MAX_DECISIONS)
    #: Add teams by capability when the run starts (AO M5); ``None``: only ``members``.
    select: OrchestratorSelect | None = None
    #: How many teams one decision may start at the same time (AO M6b); 1: one team at a time.
    max_parallel: int = Field(default=1, ge=1, le=MAX_PARALLEL_TEAMS)
    #: The orchestrator assembles its own team from the pool while it runs and may change it (AO M6b);
    #: ``False``: every pool team can get work at any time.
    staffing: bool = False
    #: The largest team at any time (with ``staffing``).
    max_team: int = Field(default=5, ge=1, le=MAX_POOL)
    #: The orchestrator may fill a team's start form fields (except files) besides its task (AO-5 level 2);
    #: the values are checked like a manual start of that team.
    allow_values: bool = False
    #: Who may give the orchestrator instructions while it runs (live steering, LRC-1): ``attended`` (default) only in
    #: runs a person started (not by a published API, schedule, batch or test series), ``always`` also in those,
    #: ``off`` never. Questions about the run (read-only) are always possible. Not part of the node's fingerprint.
    live_steering: Literal["attended", "always", "off"] = "attended"


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
    #: The fields the workflow promises to deliver (see :mod:`nodestro_runtime.contracts`); empty: no contract.
    contract: list[OutputField] = Field(default_factory=list, max_length=50)


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
    orchestrator: OrchestratorConfig | None = None


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


#: Most branches of one run that may execute at the same time when a workflow sets a limit.
MAX_PARALLEL_LIMIT = 32
#: Longest wall time a workflow may allow for its own execution (7 days, in seconds).
MAX_DURATION_LIMIT_S = 7 * 24 * 3600
#: Most node executions a workflow may allow for one run.
MAX_STEPS_LIMIT = 10_000
#: Most capability tags of a workflow, and the longest tag.
MAX_CAPABILITIES = 20
MAX_CAPABILITY_LENGTH = 40


class WorkflowLimits(_Model):
    """Execution limits of a workflow's runs (Adaptive Orchestration M2); ``None``: no limit of this kind."""

    #: Most nodes of a run executing at the same time; further ready nodes wait for a free slot.
    max_parallel: int | None = Field(default=None, ge=1, le=MAX_PARALLEL_LIMIT)
    #: Longest active execution time of a run in seconds; waiting for approvals does not count. When it is
    #: reached, running nodes are cancelled and the run fails with ``run.time_limit``.
    max_duration_s: float | None = Field(default=None, gt=0, le=MAX_DURATION_LIMIT_S)
    #: Most node executions of a run. Every executed node counts once, start and result nodes included, and
    #: every round of a loop counts again. When the next execution would exceed it, the run fails with
    #: ``run.step_limit``.
    max_steps: int | None = Field(default=None, ge=1, le=MAX_STEPS_LIMIT)


class WorkflowIn(_Model):
    #: The default name is part of the published API schema (OpenAPI) and of stored workflows; it stays German.
    name: str = "Neuer Workflow"
    description: str = ""
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    budget_usd: float | None = Field(default=None, ge=0)
    limits: WorkflowLimits = Field(default_factory=WorkflowLimits)
    #: What the workflow can do as a team, e.g. ``research`` or ``translate`` (up to 20 short tags). They travel
    #: with the workflow (versions, export) and help to find a fitting team; they grant nothing.
    capabilities: list[str] = Field(default_factory=list, max_length=MAX_CAPABILITIES)

    @field_validator("capabilities")
    @classmethod
    def _clean_capabilities(cls, value: list[str]) -> list[str]:
        return _clean_tags(value)


class WorkflowOrigin(_Model):
    """Where a workflow came from when it was made as a copy (RT-055): today a copy of an example.

    Set once by the code that makes the copy and kept by every later save (:func:`keep_origin`): it is history, not
    something an editor or a client sends (``WorkflowIn`` has no such field). An interface uses it to mark a copy
    whose integrations run on sample data, or to offer the example's newer version."""

    kind: Literal["example"] = "example"
    #: The example (template) id, e.g. ``offer-from-inquiry``.
    example_id: str
    #: The version of the examples package the copy was made from.
    version: str
    #: The language the copy was made in.
    language: str
    #: Integrations of the example that run in their offline mode on sample data in this copy.
    offline: list[str] = Field(default_factory=list)
    #: Integrations of the example that were connected to an MCP server.
    connected: list[str] = Field(default_factory=list)
    copied_at: datetime = Field(default_factory=now)


class Workflow(WorkflowIn):
    id: str = Field(default_factory=lambda: new_id("wf"))
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    #: Where the workflow came from (a copy of an example), read-only; ``None`` for workflows made otherwise.
    origin: WorkflowOrigin | None = None


def keep_origin(workflow: Workflow, previous: Workflow | None) -> Workflow:
    """``workflow`` with the origin of the stored ``previous`` state: a save never drops or replaces it (RT-055)."""
    if previous is None or previous.origin is None or workflow.origin == previous.origin:
        return workflow
    return workflow.model_copy(update={"origin": previous.origin})


class WorkflowSummary(_Model):
    id: str
    name: str
    description: str
    agents: int
    updated_at: datetime
    last_run_status: RunStatus | None = None
    #: The workflow's capability tags and the field names of its output contract (teams, AO M3).
    capabilities: list[str] = Field(default_factory=list)
    contract: list[str] = Field(default_factory=list)


#: What an agent template may say it needs of its model (NAL-B1).
TemplateCapability = Literal["tools", "vision", "structured_output", "long_context"]
#: Where an agent template comes from: ``user`` (made or changed here), ``builtin:<package>@<version>/<id>`` (shipped
#: with an examples package) or ``import:<sha256 of the imported file>``.
TEMPLATE_ORIGIN_PATTERN = r"^(user|builtin:[a-z0-9_.-]+@[A-Za-z0-9_.+-]+/[a-z0-9-]+|import:[0-9a-f]{64})$"


class TemplateRequirements(_Model):
    """What an agent template needs to work; checked before it is used, never enforced by the runtime."""

    #: What the model must support.
    capabilities: list[TemplateCapability] = Field(default_factory=list, max_length=4)
    #: The weakest model tier the template works with (``None``: any).
    min_tier: Literal["fast", "standard", "strong"] | None = None
    #: Tools the agent uses (catalog names).
    tools: list[str] = Field(default_factory=list, max_length=50)
    #: Kinds of service connection the tools need (e.g. ``search``).
    services: list[str] = Field(default_factory=list, max_length=20)
    #: Whether the agent works without internet access (with a local model).
    offline_capable: bool = True


class TemplateTest(_Model):
    """A test case of an agent template: an input and what a good answer does (checked by a judge or a person)."""

    name: str = Field(min_length=1, max_length=120)
    input: str = Field(max_length=20_000)
    expectations: list[str] = Field(default_factory=list, max_length=20)


class TemplateMeta(_Model):
    """The description of an agent template besides its agent (NAL-B1)."""

    category: str = Field(default="", max_length=60)
    summary: str = Field(default="", max_length=500)
    requirements: TemplateRequirements = Field(default_factory=TemplateRequirements)
    origin: str = Field(default="user", pattern=TEMPLATE_ORIGIN_PATTERN)
    #: Example requests to try the agent with.
    examples: list[str] = Field(default_factory=list, max_length=20)
    tests: list[TemplateTest] = Field(default_factory=list, max_length=50)
    #: The language of the template's texts (``de``, ``en``, ...); ``None``: not stated.
    locale: str | None = Field(default=None, max_length=10)


# ---- skills (NAL-B3) -------------------------------------------------------------
#: Where a skill comes from: ``user`` (made in Nodestro), ``builtin:<package>@<version>/<name>`` (copied from the
#: shipped skills) or ``import:<sha256 of the imported file>``.
SKILL_ORIGIN_PATTERN = r"^(user|builtin:[a-z0-9_.-]+@[A-Za-z0-9_.+-]+/[a-z0-9-]+|import:[0-9a-f]{64})$"
SkillStatus = Literal["active", "disabled", "archived"]


class SkillFileRef(_Model):
    """A file of a skill version: its path in the package, size and SHA-256 (the content is stored by hash)."""

    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Skill(_Model):
    """A skill of the library in its latest version (NAL-B3). ``name`` is unique in a workspace."""

    id: str = Field(default_factory=lambda: new_id("skl"))
    name: str
    description: str = ""
    status: SkillStatus = "active"
    version: int = Field(default=1, ge=1)
    #: The language of the skill's content (``metadata.language`` of the package), ``None``: not stated.
    language: str | None = Field(default=None, max_length=10)
    origin: str = Field(default="user", pattern=SKILL_ORIGIN_PATTERN)
    has_scripts: bool = False
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class SkillVersion(_Model):
    """One version of a skill; never changed once stored. ``digest`` covers every path and its bytes."""

    skill_id: str
    version: int = Field(ge=1)
    digest: str
    name: str
    description: str = ""
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: str | None = None
    files: list[SkillFileRef] = Field(default_factory=list)
    origin: str = Field(default="user", pattern=SKILL_ORIGIN_PATTERN)
    note: str = ""
    created_at: datetime = Field(default_factory=now)
    #: Who saved it (the user id on a server, empty on the desktop).
    created_by: str = ""


class AgentTemplateIn(_Model):
    agent: AgentConfig
    meta: TemplateMeta = Field(default_factory=TemplateMeta)
    #: A note of the user for the version this save makes ("shorter answers").
    note: str = Field(default="", max_length=200)


class AgentTemplate(_Model):
    """A saved agent template in its latest version; every save that changes it makes a new version (NAL-B1).

    A template stored before versions existed has ``version`` 1 and ``meta`` with ``origin`` ``user``."""

    agent: AgentConfig
    meta: TemplateMeta = Field(default_factory=TemplateMeta)
    id: str = Field(default_factory=lambda: new_id("tpl"))
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class AgentTemplateVersion(_Model):
    """One version of an agent template; never changed once stored."""

    template_id: str
    version: int = Field(ge=1)
    agent: AgentConfig
    meta: TemplateMeta = Field(default_factory=TemplateMeta)
    note: str = ""
    created_at: datetime = Field(default_factory=now)
    #: Who saved it (the user id on a server, empty on the desktop).
    created_by: str = ""


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
    #: Requests to local models (Ollama): no external API billing, so their API cost is 0; the local operating costs
    #: (hardware, energy) are not recorded. Show them as such, never as a free total.
    local_requests: int = 0
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
    #: Requests a budget admitted without capping their cost (AO-12): no upper bound (no price, native tools,
    #: images, binary documents) or one larger than the rest of the budget, admitted alone.
    uncapped_requests: int = 0
    #: The provider regions the requests went to, for providers billed per region (Amazon Bedrock: ``eu-central-1``);
    #: empty when there were none or the region is not known (usage recorded before RT-015).
    regions: list[str] = Field(default_factory=list)


class ToolCallApproval(_Model):
    """What the per-call approval rules did with one call."""

    #: ``allowed`` (no question), ``pending`` (waiting), ``approved``, ``rejected``, ``expired``, ``cancelled`` or
    #: ``denied`` (refused without a question).
    status: Literal["allowed", "pending", "approved", "rejected", "expired", "cancelled", "denied"]
    #: The stored request (collection ``tool_approvals``) when a person was asked.
    request_id: str | None = None
    #: The denial's code (``tool.not_granted``, ``tool.approval_unattended``, ``tool.approval_capacity``,
    #: ``tool.approval_rejected``, ``tool.approval_timeout``).
    code: str | None = None
    deadline: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None
    #: The pre-grant that allowed an unattended call.
    pre_grant_id: str | None = None
    #: The capability the rules saw (``access``, ``reach``, ``source``).
    capability: dict[str, str] = Field(default_factory=dict)


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
    #: The per-call approval rules' verdict (S-2); ``None`` when no rule applied (approvals off, or Nodestro's own
    #: run tools).
    approval: ToolCallApproval | None = None


class SourceEntry(_Model):
    """One web source a run's tools saw (:mod:`nodestro_runtime.source_ledger`): a search hit or a page that was read.
    The ledger holds addresses and metadata only, never the page text, and it lives in its run: nothing carries it to
    another run (search providers' terms forbid storing results)."""

    #: ``S1``, ``S2`` ... in the order the run first saw the source.
    id: str
    url: str
    #: The address after redirects, when the page was read.
    final_url: str | None = None
    title: str = ""
    #: ``search``: only a search hit; ``fetched``: the page was read (a later read upgrades a hit).
    origin: Literal["search", "fetched"] = "search"
    #: The search provider of a hit (``test``, ``brave`` ...); ``None`` for a page read directly.
    provider: str | None = None
    retrieved_at: datetime = Field(default_factory=now)
    #: SHA-256 of the content that was read (``fetched`` only).
    sha256: str | None = None
    #: The step whose tool call recorded it first.
    node_id: str | None = None


class RunCause(_Model):
    """Why a failed run failed (AD-02): the step that failed first and its problem, so that a run list or a banner
    can say "<step>: <reason>" without reading every step. Set together with ``error_code`` ``run.nodes_failed``;
    ``code``/``params`` are the step's own (``run.budget_exceeded``, ``provider.rate_limited`` ...)."""

    node_id: str
    #: The step's label as the workflow shows it.
    label: str
    code: str
    params: dict[str, Any] = Field(default_factory=dict)


class RunWarning(_Model):
    """Something a run that ended went without (AD-03): e.g. a tool call that was refused, timed out or failed.

    ``code`` is the call's problem code (``tool.approval_rejected``, ``tool.approval_timeout``, ``tool.not_granted``,
    ``mcp.unreachable``, ...; ``tool.failed`` when the tool's error has no code); ``count`` calls of ``tool`` in
    ``node_id`` had it."""

    code: str
    node_id: str
    tool: str | None = None
    count: int = 1


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


class SkillUse(_Model):
    """A skill an agent loaded (``path`` ``None``) or one of its files it read, during one step (NAL-B3)."""

    skill_id: str
    name: str
    version: int
    path: str | None = None


class GuidanceRef(_Model):
    """One instruction of a person that was part of an orchestrator decision's prompt."""

    message_id: str
    step: int
    seq: int = 0


class NodeRun(_Model):
    status: NodeStatus = "waiting"
    prompt: str | None = None
    output: Any = None
    text: str = ""
    #: The reasoning text the provider transmitted while the step ran (never generated; see :mod:`.reasoning`).
    thinking: str = ""
    #: ``thinking`` (the model's reasoning text) or ``summary`` (the provider's summary of it); ``None``: no text.
    thinking_kind: Literal["thinking", "summary"] | None = None
    #: Reasoning phases the provider's stream marked, with or without text (0: none seen).
    thinking_phases: int = 0
    #: A phase arrived only encrypted or omitted: the model reasoned, there is no text to show.
    thinking_redacted: bool = False
    #: The technical error text (English) and its code/params, which the UI shows in its own language.
    error: str | None = None
    error_code: str | None = None
    error_params: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    #: Why the step's model thought less than its effort asks: ``output_limit`` (it spent the answer limit on reasoning
    #: and answered once more without it, see ``limits_handled``), ``cpu_saving`` (the CPU saving mode of the
    #: workspace setting ``local_reasoning``, RT-050); ``None`` when it ran as configured.
    reasoning_reduced: str | None = None
    #: Limits the step's agent reached and continued at instead of failing (RT-067): ``reasoning`` (the model spent
    #: the answer limit on reasoning, so it answered once more without reasoning; ``reasoning_reduced`` is then
    #: ``output_limit``) and ``tool_rounds`` (the last tool round was used up, so the model gave its answer from the
    #: tool results so far instead of calling more tools).
    limits_handled: list[Literal["reasoning", "tool_rounds"]] = Field(default_factory=list)
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
    #: Orchestrator nodes: the record of the orchestration (``orkexa.orchestration.OrchestrationState`` as JSON)
    #: and the child run of every delegation, in order.
    orchestration: dict[str, Any] | None = None
    child_run_ids: list[str] = Field(default_factory=list)
    #: Orchestrator: the instructions of people (live steering, :mod:`nodestro_runtime.run_chat`) that were in the
    #: prompt of a decision: message id and decision index (the messages themselves are stored beside the run).
    guidance: list[GuidanceRef] = Field(default_factory=list)
    #: Orchestrator: the decision the model just made while its teams work (not recorded yet; cleared when it is).
    current_decision: dict[str, Any] | None = None
    #: Orchestrator: the usage of its own decisions, without its teams (they are in ``usage`` as well).
    decision_usage: Usage | None = None
    #: The result was taken over from this earlier run instead of executing the node again.
    reused_from: str | None = None
    #: Hash of everything the node's result depends on (configuration, inputs' sources, tools);
    #: continuing a run reuses a result only when it is unchanged.
    fingerprint: str | None = None
    #: Agents: the files the agent received and how.
    files: list[ProvidedFile] = Field(default_factory=list)
    #: Agents: content proposed for knowledge (nothing is stored without a person's confirmation).
    proposals: list[KnowledgeProposal] = Field(default_factory=list)
    #: Agents: the skills the agent loaded and the skill files it read (NAL-B3), in order.
    skills: list[SkillUse] = Field(default_factory=list)
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


class BudgetRaise(_Model):
    """One approved raise of a run's budget (BUDGET-1); the request in ``budget_requests`` holds the full record."""

    request_id: str
    previous_limit_usd: Decimal
    new_limit_usd: Decimal
    decided_by: str
    decided_at: datetime = Field(default_factory=now)


class RunBudget(_Model):
    """The budget of one run, apart from every configuration it came from (BUDGET-1).

    ``initial_limit_usd`` is the effective limit the run started with (the workflow's budget, else the workspace's
    default budget, or a published release's run limit); ``limit_usd`` is the limit that applies now: the initial one
    or the last approved raise. A raise changes only this run: never the workflow, its saved versions or the
    settings, and never another run. ``None`` limits mean: no limit.
    """

    #: Where the initial limit came from: ``workflow``, ``default`` (workspace setting), ``release`` (a published
    #: API's run limit), ``team`` / ``subworkflow`` (a child run's own workflow budget) or ``none``.
    source: Literal["workflow", "default", "release", "subworkflow", "team", "none"] = "none"
    initial_limit_usd: Decimal | None = None
    limit_usd: Decimal | None = None
    raises: list[BudgetRaise] = Field(default_factory=list)
    #: The budget request that waits for a decision (top-level runs only), ``None`` otherwise.
    pending_request_id: str | None = None


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
    #: With ``error_code`` ``run.nodes_failed``: the step that failed first and why (RT-012); ``None`` otherwise.
    cause: RunCause | None = None
    #: What the run went without although it may have succeeded (AD-03): refused, expired or failed tool calls.
    warnings: list[RunWarning] = Field(default_factory=list)
    output: Any = None
    nodes: dict[str, NodeRun] = Field(default_factory=dict)
    #: The run's source ledger (NAL-A2): every web source its tools saw; empty for runs without web tools.
    sources: list[SourceEntry] = Field(default_factory=list)
    #: The ``orkexa.flow`` state of the run (invocations, loop counters, stop); ``None`` for runs of earlier
    #: versions, whose state is derived from ``nodes`` (:mod:`nodestro_runtime.flow_driver`).
    flow_state: dict[str, Any] | None = None
    usage: Usage = Field(default_factory=Usage)
    #: The budget the run started with in USD (``None``: no limit, or a run of an earlier version).
    budget_usd: float | None = None
    #: Active execution time so far in seconds (time spent waiting for approvals not included); the clock of
    #: the workflow's ``limits.max_duration_s``.
    active_seconds: float = 0.0
    #: Whether the budget was a hard maximum: every request reserved its most expensive outcome in advance and
    #: none was uncapped or unpriced. ``None`` without a budget.
    budget_hard_maximum: bool | None = None
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
    #: The principal that started it (``local`` on a desktop; a user id on a server), when known. A budget raise
    #: needs this principal or an administrator (BUDGET-1).
    started_by: str | None = None
    #: The run's own budget: initial limit, approved raises, pending decision (``None`` before the first start or
    #: for runs of earlier versions).
    budget: RunBudget | None = None


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
    cause: RunCause | None = None
    warnings: list[RunWarning] = Field(default_factory=list)
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
    #: The run budget (BUDGET-1): a set ``pending_request_id`` means the paused run waits for a budget decision.
    budget: RunBudget | None = None


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
    #: ``auto`` (Docker, else AI-generated code is refused), ``linux`` (the built-in Linux sandbox, chosen explicitly),
    #: ``subprocess`` (only code a person wrote, AI-generated code is refused) or ``docker`` (everything in Docker).
    sandbox: Literal["auto", "subprocess", "linux", "docker"] | None = None
    #: The interface language the user chose; ``None`` follows the browser.
    language: Language | None = None
    #: Cost limit of one Workflow Architect job in USD (``None``: no limit).
    architect_budget_usd: float | None = Field(default=None, ge=0)
    #: How often the Workflow Architect may ask the AI to correct a configuration or a tool.
    architect_max_repairs: int | None = Field(default=None, ge=0, le=5)
    #: Output token limits of Nodestro's own AI operations by operation (``nodestro_runtime.output_limits``); a value
    #: replaces the stored one, ``null`` or ``0`` removes it (the built-in default applies again). Keys not sent stay.
    output_limits: dict[str, int | None] | None = None
    #: Whether the Workflow Architect asks once more with a higher output limit when an answer was cut off.
    architect_retry_truncated: bool | None = None
    #: The highest output limit such a retry may use (tokens; the model's maximum and the job's budget still apply).
    architect_retry_max_tokens: int | None = Field(default=None, ge=1024, le=1_000_000)
    #: Recognise scanned PDFs with local OCR when they are uploaded.
    ocr_enabled: bool | None = None
    #: ``standard`` or ``strict``: whether requests without a known (or with a long unchecked) price are refused.
    price_mode: Literal["standard", "strict"] | None = None
    #: The highest limit a person may raise a run's budget to (BUDGET-1); ``None`` keeps the current value. Send
    #: ``clear_max_run_budget`` to remove it.
    max_run_budget_usd: Decimal | None = Field(default=None, ge=0, le=1_000_000, decimal_places=2)
    clear_max_run_budget: bool | None = None
    #: How long a budget decision of an attended run waits before the run ends (hours).
    budget_decision_hours: int | None = Field(default=None, ge=1, le=720)
    #: How knowledge search ranks by default (``fts``, ``vector``, ``hybrid``) and with which embedding model.
    knowledge_retrieval: Literal["fts", "vector", "hybrid"] | None = None
    embedding_model: str | None = None
    #: The strict offline mode: only local models and local tools, no downloads or price updates (D12).
    strict_offline: bool | None = None
    #: Prefer European local models among equally suited ones (never a cloud service, never a weaker model).
    prefer_european_models: bool | None = None
    #: The CPU saving mode of local thinking models (``auto``: on a server without a GPU, effort ``medium`` answers
    #: without the thinking phase; ``full``: always as the agent's effort says).
    local_reasoning: Literal["auto", "full"] | None = None
    #: Ask OpenAI and Gemini for reasoning summaries (they send them only on request), for agents and the built-in AI
    #: functions; shown live where the interface's thinking display allows it.
    reasoning_summaries: bool | None = None


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


class SandboxCheck(_Model):
    """One check behind a backend's state, e.g. ``preflight`` or ``self-test`` of the Docker sandbox."""

    #: ``cli``, ``daemon``, ``preflight``, ``image`` or ``self-test`` (Docker).
    name: str
    ok: bool = False
    #: The technical detail in English (why it failed, or what passed).
    detail: str = ""


class SandboxBackendStatus(_Model):
    """One sandbox backend of the computer the runtime runs on, as the policy sees it."""

    #: ``linux-builtin``, ``macos-seatbelt``, ``windows-native``, ``docker`` or ``subprocess``.
    name: str
    #: Whether it can run on this computer now (its probe passed; for Docker: preflight and self-test passed).
    available: bool = False
    #: ``approved`` (released for AI-generated code here), ``experimental`` (works, but not reviewed or released:
    #: never used; no backend reports it since the macOS sandbox was removed, kept for older clients),
    #: ``unavailable``, ``not-isolating`` (runs only code a person wrote), ``not-applicable``, or ``checking``
    #: (Docker: the first check after the start has not finished yet; nothing runs in it until then).
    state: Literal["approved", "experimental", "unavailable", "not-isolating", "not-applicable", "checking"] = (
        "unavailable"
    )
    #: Why it is not approved, e.g. ``platform``, ``not-released``, ``not-implemented``, ``removed`` (macOS: the
    #: Seatbelt sandbox was removed), ``server``, and for Docker: ``docker-missing`` (no CLI), ``docker-unreachable``
    #: (no daemon), ``docker-limits`` (memory, process or CPU limits not enforced), ``docker-seccomp`` (no seccomp
    #: filter), ``docker-rootless-cgroup`` (rootless on cgroup v1), ``docker-os-type`` (no Linux containers),
    #: ``docker-image`` (the sandbox image cannot be found or built), ``docker-selftest`` (a container of the sandbox's
    #: shape showed a missing restriction) or ``docker-checking`` (with state ``checking``).
    reason: str = ""
    #: The technical detail in English.
    detail: str = ""
    #: ``none``, ``container`` or ``os`` (Orkexa's capability model).
    isolation: str = "none"
    maturity: str = "stable"
    #: How memory is limited: ``kernel``, ``watchdog`` or ``none``.
    memory: str = "none"
    #: Known limitations in English.
    notes: list[str] = Field(default_factory=list)
    #: Docker only (empty elsewhere): the engine as far as it can be told (``docker``, ``docker-desktop``, ``podman``,
    #: ``colima``, ``orbstack``, ``unknown``; informational, never a reason to trust it), whether it runs rootless, its
    #: cgroup version (``"1"``, ``"2"``), the checks behind the state, and when the self-test last ran.
    engine: str = ""
    rootless: bool | None = None
    cgroup_version: str | None = None
    checks: list[SandboxCheck] = Field(default_factory=list)
    tested_at: datetime | None = None


class SandboxSelection(_Model):
    """Where tool code runs now on the computer the runtime runs on (not the browser's)."""

    mode: Literal["auto", "subprocess", "linux", "docker"] = "auto"
    profile: str = "desktop"
    #: ``linux``, ``macos`` or ``windows``, and the CPU (``x86_64``, ``arm64``).
    os: str = ""
    arch: str = ""
    #: The backend that runs AI-generated and other untrusted code; empty when such code is refused.
    untrusted: str = ""
    #: The backend that runs code a person wrote in the tool editor.
    user_authored: str = ""
    #: Whether untrusted code is refused here, with ``reason``: ``no-isolation`` (``subprocess`` and ``linux`` modes),
    #: or (``auto`` and Docker mode, server, local container) the Docker backend's reason, e.g.
    #: ``docker-unreachable``, ``docker-selftest``, ``docker-checking``.
    blocked: bool = False
    reason: str = ""
    detail: str = ""


class Settings(_Model):
    #: Every AI provider with its configuration state (never the keys themselves).
    providers: list[ProviderStatus] = Field(default_factory=list)
    #: The provider of new agents: the provider of ``default_model``.
    default_provider: str = "anthropic"
    default_model: str = DEFAULT_AGENT_MODEL
    assistant_model: str = DEFAULT_ASSISTANT_MODEL
    default_budget_usd: float | None = 2.0
    sandbox: Literal["auto", "subprocess", "linux", "docker"] = "auto"
    docker_available: bool = False
    #: Where tool code runs now and why untrusted code may be refused (cross-platform sandbox architecture).
    sandbox_selection: SandboxSelection = Field(default_factory=SandboxSelection)
    #: The sandbox backends of this computer with their state.
    sandbox_backends: list[SandboxBackendStatus] = Field(default_factory=list)
    #: The built-in Linux sandbox: with it, AI-generated code runs isolated without Docker.
    builtin_sandbox: BuiltinSandboxStatus = Field(default_factory=BuiltinSandboxStatus)
    language: Language | None = None
    architect_budget_usd: float | None = 2.0
    architect_max_repairs: int = 2
    #: Output token limits set for Nodestro's own AI operations (only the overridden ones) and the built-in defaults of
    #: every operation; the model's maximum output caps both (``nodestro_runtime.output_limits``).
    output_limits: dict[str, int] = Field(default_factory=dict)
    output_limit_defaults: dict[str, int] = Field(default_factory=dict)
    architect_retry_truncated: bool = True
    architect_retry_max_tokens: int = 64_000
    ocr_enabled: bool = False
    #: Whether tesseract and pdftoppm are installed.
    ocr_available: bool = False
    #: How unknown and stale prices are treated (PRICING-1): ``standard`` warns, ``strict`` refuses.
    price_mode: Literal["standard", "strict"] = "standard"
    #: The highest limit a budget decision may raise a run's budget to (BUDGET-1; ``None``: no maximum). The limits
    #: workflows start with are not affected.
    max_run_budget_usd: Decimal | None = None
    #: The operator's maximum on a server (``NODESTRO_MAX_RUN_BUDGET_USD``): a tenant cannot lift it.
    operator_max_run_budget_usd: Decimal | None = None
    #: How long a budget decision of an attended run waits before the run ends (hours).
    budget_decision_hours: int = 24
    knowledge_retrieval: Literal["fts", "vector", "hybrid"] = "fts"
    #: The Ollama embedding model of the semantic index (``None``: none chosen yet).
    embedding_model: str | None = None
    strict_offline: bool = False
    #: Whether the model selection prefers European local models among equally suited ones.
    prefer_european_models: bool = False
    #: The CPU saving mode of local thinking models: ``auto`` (default) lets effort ``medium`` answer without the
    #: thinking phase when the local model server has no GPU; effort ``high`` is never reduced. ``full`` turns it off.
    local_reasoning: Literal["auto", "full"] = "auto"
    #: Whether OpenAI and Gemini are asked for reasoning summaries (off by default: OpenAI may require a verified
    #: organization for them). Claude agents use their own ``thinking_display``; local models send their reasoning
    #: without a request.
    reasoning_summaries: bool = False
    #: Whether this installation started with local default models (``local``) or kept its cloud defaults (``kept``).
    model_defaults: Literal["local", "kept"] = "kept"
