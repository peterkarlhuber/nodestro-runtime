"""Conversations with one's own agents (N-04): a conversation object on top of the one engine.

A conversation talks to one agent template in a fixed version (``agent``) with one model (``model``); both can be
switched between answers. Its parts:

- **Storage**: conversations and their messages are documents of the tenant's store (collections
  :data:`COLLECTION` and :data:`MESSAGES`, message key ``<conversation id>:<seq>``), so every query is bound to the
  tenant like every other document (on PostgreSQL rows of ``nodestro_documents`` with ``tenant_id``; no migration).
  Each conversation also records its tenant and its owner: on a server only its owner sees it (``owner``), on the
  desktop everyone of the workspace (``owner`` ``None``), as for the Nodestro Assistant.
- **Answers**: every answer is one run of the engine (trigger ``conversation``, ``trigger_ref`` = the conversation
  id) with a start step and the agent: there is no second agent loop. The run executes the agent with its tools,
  knowledge, per-call tool approvals (S-2) and access grants (N-06; a grant for "this conversation" names the
  conversation id), and its usage is the run's usage, so it shows in the run list and the usage ledger. The answer
  message references its run (``run_id``) and, while a tool call waits for a person, the approval requests
  (``approval_ids``).
- **History**: the run's input is the new message after the earlier messages of the conversation (the newest
  :data:`HISTORY_MESSAGES`), as text. Compacting long conversations is N-05.
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
  it is marked unavailable (:mod:`nodestro_runtime.file_delivery`).
- **Export**: :meth:`Conversations.export_markdown`.

The switch :data:`CONVERSATIONS_ENV` (default off) guards the API routes (nodestro-api); this module itself has no
effect until a conversation is created.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
import json
import os
import re
import threading
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from .agent_templates import AgentTemplates
from .engine import add_usage, as_text
from .input_files import InputFile
from .launcher import launch
from .problems import NodestroError, Problem, problem
from .providers import check_model, provider_of
from .schemas import (
    AgentConfig,
    Edge,
    Node,
    NotificationRules,
    Run,
    RunIn,
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

#: The switch of the conversation API (N-04): ``1`` turns it on; off by default.
CONVERSATIONS_ENV = "NODESTRO_CONVERSATIONS"
COLLECTION = "conversations"
MESSAGES = "conversation_messages"
#: The provider of local models: the only one a conversation picks without being told.
LOCAL_PROVIDER = "ollama"
#: Earlier messages that go to the model with a new one (the newest ones).
HISTORY_MESSAGES = 30
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

#: N-08 (outflow protection by content provenance) will record here what the conversation holds; until then every
#: conversation is ``unchecked``.
ProvenanceStatus = Literal["unchecked"]
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
    #: The provenance of the conversation's content (N-08): a placeholder.
    provenance: ProvenanceStatus = "unchecked"
    #: The conversation's budget in USD over all its answers (``None``: each answer has the default budget).
    budget_usd: float | None = Field(default=None, ge=0)
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
    #: Answers: the usage and cost of the answer's run.
    usage: Usage | None = None
    cost_usd: float | None = None
    #: Answers: the run that wrote it, and the approval requests it waits for.
    run_id: str | None = None
    approval_ids: list[str] = Field(default_factory=list)
    reply_to: str | None = None
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


class ConversationChange(BaseModel):
    """A change of a conversation: only the fields that are sent change (rename, switch agent or model)."""

    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    agent: AgentChoice | None = None
    model: str | None = Field(default=None, max_length=200)
    offline: bool | None = None
    budget_usd: float | None = Field(default=None, ge=0)


class MessageIn(BaseModel):
    #: May be empty when files are sent.
    content: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    file_ids: list[str] = Field(default_factory=list, max_length=MAX_FILES_PER_MESSAGE)


class SentConversationMessage(BaseModel):
    user: ConversationMessage
    assistant: ConversationMessage


def visible(conversation: Conversation, owner: str | None) -> bool:
    """Whether ``owner`` sees the conversation: everyone without an owner (the desktop), else its owner and the
    conversations stored without one."""
    return owner is None or conversation.owner in (None, owner)


def make_title(text: str) -> str:
    """A title from the first message, without an AI request."""
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    line = " ".join(re.sub(r"[`*_#>\[\]]", "", line).split())
    if len(line) > _TITLE_LENGTH:
        line = (line[:_TITLE_LENGTH].rsplit(" ", 1)[0] or line[:_TITLE_LENGTH]).rstrip(",.;:") + "…"
    return line


def is_local(model: str) -> bool:
    return provider_of(model) == LOCAL_PROVIDER


def answer_input(history: list[ConversationMessage], content: str) -> str:
    """The run's input of an answer: the earlier messages (the newest :data:`HISTORY_MESSAGES`) and the new one."""
    earlier = [item for item in history if item.role == "user" or item.status == "succeeded"][-HISTORY_MESSAGES:]
    if not earlier:
        return content
    parts = ["The conversation so far (oldest first):"]
    for item in earlier:
        parts.append(f"{'User' if item.role == 'user' else 'Assistant'}: {item.content}")
    parts.append(f"The user's new message:\n{content}")
    return "\n\n".join(parts)


