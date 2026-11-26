"""Conversations with one's own agents (N-04): a conversation object on top of the one engine.

A conversation talks to one agent template in a fixed version (``agent``) with one model (``model``); both can be
switched between answers. Its parts:

- **Storage**: conversations and their messages are documents of the tenant's store (collections
  :data:`COLLECTION` and :data:`MESSAGES`, message key ``<conversation id>:<seq>``), so every query is bound to the
  tenant like every other document (on PostgreSQL rows of ``nodestro_documents`` with ``tenant_id``; no migration).
  Each conversation also records its tenant and its owner: on a server only its owner sees it (``owner``), on the
  desktop everyone of the workspace (``owner`` ``None``). Who asks without an owner identity on a multi-user
  installation (a principal without a role in the tenant) passes :data:`NO_OWNER`: it sees no conversation and no
  answer run, and starts none (fail closed, W3-S1).
- **Answers**: every answer is one run of the engine (trigger ``conversation``, ``trigger_ref`` = the conversation
  id) with a start step and the agent: there is no second agent loop. The run executes the agent with its tools,
  knowledge, per-call tool approvals (S-2) and access grants (N-06; a grant for "this conversation" names the
  conversation id), and its usage is the run's usage, so it shows in the run list and the usage ledger. The answer
  message references its run (``run_id``) and, while a tool call waits for a person, the approval requests
  (``approval_ids``).
- **History** (N-05): the run's input is the new message after the earlier messages of the conversation, as text,
  fitted into the answer model's context window (:meth:`Conversations.history_input`, with
  :mod:`orkexa.core.compaction`): its window minus the agent's output reserve, its system prompt and tools; at most
  the newest :data:`HISTORY_MESSAGES`. The older messages are replaced by a summary turn that names its range. The
  conversation's ``history`` decides how: ``trim`` (the default) leaves them out with a note, ``summarize`` has a
  helper model (``summary_model``, else the conversation's model) summarize them in a run of its own (trigger
  ``conversation``, so its cost is the conversation's and it obeys the same model rules: an offline conversation or
  agent never uses a cloud helper). A failed summary falls back to trimming. The answer keeps what replaced the
  earlier messages (:class:`HistorySummary`, ``summary``), so the history shows it; the next answer starts from the
  newest summary. Untrusted data (``<untrusted_data>`` frames) never reaches the helper and stays framed with its
  source in the summary.
- **Streaming**: the answer's events are the run's events (:class:`~nodestro_runtime.schemas.RunEvent`); the API
  forwards them unchanged.
- **Cancel**: cancelling an answer cancels its run; the engine closes the model connection (``CancelFlag``,
  RT3-05).
- **Models**: a cloud model is never chosen automatically. Without an explicit model a conversation takes the
  agent's model or the workspace's default model only when it is a local one (``ollama``), otherwise the person has
  to choose (``conversation.model_required``); cloud fallbacks of the agent are dropped. With ``offline`` a
  conversation refuses cloud models.
- **Files**: files are input files of origin ``conversation`` and go to the answer's run as run files, so the run's
  file delivery decides how the model gets them: an image only as an image to a model with image input, otherwise
  it is marked unavailable (:mod:`nodestro_runtime.file_delivery`). The files sent with earlier messages that are
  still in the conversation are offered to the answer as reference files: an overview, read on demand with the files
  tool (RT-089).
- **Export**: :meth:`Conversations.export_markdown`.

The switch :data:`CONVERSATIONS_ENV` (default off) guards the API routes (nodestro-api); this module itself has no
effect until a conversation is created.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import json
import logging
import os
import re
import sys
from typing import TYPE_CHECKING, Any, Literal

from orkexa.core.compaction import (
    SUMMARY_INSTRUCTIONS,
    Compacted,
    Turn,
    acompact,
    compact,
    context_budget,
    estimate_tokens,
)
from orkexa.core.compaction import Strategy as HistoryStrategy
from pydantic import BaseModel, Field

from . import output_limits
from .agent_templates import AgentTemplates
from .engine import add_usage, as_text
from .input_files import InputFile
from .launcher import blocking_problems, launch
from .offline import OfflineScope, cloud_model_error
from .preflight import agent_models
from .problems import NodestroError, Problem, problem
from .providers import check_model, provider_of
from .schemas import (
    AgentConfig,
    Edge,
    Node,
    NotificationRules,
    Run,
    RunIn,
    RunSummary,
    SourceEntry,
    TemplateRef,
    ToolCallApproval,
    Usage,
    Workflow,
    new_id,
    now,
)

if TYPE_CHECKING:
    from .workspace import Workspace

logger = logging.getLogger(__name__)
#: The deletions waiting for an answer's run of a deleted conversation to stop (:meth:`Conversations.run_stopped`),
#: held only so they are not garbage collected. No state of a conversation (L-4): the conversation is gone, deleting is
#: idempotent, and a deletion lost with the process is done by the next sweep.
_DELETING: set[asyncio.Task[None]] = set()

#: The switch of the conversation API (N-04): ``1`` turns it on; off by default.
CONVERSATIONS_ENV = "NODESTRO_CONVERSATIONS"
COLLECTION = "conversations"
MESSAGES = "conversation_messages"
#: The provider of local models: the only one a conversation picks without being told.
LOCAL_PROVIDER = "ollama"
#: Earlier messages that go to the model with a new one at most (the newest ones; older ones are trimmed or
#: summarized, N-05).
HISTORY_MESSAGES = 30
#: Tokens kept free for each tool definition of the answer's agent when its history is fitted (an estimate).
TOOL_TOKENS = 400
#: The id of the helper's step in a summary run (N-05).
SUMMARY_NODE = "summary"
#: How long an answer waits for its history summary; then the summary run is cancelled and the history trimmed.
#: Well below :data:`RESERVATION_TIMEOUT`, so the reserved answer does not expire meanwhile.
SUMMARY_TIMEOUT_SECONDS = 300.0
MAX_MESSAGE_CHARS = 20_000
MAX_TITLE_CHARS = 120
MAX_FILES_PER_MESSAGE = 10
MAX_FILES_PER_CONVERSATION = 50
#: Characters of a tool result kept on a message (the run keeps its own copy).
MAX_TOOL_RESULT_CHARS = 20_000
#: The id of the agent's step in an answer's run.
AGENT_NODE = "agent"
_START_NODE = "start"
_TITLE_LENGTH = 60
#: A reserved answer whose run never started (the process ended between the reservation and the launch) counts as
#: failed after this long, so the conversation takes messages again.
RESERVATION_TIMEOUT = timedelta(minutes=10)
#: The ``run_id`` of a reserved answer whose run is starting. The send that reserved it and a reader that finds it
#: expired both replace it only while it is still this value (a compare-and-set), so exactly one of them wins (N-4).
RESERVED = ""
#: How long deleting a conversation waits for its cancelled answer to stop before it deletes the answer's run; a run
#: still going then is deleted once it stops (:meth:`Conversations.run_stopped`, RT-089).
DELETE_WAIT_SECONDS = 10.0
#: The most earlier files of the conversation an answer is offered (the newest ones; RT-089).
EARLIER_FILES = 20
#: The owner of a request without an owner identity on a multi-user installation (W3-S1): a server principal without
#: a role in the tenant (a service, a machine, anything that is not a member). It sees no conversation, no answer run
#: and no file of a conversation, and it starts none (fail closed). ``None`` is only the desktop's single person, who
#: sees every conversation of the workspace. The value is no valid user id (a space and angle brackets), and a
#: conversation that names it as its owner is seen by nobody but the desktop's person either.
NO_OWNER = "<no owner>"

#: N-08 (egress protection by content provenance): ``granted_content`` once content of a granted source (a folder,
#: knowledge, persistent memory, an attached file) entered one of its answers while the switch was on; never reset.
#: ``unchecked``: nothing was recorded (also everything before the switch was on).
ProvenanceStatus = Literal["unchecked", "granted_content"]
MessageStatus = Literal["complete", "queued", "running", "paused", "succeeded", "failed", "cancelled"]
#: An answer in one of these states is final: its run ended.
FINAL: frozenset[str] = frozenset({"complete", "succeeded", "failed", "cancelled"})
#: How the model received a file (as the run's :class:`~nodestro_runtime.schemas.ProvidedFile`); ``None`` before.
FileMode = Literal["text", "document", "image", "ocr_text", "overview", "unavailable"]


def conversations_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the conversation API is switched on (``NODESTRO_CONVERSATIONS=1``)."""
    return (environ if environ is not None else os.environ).get(CONVERSATIONS_ENV, "").strip() == "1"


class ConversationNotFound(KeyError):
    """No conversation, message or file with this id for this owner (another owner's is not found either)."""


class AgentConversationFile(BaseModel):
    """A file of the conversation: an input file of origin ``conversation``."""

    id: str
    name: str
    format: str
    media_type: str
    size: int
    added_at: datetime = Field(default_factory=now)


class HistorySummary(BaseModel):
    """What replaced the earlier messages of a conversation for one answer (N-05), shown in its history.

    ``method`` ``trimmed``: the messages ``first_seq`` to ``last_seq`` were left out; ``summarized``: a helper model
    (``model``, in run ``run_id`` with its ``usage``) summarized them. ``text`` is exactly what the answer's model
    read; it keeps untrusted data in ``<untrusted_data>`` frames with their ``sources``. ``error``: why summarizing
    failed (the history was trimmed instead)."""

    method: Literal["trimmed", "summarized"]
    first_seq: int | None = None
    last_seq: int | None = None
    text: str
    model: str | None = None
    run_id: str | None = None
    usage: Usage | None = None
    sources: list[str] = Field(default_factory=list)
    error: str | None = None


class Conversation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("conv"))
    #: The tenant whose store holds it (``local`` on the desktop); a document of another tenant is never read.
    tenant_id: str
    #: The server user who started it; only they see it (``None``: everyone of the workspace, as on the desktop).
    owner: str | None = None
    title: str = ""
    #: The title was made from the first message (setting a title turns this off).
    auto_title: bool = True
    #: The agent template in a fixed version that answers.
    agent: TemplateRef
    #: The model of the next answer (``provider:model``); chosen by the person unless it is a local model.
    model: str
    #: Only local models answer (a cloud model is refused).
    offline: bool = False
    #: The provenance of the conversation's content (N-08, monotonic): with ``granted_content`` every call of its
    #: answers that reaches outside and every cloud model request needs a grant (while the switch is on).
    provenance: ProvenanceStatus = "unchecked"
    #: The conversation's budget in USD over all its answers (``None``: each answer has the default budget).
    budget_usd: float | None = Field(default=None, ge=0)
    #: How earlier messages that no longer fit the model's context window are shortened (N-05): ``trim`` leaves
    #: them out, ``summarize`` has a helper model summarize them.
    history: HistoryStrategy = "trim"
    #: The helper model of ``summarize`` (``None``: the conversation's model); chosen by the person, so it may be a
    #: cloud model, but never in an offline conversation or for an offline agent.
    summary_model: str | None = None
    files: list[AgentConversationFile] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    last_seq: int = 0


class MessageFile(BaseModel):
    """A file sent with a message, and how the answer's model received it."""

    file_id: str
    name: str
    format: str | None = None
    media_type: str | None = None
    size: int = 0
    mode: FileMode | None = None
    notes: list[str] = Field(default_factory=list)


class ToolCallView(BaseModel):
    """One tool call of an answer: its arguments, result and duration."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: str | None = None
    is_error: bool = False
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    #: What the per-call approval rules did with it (S-2), with the approval request while a person is asked.
    approval: ToolCallApproval | None = None


class ConversationMessage(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg"))
    conversation_id: str
    seq: int
    role: Literal["user", "assistant"]
    content: str = ""
    #: User messages: the files sent with it (how the model got them is filled in once the answer ran).
    attachments: list[MessageFile] = Field(default_factory=list)
    #: Answers: the tool calls of the agent.
    tool_calls: list[ToolCallView] = Field(default_factory=list)
    #: Answers: the web sources the agent's tools saw (the run's source ledger).
    sources: list[SourceEntry] = Field(default_factory=list)
    #: ``complete`` for a user message; an answer has the status of its run.
    status: MessageStatus = "complete"
    error: Problem | None = None
    agent: TemplateRef | None = None
    model: str | None = None
    #: Answers: the usage and cost of the answer's run (a history summary's run is in ``summary``).
    usage: Usage | None = None
    cost_usd: float | None = None
    #: Answers: the run that wrote it (``""`` while it is reserved and its run starts), and the approval requests it
    #: waits for.
    run_id: str | None = None
    approval_ids: list[str] = Field(default_factory=list)
    reply_to: str | None = None
    #: Answers: the earlier messages this answer's model read only as a summary, or that were left out (N-05).
    summary: HistorySummary | None = None
    created_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None


class AgentConversationSummary(BaseModel):
    id: str
    title: str
    agent: TemplateRef
    model: str
    offline: bool
    messages: int
    cost_usd: float | None
    created_at: datetime
    updated_at: datetime


class AgentConversationDetail(Conversation):
    messages: list[ConversationMessage] = Field(default_factory=list)
    #: The sum of the usage of every answer's run.
    usage: Usage = Field(default_factory=Usage)
    #: The answer being written right now.
    answering: str | None = None


class AgentChoice(BaseModel):
    """An agent template; ``version`` ``None``: its current version."""

    template_id: str = Field(min_length=1, max_length=64)
    version: int | None = Field(default=None, ge=1)


class ConversationIn(BaseModel):
    agent: AgentChoice
    #: ``None``: the agent's or the workspace's default model, only when it is a local model.
    model: str | None = Field(default=None, max_length=200)
    title: str = Field(default="", max_length=MAX_TITLE_CHARS)
    offline: bool = False
    budget_usd: float | None = Field(default=None, ge=0)
    history: HistoryStrategy = "trim"
    summary_model: str | None = Field(default=None, max_length=200)


class ConversationChange(BaseModel):
    """A change of a conversation: only the fields that are sent change (rename, switch agent or model)."""

    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    agent: AgentChoice | None = None
    model: str | None = Field(default=None, max_length=200)
    offline: bool | None = None
    budget_usd: float | None = Field(default=None, ge=0)
    history: HistoryStrategy | None = None
    #: ``""`` clears it (the conversation's model summarizes).
    summary_model: str | None = Field(default=None, max_length=200)


class MessageIn(BaseModel):
    #: May be empty when files are sent.
    content: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    file_ids: list[str] = Field(default_factory=list, max_length=MAX_FILES_PER_MESSAGE)


class SentConversationMessage(BaseModel):
    user: ConversationMessage
    assistant: ConversationMessage


def visible(conversation: Conversation, owner: str | None) -> bool:
    """Whether ``owner`` sees the conversation: everyone without an owner (the desktop), else only its owner. A
    conversation stored without an owner (made on a desktop and restored on a server) is seen by nobody on a server
    (fail closed). :data:`NO_OWNER` sees no conversation at all."""
    if owner == NO_OWNER:
        return False
    return owner is None or conversation.owner == owner


def make_title(text: str) -> str:
    """A title from the first message, without an AI request."""
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    line = " ".join(re.sub(r"[`*_#>\[\]]", "", line).split())
    if len(line) > _TITLE_LENGTH:
        line = (line[:_TITLE_LENGTH].rsplit(" ", 1)[0] or line[:_TITLE_LENGTH]).rstrip(",.;:") + "…"
    return line


def is_local(model: str) -> bool:
    return provider_of(model) == LOCAL_PROVIDER


def history_turns(history: list[ConversationMessage], *, summarized: bool = False) -> list[Turn]:
    """The earlier messages as turns (the user's messages and the answers that succeeded). ``summarized``: start from
    the newest summary an answer read (N-05) and the messages after it."""
    finished = [item for item in history if item.role == "user" or item.status == "succeeded"]
    start: list[Turn] = []
    if summarized:
        latest = next(
            (
                item.summary
                for item in reversed(history)
                if item.summary is not None and item.summary.method == "summarized" and item.summary.last_seq
            ),
            None,
        )
        if latest is not None and latest.last_seq is not None:
            start = [Turn(role="summary", text=latest.text, ref=latest.last_seq, first=latest.first_seq)]
            finished = [item for item in finished if item.seq > latest.last_seq]
    return start + [Turn(role=item.role, text=item.content, ref=item.seq) for item in finished]


def render_input(turns: tuple[Turn, ...] | list[Turn]) -> str:
    """The run's input of an answer: the earlier turns (a summary turn first, if any) and the new message (the last
    turn)."""
    *earlier, new = turns
    if not earlier:
        return new.text
    parts = ["The conversation so far (oldest first):"]
    for turn in earlier:
        if turn.role == "summary":
            parts.append(turn.text)  # it starts with its own label: "[Summary of turns 1 to 12 (summarized)]"
        else:
            parts.append(f"{'User' if turn.role == 'user' else 'Assistant'}: {turn.text}")
    parts.append(f"The user's new message:\n{new.text}")
    return "\n\n".join(parts)


def earlier_files(conversation: Conversation, history: list[ConversationMessage], sent: list[str]) -> list[str]:
    """The files an answer is offered besides the ones sent with its message (RT-089): the files sent with earlier
    messages of this conversation that are still in it (a removed file is deleted), newest first, at most
    :data:`EARLIER_FILES`. Only files of the conversation itself: the caller has checked that ``owner`` sees it, and the
    answer's run is seen by nobody else (H-1). A file uploaded but never sent is not offered."""
    present = {item.id for item in conversation.files}
    found: list[str] = []
    for message in sorted(history, key=lambda item: item.seq, reverse=True):
        if message.role != "user":
            continue
        for item in message.attachments:
            if item.file_id in present and item.file_id not in sent and item.file_id not in found:
                found.append(item.file_id)
    return found[:EARLIER_FILES]