def answer_agent(config: AgentConfig, *, model: str, ref: TemplateRef) -> AgentConfig:
    """The template's agent as it answers in a conversation: the conversation's model, no cloud fallback (a cloud
    model is never chosen automatically) and the message as its task."""
    return config.model_copy(
        update={
            "model": model,
            "fallbacks": [item for item in config.fallbacks if is_local(item)],
            "task_template": "{{input}}",
            "origin": ref,
        },
        deep=True,
    )


def answer_workflow(conversation: Conversation, config: AgentConfig, budget: float | None) -> Workflow:
    """The workflow one answer runs: the start step and the agent (it notifies nobody; the person waits)."""
    return Workflow(
        id=conversation.id,
        name=conversation.title or "Conversation",
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
        self._lock = threading.Lock()
        self._sending: dict[str, asyncio.Lock] = {}

    # ---- reading ------------------------------------------------------------------------------------------------

    def _stored(self, conversation_id: str, owner: str | None) -> Conversation:
        found = self.store.get(COLLECTION, conversation_id, Conversation)
        if found is None or found.tenant_id != self.workspace.tenant_id or not visible(found, owner):
            raise ConversationNotFound(conversation_id)
        return found

    def get(self, conversation_id: str, owner: str | None) -> Conversation:
        """The conversation; :class:`ConversationNotFound` for an unknown id and for another owner's."""
        return self._stored(conversation_id, owner)

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
                    cost_usd=self._usage(messages).estimated_cost_usd,
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
            **conversation.model_dump(), messages=messages, usage=self._usage(messages), answering=answering
        )

    def usage(self, conversation_id: str, owner: str | None) -> Usage:
        """The sum of the usage of every answer's run (failed and cancelled ones included)."""
        return self._usage(self.messages(conversation_id, owner))

    @staticmethod
    def _usage(messages: list[ConversationMessage]) -> Usage:
        total = Usage()
        for item in messages:
            if item.usage is not None:
                total = add_usage(total, item.usage)
        return total

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
        ref, config = self._agent(request.agent)
        model = request.model if request.model else self._default_model(config)
        conversation = Conversation(
            tenant_id=self.workspace.tenant_id,
            owner=owner,
            title=request.title.strip(),
            auto_title=not request.title.strip(),
            agent=ref,
            model=self._checked_model(model, offline=request.offline),
            offline=request.offline,
            budget_usd=request.budget_usd,
        )
        self.store.put(COLLECTION, conversation.id, conversation)
        return self.detail(conversation.id, owner)

    def update(self, conversation_id: str, owner: str | None, change: ConversationChange) -> AgentConversationDetail:
        """Rename it, switch its agent or model, its offline flag or budget; only the fields sent change."""
        sent = change.model_fields_set
        with self._lock:
            conversation = self._stored(conversation_id, owner)
            if "title" in sent and change.title is not None:
                conversation.title = change.title.strip()
                conversation.auto_title = False
            if "agent" in sent and change.agent is not None:
                conversation.agent, _config = self._agent(change.agent)
            if "offline" in sent and change.offline is not None:
                conversation.offline = change.offline
            if "model" in sent and change.model:
                conversation.model = change.model
            if "budget_usd" in sent:
                conversation.budget_usd = change.budget_usd
            conversation.model = self._checked_model(conversation.model, offline=conversation.offline)
            conversation.updated_at = now()
            self.store.put(COLLECTION, conversation.id, conversation)
        return self.detail(conversation_id, owner)

    def delete(self, conversation_id: str, owner: str | None) -> None:
        """Delete the conversation with its messages and files; a running answer is cancelled first. The answers'
        runs stay (they are runs of the tenant, with their cost in the usage ledger)."""
        conversation = self._stored(conversation_id, owner)
        for item in self.messages(conversation_id, owner):
            if item.status not in FINAL and item.run_id is not None:
                self.workspace.cancel(item.run_id)
        self.store.delete_prefix(MESSAGES, f"{conversation_id}:")
        self.store.delete(COLLECTION, conversation_id)
        for file in conversation.files:
            if file.id in self.workspace.inputs:
                self.workspace.inputs.delete(file.id, force=True)

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
        try:
            with self._lock:
                conversation = self._room(self._stored(conversation_id, owner))
                attached = AgentConversationFile(
                    id=item.id, name=item.name, format=item.format, media_type=item.media_type, size=item.size
                )
                conversation.files.append(attached)
                conversation.updated_at = now()
                self.store.put(COLLECTION, conversation.id, conversation)
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
        with self._lock:
            conversation = self._stored(conversation_id, owner)
            if not any(item.id == file_id for item in conversation.files):
                raise ConversationNotFound(file_id)
            conversation.files = [item for item in conversation.files if item.id != file_id]
            conversation.updated_at = now()
            self.store.put(COLLECTION, conversation.id, conversation)
        if file_id in self.workspace.inputs:
            self.workspace.inputs.delete(file_id, force=True)
        return self.detail(conversation_id, owner)

    # ---- answers ------------------------------------------------------------------------------------------------

    async def send(
        self, conversation_id: str, owner: str | None, request: MessageIn, *, actor: str = "local"
    ) -> SentConversationMessage:
        """Store the message and start its answer as a run; follow it with the run's events.

        Nothing is stored when the answer cannot start (an unknown file, a running answer, the budget is used up, a
        refused model or workflow start): the message stays with the sender."""
        lock = self._sending.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            conversation = self._stored(conversation_id, owner)
            content = request.content.strip()
            if not content and not request.file_ids:
                raise NodestroError("conversation.empty_message", "The message is empty")
            history = self.messages(conversation_id, owner)
            if any(item.status not in FINAL for item in history):
                raise NodestroError("conversation.answer_running", "Wait until the current answer is finished")
            files = {item.id: item for item in conversation.files}
            missing = [file_id for file_id in request.file_ids if file_id not in files]
            if missing:
                raise ConversationNotFound(missing[0])
            budget: float | None = None
            if conversation.budget_usd is not None:
                total = self._usage(history)
                spent = total.known_cost_usd if total.known_cost_usd is not None else total.estimated_cost_usd or 0.0
                budget = conversation.budget_usd - spent
                if budget <= 0:
                    raise NodestroError(
                        "conversation.budget_exhausted",
                        "The conversation's budget is used up",
                        budget_usd=conversation.budget_usd,
                    )
            model = self._checked_model(conversation.model, offline=conversation.offline)
            ref, template_agent = self._agent(
                AgentChoice(template_id=conversation.agent.template_id, version=conversation.agent.version)
            )
            config = answer_agent(template_agent, model=model, ref=ref)
            file_ids = list(dict.fromkeys(request.file_ids))
            run = await launch(
                self.workspace,
                answer_workflow(conversation, config, budget),
                RunIn(input=answer_input(history, content), file_ids=file_ids),
                trigger="conversation",
                trigger_ref=conversation.id,
                started_by=actor,
            )
            with self.store.transaction():
                self.store.lock(COLLECTION, conversation_id)
                current = self._stored(conversation_id, owner)
                user = ConversationMessage(
                    conversation_id=conversation_id,
                    seq=current.last_seq + 1,
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
                    seq=current.last_seq + 2,
                    role="assistant",
                    status="queued",
                    agent=ref,
                    model=model,
                    run_id=run.id,
                    reply_to=user.id,
                )
                current.last_seq += 2
                if current.auto_title and not current.title:
                    current.title = make_title(content) or (user.attachments[0].name if user.attachments else "")
                current.updated_at = now()
                self.store.put(MESSAGES, _key(conversation_id, user.seq), user)
                self.store.put(MESSAGES, _key(conversation_id, answer.seq), answer)
                self.store.put(COLLECTION, conversation_id, current)
        return SentConversationMessage(user=user, assistant=self._sync(answer))

    def cancel(self, conversation_id: str, owner: str | None, message_id: str) -> ConversationMessage:
        """Cancel a running answer: its run is cancelled, which closes the model connection."""
        found = self.message(conversation_id, owner, message_id)
        if found.status not in FINAL and found.run_id is not None:
            self.workspace.cancel(found.run_id)
        return self.message(conversation_id, owner, message_id)

    def _sync(self, message: ConversationMessage) -> ConversationMessage:
        """An answer as its run is now; stored once the run's status changed (a final one, a pause)."""
        if message.role != "assistant" or message.status in FINAL or message.run_id is None:
            return message
        run = self.workspace.runs.get(message.run_id)
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
        if found.run_id is None:
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
    "MessageFile",
    "MessageIn",
    "MessageStatus",
    "SentConversationMessage",
    "ToolCallView",
    "answer_agent",
    "answer_input",
    "answer_workflow",
    "conversations_enabled",
]