def answer_input(history: list[ConversationMessage], content: str) -> str:
    """The run's input of an answer without a known context window: the earlier messages (the newest
    :data:`HISTORY_MESSAGES`, older ones trimmed with a note) and the new one."""
    turns = [*history_turns(history), Turn(role="user", text=content)]
    return render_input(compact(turns, budget=sys.maxsize, max_turns=HISTORY_MESSAGES + 1).turns)


def history_budget(context_window: int | None, max_output: int | None, config: AgentConfig) -> int | None:
    """The tokens an answer's history may take (N-05): the model's context window minus the agent's output limit
    (capped by the model's maximum output ``max_output``), its system prompt and its tools (an estimate each);
    ``None`` for an unknown window."""
    reserve = min(config.max_tokens, max_output) if max_output else config.max_tokens
    reserved = estimate_tokens(config.system_prompt) + TOOL_TOKENS * (len(config.tools) + len(config.server_tools))
    return context_budget(context_window, output_reserve=reserve, reserved=reserved)


def summary_workflow(
    conversation: Conversation, model: str, max_tokens: int, budget: float | None, *, offline: bool = False
) -> Workflow:
    """The run that summarizes earlier messages (N-05): the start step and a helper agent without tools; ``offline``
    (N-09): the helper agent is offline, so the engine allows it local models only at every call."""
    helper = AgentConfig(
        name="Summary",
        model=model,
        system_prompt=SUMMARY_INSTRUCTIONS,
        max_tokens=max(1024, max_tokens * 2),  # room for a reasoning model; the text is cut to ``max_tokens``
        max_tool_rounds=0,
        effort=None,
        offline=offline,
    )
    return Workflow(
        id=conversation.id,
        name="Conversation summary",
        nodes=[
            Node(id=_START_NODE, type="start"),
            Node(id=SUMMARY_NODE, type="agent", label=helper.name, agent=helper),
        ],
        edges=[Edge(id="e1", source=_START_NODE, target=SUMMARY_NODE)],
        budget_usd=budget,
        notifications=NotificationRules(events=[]),
    )


def answer_agent(config: AgentConfig, *, model: str, ref: TemplateRef, offline: bool = False) -> AgentConfig:
    """The template's agent as it answers in a conversation: the conversation's model, no cloud fallback and no
    quality check with a cloud judge (a cloud model is never chosen automatically; the judge model defaults to a
    cloud model), and the message as its task. A quality check with a local judge stays.

    Persistent memory becomes run memory (W3-S1 review): a memory space is shared by every member of the workspace or
    tenant (``/api/memory``), so an answer that wrote into it would hand the conversation to them. Run memory belongs
    to the answer's run, which only the conversation's owner sees, and goes with it.

    ``offline`` (N-09): the conversation is offline, so the answer's agent is too (the agent's own flag stays on), and
    an offline answer proposes no knowledge (``offline.shared_memory``: other agents would read it)."""
    quality = config.quality
    if quality.enabled and not is_local(quality.judge_model):
        quality = quality.model_copy(update={"enabled": False})
    memory = config.memory
    if memory.mode == "persistent":
        memory = memory.model_copy(update={"mode": "run", "space": None})
    return config.model_copy(
        update={
            "model": model,
            "fallbacks": [item for item in config.fallbacks if is_local(item)],
            "quality": quality,
            "memory": memory,
            "task_template": "{{input}}",
            "origin": ref,
            "offline": config.offline or offline,
            "knowledge_proposals": (
                config.knowledge_proposals.model_copy(update={"enabled": False})
                if config.offline or offline
                else config.knowledge_proposals
            ),
        },
        deep=True,
    )


def check_answer_models(workflow: Workflow, *, model: str, offline: bool) -> None:
    """Every model the answer's run will use (:func:`~nodestro_runtime.preflight.agent_models`: the agent's and its
    judge's), checked before the run starts: only the conversation's own ``model`` may be a cloud model (the person
    chose it), and an offline conversation uses local models only."""
    for used in sorted(agent_models(workflow)):
        if is_local(used):
            continue
        if offline:
            raise NodestroError(
                "conversation.offline_cloud_model",
                "This conversation is offline: only local models can answer",
                model=used,
            )
        if used != model:
            raise NodestroError(
                "conversation.cloud_model_not_chosen",
                "The answer would use a cloud model that nobody chose for this conversation",
                model=used,
            )


def answer_workflow(conversation: Conversation, config: AgentConfig, budget: float | None) -> Workflow:
    """The workflow one answer runs: the start step and the agent (it notifies nobody; the person waits)."""
    return Workflow(
        id=conversation.id,
        # A fixed name: the run list and the usage report show it to every member, the title only its owner sees.
        name="Conversation",
        nodes=[
            Node(id=_START_NODE, type="start"),
            Node(id=AGENT_NODE, type="agent", label=config.name, agent=config),
        ],
        edges=[Edge(id="e1", source=_START_NODE, target=AGENT_NODE)],
        budget_usd=budget,
        notifications=NotificationRules(events=[]),
    )


def _duration_ms(started: datetime, finished: datetime | None) -> int | None:
    if finished is None:
        return None
    return max(0, round((finished - started).total_seconds() * 1000))


def _cost(usage: Usage | None) -> float | None:
    return None if usage is None else usage.estimated_cost_usd


def _key(conversation_id: str, seq: int) -> str:
    return f"{conversation_id}:{seq:06d}"


class Conversations:
    """The conversations of one workspace (tenant)."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.store = workspace.store
        self.templates = AgentTemplates(workspace.store)

    # ---- reading ------------------------------------------------------------------------------------------------

    def _stored(self, conversation_id: str, owner: str | None) -> Conversation:
        found = self.store.get(COLLECTION, conversation_id, Conversation)
        if found is None or found.tenant_id != self.workspace.tenant_id or not visible(found, owner):
            raise ConversationNotFound(conversation_id)
        return found

    def get(self, conversation_id: str, owner: str | None) -> Conversation:
        """The conversation; :class:`ConversationNotFound` for an unknown id and for another owner's."""
        return self._stored(conversation_id, owner)

    def is_offline(self, conversation_id: str) -> bool:
        """Whether the conversation is offline now (N-09), read by the engine at every call of its answers' runs. An
        unknown or another tenant's conversation counts as offline (fail closed): its runs are cancelled anyway."""
        found = self.store.get(COLLECTION, conversation_id, Conversation)
        if found is None or found.tenant_id != self.workspace.tenant_id:
            return True
        return found.offline

    def is_granted(self, conversation_id: str) -> bool:
        """Whether the conversation holds content of a granted source (N-08), read by the engine at every check of its
        answers' runs. An unknown or another tenant's conversation counts as holding it (fail closed)."""
        found = self.store.get(COLLECTION, conversation_id, Conversation)
        if found is None or found.tenant_id != self.workspace.tenant_id:
            return True
        return found.provenance == "granted_content"

    def mark_granted(self, conversation_id: str) -> None:
        """Record that content of a granted source entered an answer of the conversation (N-08; by the engine).
        Monotonic: once ``granted_content``, it stays so. One store transaction under the conversation's lock, so a
        concurrent change of the conversation is not lost; a deleted conversation is not written again."""
        with self.store.transaction():
            self.store.lock(COLLECTION, conversation_id)
            current = self.store.get(COLLECTION, conversation_id, Conversation)
            if current is None or current.provenance == "granted_content":
                return
            self.store.put(COLLECTION, conversation_id, current.model_copy(update={"provenance": "granted_content"}))

    def run_visible(self, run_id: str, owner: str | None) -> bool:
        """Whether ``owner`` may see run ``run_id`` through the run routes (H-1). An answer's run (trigger
        ``conversation``, or a run below one) belongs to its conversation: only who sees the conversation sees the
        run, and the run of a deleted conversation is seen by nobody. Every other run (and an unknown id) is left to
        the run routes.

        A run below another one whose top cannot be reached (a parent is missing, or the parents form a loop) is seen
        by nobody: whether it belongs to a conversation is unknown, so it fails closed."""
        run = self.store.get("runs", run_id, RunSummary)
        seen: set[str] = set()
        while run is not None and run.parent_run_id:
            if run.id in seen:
                return False
            seen.add(run.id)
            run = self.store.get("runs", run.parent_run_id, RunSummary)
            if run is None:
                return False
        return run is None or self._top_visible(run, owner)

    def summary_visible(self, run: RunSummary | Run, owner: str | None) -> bool:
        """:meth:`run_visible` for a run at hand (a run list's summary): a top-level run that is not an answer needs
        no read, an answer's run one (its conversation), a run below another one the walk to its top (N-2)."""
        if run.parent_run_id:
            return self.run_visible(run.id, owner)
        return self._top_visible(run, owner)

    def _top_visible(self, run: RunSummary | Run, owner: str | None) -> bool:
        """Whether ``owner`` sees a top-level run: every run but an answer, and an answer only with its conversation."""
        if run.trigger != "conversation":
            return True
        found = self.store.get(COLLECTION, run.trigger_ref or "", Conversation)
        return found is not None and found.tenant_id == self.workspace.tenant_id and visible(found, owner)

    def summaries(self, owner: str | None) -> list[AgentConversationSummary]:
        """The conversations ``owner`` sees, most recently changed first."""
        items = [
            item
            for item in self.store.list(COLLECTION, Conversation)
            if item.tenant_id == self.workspace.tenant_id and visible(item, owner)
        ]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        result = []
        for item in items:
            messages = self.messages(item.id, owner)
            result.append(
                AgentConversationSummary(
                    id=item.id,
                    title=item.title,
                    agent=item.agent,
                    model=item.model,
                    offline=item.offline,
                    messages=len(messages),
                    cost_usd=self._usage(item.id, messages).estimated_cost_usd,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
            )
        return result

    def messages(self, conversation_id: str, owner: str | None) -> list[ConversationMessage]:
        """The messages in order; an answer whose run moved on is brought up to date (also after a restart)."""
        self._stored(conversation_id, owner)
        found = self.store.list_prefix(MESSAGES, f"{conversation_id}:", ConversationMessage)
        return [self._sync(item) for item in sorted(found, key=lambda item: item.seq)]

    def message(self, conversation_id: str, owner: str | None, message_id: str) -> ConversationMessage:
        found = next((item for item in self.messages(conversation_id, owner) if item.id == message_id), None)
        if found is None:
            raise ConversationNotFound(message_id)
        return found

    def detail(self, conversation_id: str, owner: str | None) -> AgentConversationDetail:
        conversation = self._stored(conversation_id, owner)
        messages = self.messages(conversation_id, owner)
        answering = next((item.id for item in messages if item.status not in FINAL), None)
        return AgentConversationDetail(
            **conversation.model_dump(),
            messages=messages,
            usage=self._usage(conversation_id, messages),
            answering=answering,
        )

    def usage(self, conversation_id: str, owner: str | None) -> Usage:
        """The sum of the usage of every answer's run (failed and cancelled ones included)."""
        return self._usage(conversation_id, self.messages(conversation_id, owner))

    def _usage(self, conversation_id: str, messages: list[ConversationMessage]) -> Usage:
        """The answers' usage and that of every history summary run of the conversation (N-05), read from the
        runs, so a summary whose answer never started still counts (its reservation was released)."""
        total = Usage()
        for item in messages:
            if item.usage is not None:
                total = add_usage(total, item.usage)
        for run in self._summary_runs(conversation_id):
            total = add_usage(total, run.usage)
        return total

    def _summary_runs(self, conversation_id: str) -> list[Run]:
        """The history summary runs of a conversation (N-05): its runs whose workflow has the summary step."""
        return [
            run
            for run in self.store.find("runs", "trigger_ref", [conversation_id], Run)
            if run.trigger == "conversation" and any(node.id == SUMMARY_NODE for node in run.workflow.nodes)
        ]

    # ---- writing ------------------------------------------------------------------------------------------------

    def _agent(self, choice: AgentChoice) -> tuple[TemplateRef, AgentConfig]:
        template = self.templates.get(choice.template_id)
        if template is None:
            raise NodestroError(
                "conversation.agent_not_found", "The agent does not exist", template_id=choice.template_id
            )
        version = choice.version or template.version
        try:
            found = self.templates.version(choice.template_id, version)
        except KeyError:
            raise NodestroError(
                "conversation.agent_not_found", "The agent does not exist", template_id=choice.template_id
            ) from None
        return TemplateRef(template_id=choice.template_id, version=version), found.agent

    def _default_model(self, config: AgentConfig) -> str:
        """The model a conversation takes without being told: a local one only (never a cloud model)."""
        for candidate in (config.model, self.workspace.settings().default_model):
            if candidate and is_local(candidate):
                return check_model(candidate)
        raise NodestroError(
            "conversation.model_required",
            "Choose a model: neither the agent nor the default model is a local model, and a cloud model is never "
            "chosen automatically",
        )

    def _template_offline(self, conversation: Conversation) -> bool:
        """Whether the conversation's agent template version is offline (N-09); a template that no longer exists
        counts as not offline here (an answer refuses it anyway)."""
        try:
            choice = AgentChoice(template_id=conversation.agent.template_id, version=conversation.agent.version)
            return self._agent(choice)[1].offline
        except NodestroError:
            return False

    @staticmethod
    def _checked_model(model: str, *, offline: bool) -> str:
        qualified = check_model(model)
        if offline and not is_local(qualified):
            raise NodestroError(
                "conversation.offline_cloud_model",
                "This conversation is offline: only local models can answer",
                model=qualified,
            )
        return qualified

    def create(self, request: ConversationIn, *, owner: str | None) -> AgentConversationDetail:
        """Start a conversation of ``owner``; :data:`NO_OWNER` is refused (``access.person_required``): a conversation
        always belongs to a person (W3-S1)."""
        if owner == NO_OWNER:
            raise NodestroError("access.person_required", "Only a member of the tenant can start a conversation")
        ref, config = self._agent(request.agent)
        model = request.model if request.model else self._default_model(config)
        conversation = Conversation(
            tenant_id=self.workspace.tenant_id,
            owner=owner,
            title=request.title.strip(),
            auto_title=not request.title.strip(),
            agent=ref,
            # An offline agent (N-09) answers with local models only, also in a conversation that is not offline.
            model=self._checked_model(model, offline=request.offline or config.offline),
            offline=request.offline,
            budget_usd=request.budget_usd,
            history=request.history,
            summary_model=self._checked_model(request.summary_model, offline=request.offline or config.offline)
            if request.summary_model
            else None,
        )
        self.store.put(COLLECTION, conversation.id, conversation)
        return self.detail(conversation.id, owner)

    def update(self, conversation_id: str, owner: str | None, change: ConversationChange) -> AgentConversationDetail:
        """Rename it, switch its agent or model, its offline flag or budget; only the fields sent change. The change is
        made on the stored conversation under its lock, so it never writes back an older ``last_seq`` or file list
        (M-1)."""
        sent = change.model_fields_set
        chosen = self._agent(change.agent) if "agent" in sent and change.agent is not None else None
        agent = chosen[0] if chosen is not None else None
        with self.store.transaction():
            self.store.lock(COLLECTION, conversation_id)
            conversation = self._stored(conversation_id, owner)
            updates: dict[str, Any] = {"updated_at": now()}
            if "title" in sent and change.title is not None:
                updates.update(title=change.title.strip(), auto_title=False)
            if agent is not None:
                updates["agent"] = agent
            offline = change.offline if "offline" in sent and change.offline is not None else conversation.offline
            updates["offline"] = offline
            model = change.model if "model" in sent and change.model else conversation.model
            template_offline = chosen[1].offline if chosen is not None else self._template_offline(conversation)
            updates["model"] = self._checked_model(model, offline=offline or template_offline)
            if "budget_usd" in sent:
                updates["budget_usd"] = change.budget_usd
            if "history" in sent and change.history is not None:
                updates["history"] = change.history
            helper = change.summary_model if "summary_model" in sent else conversation.summary_model
            updates["summary_model"] = (
                self._checked_model(helper, offline=offline or template_offline) if helper else None
            )
            self.store.put(COLLECTION, conversation_id, conversation.model_copy(update=updates))
        return self.detail(conversation_id, owner)

    async def delete(self, conversation_id: str, owner: str | None) -> None:
        """Delete the conversation with its messages, files and the answers' runs (H-1: the runs hold the messages as
        input, the answers as output and the files). A running answer is cancelled first and deleted once it stopped
        (at most :data:`DELETE_WAIT_SECONDS`; a run that is still going then is hidden from everyone by
        :meth:`run_visible` and deleted once it stops, :meth:`run_stopped`, RT-089). The usage ledger keeps the cost.
        The caller revokes the grants for "this conversation" (``revoke_conversation`` of the tool approval rules,
        L-2): only a person's request writes grants."""
        with self.store.transaction():
            self.store.lock(COLLECTION, conversation_id)
            conversation = self._stored(conversation_id, owner)
            messages = self.store.list_prefix(MESSAGES, f"{conversation_id}:", ConversationMessage)
            self.store.delete_prefix(MESSAGES, f"{conversation_id}:")
            self.store.delete(COLLECTION, conversation_id)
        run_ids = {item.run_id for item in messages if item.run_id}
        run_ids |= {
            run.id
            for run in self.store.find("runs", "trigger_ref", [conversation_id], Run)
            if run.trigger == "conversation"
        }
        for run_id in run_ids:
            self.workspace.cancel(run_id)
        for run_id in sorted(run_ids):
            if await self._stopped(run_id):
                self._delete_run(run_id)
        for file in conversation.files:
            if file.id in self.workspace.inputs:
                self.workspace.inputs.delete(file.id, force=True)

    # ---- the runs of deleted conversations (RT-089) ------------------------------------------------------------------

    def _orphan(self, run: Run) -> bool:
        """Whether ``run`` is a stopped answer's run whose conversation is gone (deleted while it ran)."""
        if run.trigger != "conversation" or run.parent_run_id or run.status in ("queued", "running", "paused"):
            return False
        # Only a conversation that is gone: a stored one of another tenant hides the run (run_visible) but is not
        # taken as deleted.
        return self.store.get_raw(COLLECTION, run.trigger_ref or "") is None

    def run_stopped(self, run: Run) -> None:
        """A listener of the run manager (every stop of a run): an answer's run whose conversation was deleted while it
        ran is deleted once it stopped, with everything stored for it (as the delete does). Called in the process that
        executed it, and by the reaper's interrupt on a server; the desktop's start sweeps the rest
        (:meth:`sweep_orphan_runs`)."""
        if not self._orphan(run):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or not self.workspace.runs.is_live(run.id):
            self._delete_run(run.id)
            return
        # The engine still finishes the run's execution (its last event): delete it right after.
        task = loop.create_task(self._delete_when_stopped(run.id))
        _DELETING.add(task)
        task.add_done_callback(_DELETING.discard)

    async def _delete_when_stopped(self, run_id: str) -> None:
        try:
            if await self._stopped(run_id):
                self._delete_run(run_id)
        except Exception:  # housekeeping: the next sweep tries again; the run stays hidden meanwhile
            logger.exception("Deleting run %s of a deleted conversation failed", run_id)

    def sweep_orphan_runs(self) -> list[str]:
        """Delete every stopped answer's run whose conversation is gone (a run that was still going when its
        conversation was deleted, and the process ended before it stopped). Returns the deleted run ids."""
        deleted: list[str] = []
        for run in self.store.find("runs", "trigger", ["conversation"], Run):
            if self._orphan(run) and not self.workspace.runs.is_live(run.id):
                self._delete_run(run.id)
                deleted.append(run.id)
        return deleted

    async def _stopped(self, run_id: str) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DELETE_WAIT_SECONDS
        while True:
            run = self.store.get("runs", run_id, Run)
            if not self.workspace.runs.is_live(run_id) and (run is None or run.status not in ("queued", "running")):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    def _delete_run(self, run_id: str) -> None:
        """Delete an answer's run with everything stored for it (as the run API's delete does)."""
        run = self.store.get("runs", run_id, Run)
        if run is None:
            return
        tree = [run]
        queue = [run]
        while queue:
            parent = queue.pop()
            for state in parent.nodes.values():
                for child_id in [state.child_run_id, *state.child_run_ids]:
                    child = self.store.get("runs", child_id, Run) if child_id else None
                    if child is not None and child.parent_run_id == parent.id and child not in tree:
                        tree.append(child)
                        queue.append(child)
        for item in tree:
            self.store.delete("runs", item.id)
            self.workspace.files.delete_run(item.id)
            self.workspace.runs.discard_tool_waits(item.id, because="run.deleted")
            # Its events too, here and in the server's event table (W3-P4a L-2); a last event of an execution
            # that ends after this is not stored.
            self.workspace.runs.delete_events(item.id)
        self.workspace.runs.chat.delete_run(run.id)
        self.workspace.inputs.release([file_id for item in tree for file_id in item.file_ids])

    # ---- files --------------------------------------------------------------------------------------------------

    def add_file(
        self,
        conversation_id: str,
        owner: str | None,
        data: bytes,
        *,
        name: str,
        media_type: str | None,
        uploaded_by: str,
    ) -> AgentConversationFile:
        """Check, read and store an upload as a file of the conversation; it waits for a message. Raises
        :class:`~nodestro_runtime.file_formats.FileFormatError` or ``conversation.too_many_files``."""
        self._room(self._stored(conversation_id, owner))
        item: InputFile = self.workspace.inputs.add(
            data, name=name, media_type=media_type, origin="conversation", uploaded_by=uploaded_by
        )
        attached = AgentConversationFile(
            id=item.id, name=item.name, format=item.format, media_type=item.media_type, size=item.size
        )
        try:
            with self.store.transaction():
                self.store.lock(COLLECTION, conversation_id)
                conversation = self._room(self._stored(conversation_id, owner))
                changed = {"files": [*conversation.files, attached], "updated_at": now()}
                self.store.put(COLLECTION, conversation_id, conversation.model_copy(update=changed))
        except BaseException:
            self.workspace.inputs.delete(item.id, force=True)
            raise
        return attached

    @staticmethod
    def _room(conversation: Conversation) -> Conversation:
        if len(conversation.files) >= MAX_FILES_PER_CONVERSATION:
            raise NodestroError(
                "conversation.too_many_files",
                f"A conversation holds at most {MAX_FILES_PER_CONVERSATION} files; remove some first",
                limit=MAX_FILES_PER_CONVERSATION,
            )
        return conversation

    def file(self, conversation_id: str, owner: str | None, file_id: str) -> AgentConversationFile:
        found = next((item for item in self._stored(conversation_id, owner).files if item.id == file_id), None)
        if found is None:
            raise ConversationNotFound(file_id)
        return found

    def remove_file(self, conversation_id: str, owner: str | None, file_id: str) -> AgentConversationDetail:
        """Remove a file from the conversation and delete it; sent messages keep its name."""
        with self.store.transaction():
            self.store.lock(COLLECTION, conversation_id)
            conversation = self._stored(conversation_id, owner)
            if not any(item.id == file_id for item in conversation.files):
                raise ConversationNotFound(file_id)
            changed = {"files": [item for item in conversation.files if item.id != file_id], "updated_at": now()}
            self.store.put(COLLECTION, conversation_id, conversation.model_copy(update=changed))
        if file_id in self.workspace.inputs:
            self.workspace.inputs.delete(file_id, force=True)
        return self.detail(conversation_id, owner)

    # ---- answers ------------------------------------------------------------------------------------------------

    async def send(
        self, conversation_id: str, owner: str | None, request: MessageIn, *, actor: str = "local"
    ) -> SentConversationMessage:
        """Store the message and start its answer as a run; follow it with the run's events.

        The answer is reserved first, in one store transaction under the conversation's lock (M-2): the checks
        (another answer running, the budget, the files) and the two new messages are one step for every process, and
        a message key is only written when it is free. The run starts after that; when it cannot start (a refused
        model or workflow), the reservation is removed again and nothing stays stored."""
        content = request.content.strip()
        if not content and not request.file_ids:
            raise NodestroError("conversation.empty_message", "The message is empty")
        file_ids = list(dict.fromkeys(request.file_ids))
        conversation, user, answer, budget, earlier_ids = self._reserve(conversation_id, owner, content, file_ids)
        try:
            ref, template_agent = self._agent(
                AgentChoice(template_id=conversation.agent.template_id, version=conversation.agent.version)
            )
            # N-09: an offline conversation or an offline agent answers with local models only; the answer's agent is
            # offline then, and the engine checks every call of the run again.
            history = self._history(conversation_id, user.seq)
            scope: OfflineScope | None = (
                "conversation" if conversation.offline else "agent" if template_agent.offline else None
            )
            offline = scope is not None
            earlier = None if offline else self.offline_history(history)
            if earlier is not None:
                # The history repeats earlier offline answers: they stay on local models after the switch is off.
                if not is_local(check_model(conversation.model)):
                    raise cloud_model_error(check_model(conversation.model), earlier)
                offline = True
                scope = earlier
            model = self._checked_model(conversation.model, offline=offline)
            agent = answer_agent(template_agent, model=model, ref=ref, offline=offline)
            check_answer_models(answer_workflow(conversation, agent, budget), model=model, offline=offline)
            if conversation.history == "summarize":
                # The answer's own pre-check before a summary is paid for (N-05 review L1); launch repeats it.
                await asyncio.to_thread(blocking_problems, self.workspace, answer_workflow(conversation, agent, budget))
            # N-05: the history fitted into the model's context window, perhaps summarized in a run of its own. The
            # helper obeys the same offline rules (the conversation, its agent and its offline history).
            text, summary = await self.history_input(
                conversation,
                history,
                Turn(role="user", text=content, ref=user.seq),
                agent=agent,
                budget=budget,
                actor=actor,
                offline=scope,
            )
            if summary is not None and summary.run_id and self._gone(conversation_id):
                raise ConversationNotFound(conversation_id)  # deleted while its history was summarized (L2)
            if summary is not None and summary.usage is not None and budget is not None:
                budget = max(0.0, budget - (summary.usage.estimated_cost_usd or 0.0))
            workflow = answer_workflow(conversation, agent, budget)
            run = await launch(
                self.workspace,
                workflow,
                RunIn(input=text, file_ids=file_ids),
                trigger="conversation",
                trigger_ref=conversation.id,
                started_by=actor,
                reference_file_ids=earlier_ids,
            )
        except BaseException:
            self._release(conversation_id, user, answer)
            raise
        with self.store.transaction():
            self.store.lock(COLLECTION, conversation_id)
            current = self.store.get(COLLECTION, conversation_id, Conversation)
            if current is None:  # deleted while the run started: the run goes too
                self.workspace.cancel(run.id)
                raise ConversationNotFound(conversation_id)
            answer = answer.model_copy(update={"run_id": run.id, "agent": ref, "model": model, "summary": summary})
            if not self.store.replace_if(
                MESSAGES, _key(conversation_id, answer.seq), answer, field="run_id", expected=RESERVED
            ):
                # The reservation expired while the run started (a reader failed it, and another answer may run
                # now): this run must not become a second answer (N-4).
                self.workspace.cancel(run.id)
                raise NodestroError("conversation.answer_running", "Wait until the current answer is finished")
            changed: dict[str, Any] = {"updated_at": now()}
            if current.auto_title and not current.title:
                changed["title"] = make_title(content) or (user.attachments[0].name if user.attachments else "")
            self.store.put(COLLECTION, conversation_id, current.model_copy(update=changed))
        return SentConversationMessage(user=user, assistant=self._sync(answer))

    async def history_input(
        self,
        conversation: Conversation,
        history: list[ConversationMessage],
        new: Turn,
        *,
        agent: AgentConfig,
        budget: float | None,
        actor: str,
        offline: OfflineScope | None,
    ) -> tuple[str, HistorySummary | None]:
        """The answer's input (N-05): ``history`` and the ``new`` message fitted into the context window of the
        answer's model (:func:`history_budget`; an unknown window keeps the newest :data:`HISTORY_MESSAGES`), and
        what replaced the older messages (``None`` when everything fits).

        With ``history`` ``summarize`` a helper model summarizes them in a run of its own
        (:meth:`_summarize`): the conversation's ``summary_model``, else its model. Offline content (N-09: ``offline``,
        the scope the answer runs under, else the conversation's flag or an offline answer in ``history``,
        :meth:`offline_history`) allows only a local helper on this computer: a cloud helper is refused before
        anything is sent (``offline.cloud_model``), and the history is trimmed instead, as for any failed summary."""
        if offline is None:
            offline = "conversation" if conversation.offline else self.offline_history(history)
        limits = self.workspace.builder.limits(agent.model)
        tokens = history_budget(limits.context_window, output_limits.model_ceiling(limits)[0], agent)
        summarizing = conversation.history == "summarize"
        turns = [*history_turns(history, summarized=summarizing), new]
        found: dict[str, Any] = {}

        async def summarize(transcript: str, max_tokens: int) -> str:
            helper = conversation.summary_model or agent.model
            found["model"] = helper
            return await self._summarize(
                conversation,
                transcript,
                max_tokens,
                model=helper,
                budget=budget,
                actor=actor,
                offline=offline,
                into=found,
            )

        result: Compacted = await acompact(
            turns,
            budget=tokens if tokens is not None else sys.maxsize,
            strategy=conversation.history,
            summarize=summarize if summarizing else None,
            max_turns=HISTORY_MESSAGES + 1,
        )
        if result.summary is None or result.method == "none":
            return render_input(result.turns), None
        summary = HistorySummary(
            method="summarized" if result.method == "summarized" else "trimmed",
            first_seq=result.summary.first,
            last_seq=result.summary.ref,
            text=result.summary.text,
            model=found.get("model") if result.method == "summarized" else None,
            run_id=found.get("run_id"),
            usage=found.get("usage"),
            sources=list(result.sources),
            error=found.get("error") or ("run.failed" if result.error else None),
        )
        return render_input(result.turns), summary

    async def _summarize(
        self,
        conversation: Conversation,
        transcript: str,
        max_tokens: int,
        *,
        model: str,
        budget: float | None,
        actor: str,
        offline: OfflineScope | None,
        into: dict[str, Any],
    ) -> str:
        """One summary of earlier messages by ``model``, as a run of the conversation (trigger ``conversation``): its
        cost counts for the conversation, and the engine applies the conversation's grants and model rules. With
        offline content (``offline``, N-09) the helper must be a local model on this computer
        (:meth:`~nodestro_runtime.workspace.Workspace.connect_model` refuses anything else with
        ``offline.cloud_model`` before the run starts), and its agent is offline, so the engine checks every call
        again. Records the run, its usage and a refusal's code in ``into``; raises when there is no summary."""
        try:
            helper = check_model(model)
            await asyncio.to_thread(
                self.workspace.connect_model,
                helper,
                actor=actor,
                offline=offline,
                shared_content=self.is_granted(conversation.id),
            )
            workflow = summary_workflow(conversation, helper, max_tokens, budget, offline=offline is not None)
            check_answer_models(workflow, model=helper, offline=offline is not None)
            run = await launch(
                self.workspace,
                workflow,
                RunIn(input=transcript),
                trigger="conversation",
                trigger_ref=conversation.id,
                started_by=actor,
            )
        except NodestroError as exc:
            into["error"] = exc.code
            raise
        into["run_id"] = run.id
        if self._gone(conversation.id):
            # Deleted between the reservation and this launch, so the delete did not see this run (N-05 review L2):
            # it goes the same way.
            self.workspace.cancel(run.id)
            if await self._stopped(run.id):
                self._delete_run(run.id)
            raise ConversationNotFound(conversation.id)
        try:
            done = await self.workspace.wait(run.id, timeout=SUMMARY_TIMEOUT_SECONDS)
        except TimeoutError:
            self.workspace.cancel(run.id)
            into["error"] = "run.cancelled"
            raise
        into["usage"] = done.usage
        state = done.nodes.get(SUMMARY_NODE)
        text = (as_text(state.output) if state.output is not None else state.text) if state is not None else ""
        if done.status != "succeeded" or not text.strip():
            into["error"] = (state.error_code if state is not None else None) or done.error_code or "run.failed"
            raise RuntimeError(into["error"])
        return text

    def _gone(self, conversation_id: str) -> bool:
        return self.store.get(COLLECTION, conversation_id, Conversation) is None

    def _reserve(
        self, conversation_id: str, owner: str | None, content: str, file_ids: list[str]
    ) -> tuple[Conversation, ConversationMessage, ConversationMessage, float | None, list[str]]:
        """Check and store the user's message and a queued answer without a run, in one transaction. Also returns the
        earlier files the answer is offered (:func:`earlier_files`)."""
        with self.store.transaction():
            self.store.lock(COLLECTION, conversation_id)
            conversation = self._stored(conversation_id, owner)
            history = self.messages(conversation_id, owner)
            if any(item.status not in FINAL for item in history):
                raise NodestroError("conversation.answer_running", "Wait until the current answer is finished")
            files = {item.id: item for item in conversation.files}
            missing = [file_id for file_id in file_ids if file_id not in files]
            if missing:
                raise ConversationNotFound(missing[0])
            budget: float | None = None
            if conversation.budget_usd is not None:
                total = self._usage(conversation_id, history)
                spent = total.known_cost_usd if total.known_cost_usd is not None else total.estimated_cost_usd or 0.0
                budget = conversation.budget_usd - spent
                if budget <= 0:
                    raise NodestroError(
                        "conversation.budget_exhausted",
                        "The conversation's budget is used up",
                        budget_usd=conversation.budget_usd,
                    )
            user = ConversationMessage(
                conversation_id=conversation_id,
                seq=conversation.last_seq + 1,
                role="user",
                content=content,
                attachments=[
                    MessageFile(
                        file_id=file_id,
                        name=files[file_id].name,
                        format=files[file_id].format,
                        media_type=files[file_id].media_type,
                        size=files[file_id].size,
                    )
                    for file_id in file_ids
                ],
            )
            answer = ConversationMessage(
                conversation_id=conversation_id,
                seq=conversation.last_seq + 2,
                role="assistant",
                status="queued",
                agent=conversation.agent,
                model=conversation.model,
                run_id=RESERVED,
                reply_to=user.id,
            )
            for message in (user, answer):
                key = _key(conversation_id, message.seq)
                if not self.store.put_raw_if_absent(MESSAGES, key, message.model_dump(mode="json")):
                    raise NodestroError("conversation.answer_running", "Wait until the current answer is finished")
            reserved = conversation.model_copy(update={"last_seq": answer.seq, "updated_at": now()})
            self.store.put(COLLECTION, conversation_id, reserved)
        return reserved, user, answer, budget, earlier_files(conversation, history, file_ids)

    def _release(self, conversation_id: str, user: ConversationMessage, answer: ConversationMessage) -> None:
        """Remove a reservation whose run did not start (the sequence numbers stay used)."""
        with self.store.transaction():
            self.store.lock(COLLECTION, conversation_id)
            for message in (user, answer):
                key = _key(conversation_id, message.seq)
                stored = self.store.get(MESSAGES, key, ConversationMessage)
                if stored is not None and stored.id == message.id:
                    self.store.delete(MESSAGES, key)

    def offline_history(self, history: list[ConversationMessage]) -> OfflineScope | None:
        """Whether an earlier answer of ``history`` is offline content (N-09, ``RunManager.content_offline_scope``),
        and why: the next answer gets it as its history (:func:`answer_input`), so it stays on local models also after
        the conversation's offline switch is off (sticky, as the assistant's run attachments). Fails closed: an
        answer whose run cannot be read counts as offline."""
        runs = self.workspace.runs
        for item in history:
            if item.role == "user" or not item.run_id or item.run_id == RESERVED:
                continue
            run = runs.get(item.run_id)
            if run is None:
                return "conversation"
            scope = runs.content_offline_scope(run)
            if scope is not None:
                return scope
        return None

    def _history(self, conversation_id: str, before: int) -> list[ConversationMessage]:
        found = self.store.list_prefix(MESSAGES, f"{conversation_id}:", ConversationMessage)
        return [item for item in sorted(found, key=lambda item: item.seq) if item.seq < before]

    def cancel(self, conversation_id: str, owner: str | None, message_id: str) -> ConversationMessage:
        """Cancel a running answer: its run is cancelled, which closes the model connection."""
        found = self.message(conversation_id, owner, message_id)
        if found.status not in FINAL and found.run_id:
            self.workspace.cancel(found.run_id)
        return self.message(conversation_id, owner, message_id)

    def _sync(self, message: ConversationMessage) -> ConversationMessage:
        """An answer as its run is now; stored once the run's status changed (a final one, a pause)."""
        if message.role != "assistant" or message.status in FINAL:
            return message
        if message.run_id == RESERVED:
            created = message.created_at if message.created_at.tzinfo else message.created_at.replace(tzinfo=UTC)
            if now() - created < RESERVATION_TIMEOUT:
                return message  # reserved; its run is starting
            expired = message.model_copy(
                update={
                    "status": "failed",
                    "run_id": None,
                    "error": problem("conversation.run_missing", "The answer's run never started"),
                    "finished_at": now(),
                }
            )
            key = _key(message.conversation_id, message.seq)
            if self.store.replace_if(MESSAGES, key, expired, field="run_id", expected=RESERVED):
                return expired
            # The send wrote its run in the meantime: the answer is that run's.
            stored = self.store.get(MESSAGES, key, ConversationMessage)
            return message if stored is None or stored.run_id == RESERVED else self._sync(stored)
        run = self.workspace.runs.get(message.run_id) if message.run_id else None
        if run is None:
            updated = message.model_copy(
                update={
                    "status": "failed",
                    "error": problem("conversation.run_missing", "The answer's run no longer exists"),
                    "finished_at": now(),
                }
            )
        else:
            updated = self._from_run(message, run)
        if updated.status != message.status:
            self.store.put(MESSAGES, _key(message.conversation_id, message.seq), updated)
            if updated.status in FINAL:
                self._record_files(updated, run)
        return updated

    @staticmethod
    def _from_run(message: ConversationMessage, run: Run) -> ConversationMessage:
        state = run.nodes.get(AGENT_NODE)
        content = ""
        calls: list[ToolCallView] = []
        if state is not None:
            content = as_text(state.output) if state.output is not None else state.text
            calls = [
                ToolCallView(
                    id=call.id,
                    name=call.name,
                    arguments=call.input,
                    result=None if call.output is None else call.output[:MAX_TOOL_RESULT_CHARS],
                    is_error=call.is_error,
                    started_at=call.started_at,
                    finished_at=call.finished_at,
                    duration_ms=_duration_ms(call.started_at, call.finished_at),
                    approval=call.approval,
                )
                for call in state.tool_calls
            ]
        error: Problem | None = None
        if run.status in ("failed", "cancelled"):
            if state is not None and state.error_code:
                error = problem(state.error_code, state.error or "", **state.error_params)
            elif run.error_code:
                error = problem(run.error_code, run.error or "", **run.error_params)
        status: MessageStatus = run.status
        return message.model_copy(
            update={
                "content": content,
                "tool_calls": calls,
                "sources": list(run.sources),
                "status": status,
                "error": error,
                "usage": run.usage,
                "cost_usd": _cost(run.usage),
                "approval_ids": [
                    call.approval.request_id
                    for call in calls
                    if call.approval is not None and call.approval.status == "pending" and call.approval.request_id
                ],
                "finished_at": run.finished_at,
            }
        )

    def _record_files(self, answer: ConversationMessage, run: Run | None) -> None:
        """How the answer's model received the files of the message it answers (from the run's step)."""
        state = run.nodes.get(AGENT_NODE) if run is not None else None
        if state is None or not state.files or answer.reply_to is None:
            return
        provided = {item.file_id: item for item in state.files}
        key = _key(answer.conversation_id, answer.seq - 1)
        user = self.store.get(MESSAGES, key, ConversationMessage)
        if user is None or user.id != answer.reply_to:
            return
        for item in user.attachments:
            given = provided.get(item.file_id)
            if given is not None:
                item.mode = given.mode
                item.notes = list(given.notes)
        self.store.put(MESSAGES, key, user)

    def run_of(self, conversation_id: str, owner: str | None, message_id: str) -> str:
        """The run of an answer (its events are the answer's stream)."""
        found = self.message(conversation_id, owner, message_id)
        if not found.run_id:
            raise ConversationNotFound(message_id)
        return found.run_id

    # ---- export -------------------------------------------------------------------------------------------------

    def export_markdown(self, conversation_id: str, owner: str | None) -> str:
        """The conversation as Markdown: its messages with files, tool calls, sources, runs and cost."""
        detail = self.detail(conversation_id, owner)
        template = self.templates.get(detail.agent.template_id)
        agent_name = template.agent.name if template is not None else detail.agent.template_id
        cost = detail.usage.estimated_cost_usd
        lines = [
            f"# {detail.title or 'Conversation'}",
            "",
            f"- Agent: {agent_name} (template {detail.agent.template_id}, version {detail.agent.version})",
            f"- Model: {detail.model}",
            f"- Cost: {'unknown' if cost is None else f'${cost:.4f}'}",
            f"- Exported: {_stamp(datetime.now(UTC))}",
        ]
        for item in detail.messages:
            if item.summary is not None:
                lines += ["", f"## History · {_summary_label(item.summary)}", "", item.summary.text]
            who = "User" if item.role == "user" else f"Assistant ({item.model})"
            lines += ["", f"## {who} · {_stamp(item.created_at)}", "", item.content or "_(no text)_"]
            if item.attachments:
                lines += ["", "Files:"]
                lines += [f"- {file.name}" + (f" ({file.mode})" if file.mode else "") for file in item.attachments]
            if item.tool_calls:
                lines += ["", "Tool calls:"]
                for call in item.tool_calls:
                    took = f", {call.duration_ms} ms" if call.duration_ms is not None else ""
                    arguments = json.dumps(call.arguments, ensure_ascii=False)
                    lines.append(f"- `{call.name}`{took}: `{arguments}`")
                    if call.result is not None:
                        lines.append(f"  - {'Error' if call.is_error else 'Result'}: {_one_line(call.result)}")
            if item.sources:
                lines += ["", "Sources:"]
                lines += [f"- [{source.title or source.url}]({source.url})" for source in item.sources]
            if item.role == "assistant":
                spent = "unknown" if item.cost_usd is None else f"${item.cost_usd:.4f}"
                note = f"Run {item.run_id} · {item.status} · cost {spent}"
                if item.error is not None:
                    note += f" · {item.error.code}"
                lines += ["", f"_{note}_"]
        return "\n".join(lines) + "\n"


def _summary_label(summary: HistorySummary) -> str:
    """Where a history summary comes from: its messages, its method and the helper model."""
    span = f"messages {summary.first_seq} to {summary.last_seq}"
    if summary.method == "trimmed":
        note = f"{span} left out" + (f" ({summary.error})" if summary.error else "")
    else:
        note = f"{span} summarized by {summary.model}" + (f" (run {summary.run_id})" if summary.run_id else "")
    return note


def _stamp(value: datetime) -> str:
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _one_line(text: str, limit: int = 500) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


__all__ = [
    "AGENT_NODE",
    "COLLECTION",
    "CONVERSATIONS_ENV",
    "MESSAGES",
    "NO_OWNER",
    "AgentChoice",
    "AgentConversationDetail",
    "AgentConversationFile",
    "AgentConversationSummary",
    "Conversation",
    "ConversationChange",
    "ConversationIn",
    "ConversationMessage",
    "ConversationNotFound",
    "Conversations",
    "HistorySummary",
    "MessageFile",
    "MessageIn",
    "MessageStatus",
    "SentConversationMessage",
    "ToolCallView",
    "answer_agent",
    "answer_input",
    "answer_workflow",
    "conversations_enabled",
    "earlier_files",
    "history_budget",
    "history_turns",
    "render_input",
    "summary_workflow",
]
