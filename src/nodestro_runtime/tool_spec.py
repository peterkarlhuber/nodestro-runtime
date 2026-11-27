"""What Nodestro knows about each built-in tool: :class:`ToolSpec` (ADR-0001, NAL-A1).

A spec is the tool's catalog entry: a stable id and version, a category, the capability statement of the shared tool
model (S-1, :mod:`nodestro_runtime.tool_policy`), whether it needs the network and still works in the strict offline
mode, which service connection it needs, the binding parameters an agent may set, the cost unit, the modes it runs
in, its time limit and its call limit per run. The specs are the single source for the tool catalog, the offline and
connection checks of the workflow check, and the limits a call runs under; the capability itself stays in
:data:`~nodestro_runtime.tool_policy.BUILTIN_CAPABILITIES` (security-reviewed) and the spec takes it from there.

At run time a built-in tool is wrapped (:func:`bind_tool`): every call counts against ``max_calls_per_run``, runs
under ``timeout_seconds`` and sees its connection and parameters through :func:`current_tool_context`. What the model
sees (name, description, input schema) never contains connection data or parameters.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from decimal import Decimal
import os
import re
import threading
from typing import Any, Literal

from orkexa import Tool, ToolResultError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .connections import ResolvedConnection
from .folders import FolderReader
from .mail import Attachment
from .schemas import ToolSettings
from .source_ledger import SourceLedger
from .tool_policy import BUILTIN_CAPABILITIES, ToolCapability

Category = Literal["compute", "data", "files", "web", "communication"]
Network = Literal["none", "external"]
SpecMode = Literal["desktop", "local-container", "server", "export"]
ALL_MODES: frozenset[SpecMode] = frozenset({"desktop", "local-container", "server", "export"})

#: Names of built-in tools that later packages add: no own or generated tool may take them, so a stored tool never
#: shadows them when they arrive. The NAL-A2 web tools arrived and keep their names reserved.
PLANNED_BUILTIN_NAMES: frozenset[str] = frozenset({"search_web", "fetch_web_page"})
#: The environment variable that offers the web tools (NAL-A2) in the server profile. Off unless set to ``1``: the
#: owner enables them only after the independent review of the egress guard (D1, RT-077).
WEB_TOOLS_SERVER_ENV = "NODESTRO_WEB_TOOLS_SERVER"
#: The modes the web tools run in without that switch (never ``server``).
_WEB_MODES: frozenset[SpecMode] = frozenset({"desktop", "local-container", "export"})

_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
#: Upper bound of a spec's time limit; a call that needs longer belongs in a job, not in a tool round.
MAX_TIMEOUT_SECONDS = 600.0


class ToolSpec(BaseModel):
    """The catalog entry of one built-in tool (see the module documentation)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Stable, snake_case, unique across built-in tools; the name agents bind the tool by.
    id: str
    #: Raised when behavior or schema change; frozen into releases as ``id@version``.
    version: int = Field(default=1, ge=1)
    category: Category
    capability: ToolCapability
    network: Network = "none"
    #: Usable while the strict offline mode is on.
    offline: bool = True
    #: The connection type the tool needs (``search``, ``smtp``, ``webhook``, ...); ``None``: none.
    connection_type: str | None = None
    #: JSON Schema of the binding parameters an agent sets (never visible to the model); ``{}``: none.
    params_schema: dict[str, Any] = Field(default_factory=dict)
    #: What one priced call is called (``search_request``); priced through the connection (NAL-A2).
    cost_unit: str | None = None
    modes: frozenset[SpecMode] = ALL_MODES
    timeout_seconds: float = Field(default=30.0, gt=0, le=MAX_TIMEOUT_SECONDS)
    max_calls_per_run: int | None = Field(default=None, ge=1, le=10_000)
    #: Prefix of the tool's texts in the studio (``<key>.name``, ``<key>.description``).
    i18n_key: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError("a tool id is snake_case, 2-64 characters, starting with a letter")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ToolSpec:
        if self.network == "external" and self.offline:
            raise ValueError("a tool that needs the network cannot be usable offline")
        if self.network == "external" and self.capability.reach != "external":
            raise ValueError("a tool that needs the network must state reach 'external'")
        if not self.i18n_key:
            object.__setattr__(self, "i18n_key", f"tools.builtin.{self.id}")
        return self

    @property
    def ref(self) -> str:
        """``id@version``, as releases record it."""
        return f"{self.id}@{self.version}"

    def catalog(self) -> dict[str, Any]:
        """The spec for the API's tool catalog."""
        data = self.model_dump(mode="json")
        data["modes"] = sorted(self.modes)
        data["ref"] = self.ref
        return data


def _builtin(tool_id: str, category: Category) -> ToolSpec:
    return ToolSpec(id=tool_id, category=category, capability=BUILTIN_CAPABILITIES[tool_id], timeout_seconds=30.0)


_HOSTS = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 253}, "maxItems": 100}

#: ``search_web`` (NAL-A2): the search provider of a ``search`` connection; binding parameters narrow what the model
#: may ask for and which hits it gets.
SEARCH_WEB_SPEC = ToolSpec(
    id="search_web",
    category="web",
    capability=BUILTIN_CAPABILITIES["search_web"],
    network="external",
    offline=False,
    connection_type="search",
    cost_unit="search_request",
    modes=_WEB_MODES,
    timeout_seconds=20.0,
    max_calls_per_run=10,
    params_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
            "region": {"type": "string", "pattern": "^[A-Za-z]{2}$"},
            "language": {"type": "string", "pattern": "^[A-Za-z]{2}$"},
            "time_range": {"enum": ["day", "week", "month", "year"]},
            "allow_hosts": _HOSTS,
            "deny_hosts": _HOSTS,
            "max_calls": {"type": "integer", "minimum": 1, "maximum": 10},
        },
    },
)
#: ``fetch_web_page`` (NAL-A2): reads one page through the egress guard; no connection.
FETCH_WEB_PAGE_SPEC = ToolSpec(
    id="fetch_web_page",
    category="web",
    capability=BUILTIN_CAPABILITIES["fetch_web_page"],
    network="external",
    offline=False,
    modes=_WEB_MODES,
    timeout_seconds=30.0,
    max_calls_per_run=20,
    params_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "allow_hosts": _HOSTS,
            "deny_hosts": _HOSTS,
            "max_chars": {"type": "integer", "minimum": 500, "maximum": 50_000},
            "max_calls": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    },
)

#: ``notify_user`` (NAL-A3): an internal message in the inbox; the run allows three in total
#: (:data:`~nodestro_runtime.notifications.MAX_MESSAGES_PER_RUN`), the per-step limit is the same number.
NOTIFY_USER_SPEC = ToolSpec(
    id="notify_user",
    category="communication",
    capability=BUILTIN_CAPABILITIES["notify_user"],
    timeout_seconds=10.0,
    max_calls_per_run=3,
)

#: The folder tools (N-10): they read granted folders (:mod:`nodestro_runtime.folders`) on this computer, so they work
#: offline and only where a person's folders are: the desktop and Nodestro Local Docker, never a server. They are
#: offered only while ``NODESTRO_FOLDER_GRANTS=1`` (:func:`offered`).
FOLDER_TOOL_IDS: frozenset[str] = frozenset({"list_folder", "read_file", "search_files"})
_FOLDER_MODES: frozenset[SpecMode] = frozenset({"desktop", "local-container"})
LIST_FOLDER_SPEC = ToolSpec(
    id="list_folder",
    category="files",
    capability=BUILTIN_CAPABILITIES["list_folder"],
    modes=_FOLDER_MODES,
    timeout_seconds=30.0,
    max_calls_per_run=100,
)
#: PDFs are read in an isolated process within the grant's time budget (at most 60 seconds).
READ_FILE_SPEC = ToolSpec(
    id="read_file",
    category="files",
    capability=BUILTIN_CAPABILITIES["read_file"],
    modes=_FOLDER_MODES,
    timeout_seconds=90.0,
    max_calls_per_run=100,
)
#: A search stops after ``folders.SEARCH_SECONDS`` (30) and returns what it found.
SEARCH_FILES_SPEC = ToolSpec(
    id="search_files",
    category="files",
    capability=BUILTIN_CAPABILITIES["search_files"],
    modes=_FOLDER_MODES,
    timeout_seconds=60.0,
    max_calls_per_run=30,
)

#: ``draft_email`` / ``draft_webhook`` (NAL-A4): a draft file in the run, no network, no connection. The sender of an
#: e-mail draft and the schema of a webhook payload are binding parameters (never visible to the model).
DRAFT_EMAIL_SPEC = ToolSpec(
    id="draft_email",
    category="communication",
    capability=BUILTIN_CAPABILITIES["draft_email"],
    timeout_seconds=20.0,
    max_calls_per_run=10,
    params_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {"from_address": {"type": "string", "minLength": 3, "maxLength": 254}},
    },
)
DRAFT_WEBHOOK_SPEC = ToolSpec(
    id="draft_webhook",
    category="communication",
    capability=BUILTIN_CAPABILITIES["draft_webhook"],
    timeout_seconds=10.0,
    max_calls_per_run=10,
    params_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {"payload_schema": {"type": "object"}},
    },
)

#: The specs of Nodestro's built-in tools (``builtin_tools.BUILTIN_TOOLS`` and ``web_tools.WEB_TOOLS``), by name.
BUILTIN_SPECS: dict[str, ToolSpec] = {
    spec.id: spec
    for spec in (
        _builtin("calculator", "compute"),
        _builtin("current_datetime", "compute"),
        _builtin("days_between", "compute"),
        _builtin("text_statistics", "data"),
        _builtin("table_totals", "data"),
        SEARCH_WEB_SPEC,
        FETCH_WEB_PAGE_SPEC,
        NOTIFY_USER_SPEC,
        DRAFT_EMAIL_SPEC,
        DRAFT_WEBHOOK_SPEC,
        LIST_FOLDER_SPEC,
        READ_FILE_SPEC,
        SEARCH_FILES_SPEC,
    )
}


def offered(spec: ToolSpec, mode: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``spec``'s tool runs in ``mode``. The web tools join the server profile only with
    ``NODESTRO_WEB_TOOLS_SERVER=1`` (:data:`WEB_TOOLS_SERVER_ENV`); the folder tools run only while
    ``NODESTRO_FOLDER_GRANTS=1`` (:data:`~nodestro_runtime.folders.FOLDERS_ENV`)."""
    if spec.id in FOLDER_TOOL_IDS:
        from .folders import folders_enabled

        return mode in spec.modes and folders_enabled(environ)
    if mode in spec.modes:
        return True
    if mode == "server" and spec.id in PLANNED_BUILTIN_NAMES:
        return (environ if environ is not None else os.environ).get(WEB_TOOLS_SERVER_ENV, "").strip() == "1"
    return False


#: The time limit of an integration's tool without a spec (contract 1): generous, since such tools (e.g. recognising a
#: scanned document) may take a while, but a hanging one no longer holds the run.
HOST_TOOL_TIMEOUT_SECONDS = 300.0
#: How much longer the run waits for a tool that ends its own execution at the time limit (an extension tool in the
#: Docker sandbox removes its container, N-22), so its answer is that timeout and not an abandoned thread.
STOP_GRACE_SECONDS = 30.0


def host_tool_spec(name: str) -> ToolSpec | None:
    """Limits for an integration's tool without a spec: only a time limit; never used for the catalog, the offline
    check or the approval rules (its capability stays unknown)."""
    if not _ID.match(name):
        return None
    from .tool_policy import UNKNOWN

    return ToolSpec(
        id=name,
        category="data",
        capability=UNKNOWN,
        network="external",
        offline=False,
        timeout_seconds=HOST_TOOL_TIMEOUT_SECONDS,
    )


def builtin_spec(name: str) -> ToolSpec | None:
    """The spec of Nodestro's own built-in tool ``name`` (``None`` for every other tool)."""
    return BUILTIN_SPECS.get(name)


# ---- binding settings ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingsProblem:
    code: str
    message: str
    params: dict[str, Any] = field(default_factory=dict)


def _settings_issue(code: str, message: str, **params: Any) -> SettingsProblem:
    return SettingsProblem(code, message, params)


def settings_problems(name: str, spec: ToolSpec | None, settings: ToolSettings) -> list[SettingsProblem]:
    """What is wrong with ``settings`` of tool ``name`` (validation only; whether the connection exists is checked by
    the workflow check)."""
    if spec is None:
        return [_settings_issue("tool.settings_unsupported", f"{name} has no settings", name=name)]
    found: list[SettingsProblem] = []
    if settings.connection_id and spec.connection_type is None:
        found.append(
            _settings_issue("tool.settings_invalid", f"{name} uses no connection", name=name, at="connection_id")
        )
    if settings.params:
        schema = spec.params_schema or {"type": "object", "properties": {}, "additionalProperties": False}
        import jsonschema  # type: ignore[import-untyped, unused-ignore]

        validator = jsonschema.validators.validator_for(schema)(schema)
        for error in sorted(validator.iter_errors(settings.params), key=lambda item: list(item.path)):
            where = "/".join(str(part) for part in error.path) or "params"
            found.append(
                _settings_issue("tool.settings_invalid", f"{name}: invalid parameter at {where}", name=name, at=where)
            )
    return found


# ---- run-time context, time and call limits ----------------------------------------------------------------------


@dataclass(frozen=True)
class RunServices:
    """What a run lends the built-in tools of one step (NAL-A2): its source ledger and the booking of a tool's own
    cost into the run's budget. Outside a run (the agent test, validation) a tool gets none."""

    #: The run's source ledger and the step that records into it.
    ledger: SourceLedger | None = None
    node_id: str | None = None
    #: Books ``units`` of the tool's ``cost_unit`` at ``cost_usd`` in total (``None``: unknown) for ``provider``.
    book: Callable[[str, str, int, Decimal | None], None] | None = None
    #: Raises when the run's budget is used up (asked before a priced call).
    check_budget: Callable[[], None] | None = None
    #: The installation's mode (``desktop``, ``local-container``, ``server``, ``export``).
    mode: str = "desktop"
    #: Set when the tool is not offered in ``mode``: every call is refused with this code.
    refused: str | None = None
    #: Pages a run's tools already read (url -> result), so a page is fetched once per step; memory only.
    cache: dict[str, Any] = field(default_factory=dict)
    #: Calls per tool across all steps of the run; each tool may make at most :data:`RUN_CALL_FACTOR` times its
    #: per-step limit in one run (``None`` outside a run).
    run_calls: CallCounter | None = None
    #: Leaves an internal message for the run's starter (NAL-A3): ``(title, text, level) -> message id``; raises
    #: ``ValueError`` with a code. ``None``: no inbox here (outside a run, or a server with Axentra's inbox).
    notify: Callable[[str, str, str], str] | None = None
    #: Stores a draft of NAL-A4 with the top-level run: ``(name, format, data, preview) -> "<file name> (<id>)"``.
    #: ``None``: no run files here (outside a run, an imported run).
    save_file: Callable[[str, str, bytes, dict[str, Any]], str] | None = None
    #: A file of the run by its file name (input or result file), read for an attachment: ``(name, max_bytes)``;
    #: ``None`` if unknown, ``DraftError("mail.too_large")`` above ``max_bytes`` (checked before reading).
    run_file: Callable[[str, int], Attachment | None] | None = None
    #: The granted folders this step may read (N-10, :class:`~nodestro_runtime.folders.FolderReader`); ``None``:
    #: none here (the switch is off, outside a run, or not in this mode).
    folders: FolderReader | None = None
    #: N-08: raises before the agent's knowledge search embeds its query (vector or hybrid), when the query may not
    #: reach the embedding model at the given address (the one it really sends to,
    #: :meth:`~nodestro_runtime.knowledge.KnowledgeBase.views`); ``None``: no check.
    embedding_check: Callable[[str | None], None] | None = None
    #: RT-089d (Welle 5): asked with the tool's name before each call resolves its connection, in a step that opened a
    #: person's memory space under their consent; raises :class:`ToolResultError` when the connection (or anything
    #: else the consent covers) changed since the step's check. ``None``: no check.
    connection_check: Callable[[str], None] | None = None


@dataclass(frozen=True)
class ToolContext:
    """What a built-in tool sees about its binding while it runs (never part of the model's input)."""

    spec: ToolSpec
    params: Mapping[str, Any]
    connection: ResolvedConnection | None = None
    services: RunServices = field(default_factory=RunServices)


_CONTEXT: ContextVar[ToolContext | None] = ContextVar("nodestro_tool_context", default=None)


def current_tool_context() -> ToolContext | None:
    """The binding of the built-in tool that is running now (``None`` outside a bound call)."""
    return _CONTEXT.get()


@contextmanager
def _bound(context: ToolContext) -> Any:
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


#: A run may call a built-in tool at most this many times its per-step limit, across all its steps and loops.
RUN_CALL_FACTOR = 5


class CallCounter:
    """Calls per tool of one agent run (the agent is built once per run of a node)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def take(self, name: str, limit: int | None) -> bool:
        with self._lock:
            count = self._counts.get(name, 0)
            if limit is not None and count >= limit:
                return False
            self._counts[name] = count + 1
            return True


class SpecTool(Tool):
    """A built-in tool under its spec: call limit, time limit and binding context. Name, description and input schema
    are the wrapped tool's, so the model sees exactly the same tool."""

    def __init__(
        self,
        inner: Tool,
        spec: ToolSpec,
        *,
        params: Mapping[str, Any] | None = None,
        connection: Callable[[], ResolvedConnection | None] | None = None,
        counter: CallCounter | None = None,
        services: RunServices | None = None,
    ) -> None:
        super().__init__(name=inner.name, strict=inner.strict, content_source=inner.content_source)
        self.inner = inner
        self.spec = spec
        self.params: Mapping[str, Any] = dict(params or {})
        self._resolve_connection = connection or (lambda: None)
        self._counter = counter or CallCounter()
        self.services = services or RunServices()
        self.description = inner.description
        self.return_description = inner.return_description
        self.input_schema = inner.input_schema
        self.output_schema = inner.output_schema
        self.returns_content = inner.returns_content
        self.is_async = False
        self.function = inner.function

    def __repr__(self) -> str:
        return f"<SpecTool {self.name!r} {self.spec.ref}>"

    def schema(self) -> dict[str, Any]:
        return self.inner.schema()

    def definition(self) -> Any:
        return self.inner.definition()

    @property
    def call_limit(self) -> int | None:
        """The spec's call limit, lowered by the binding's ``max_calls`` (never raised)."""
        limit = self.spec.max_calls_per_run
        wanted = self.params.get("max_calls")
        if isinstance(wanted, int) and not isinstance(wanted, bool) and wanted >= 1:
            limit = wanted if limit is None else min(limit, wanted)
        return limit

    def _context(self) -> ToolContext:
        if self.services.refused is not None:
            raise ToolResultError(f"Error: {self.name} is not offered here ({self.services.refused})")
        limit = self.call_limit
        if not self._counter.take(self.spec.id, limit):
            raise ToolResultError(
                f"Error: the call limit of {self.name} is reached ({limit} per run, tool.call_limit); answer with "
                "what you have"
            )
        run_calls = self.services.run_calls
        cap = None if self.spec.max_calls_per_run is None else self.spec.max_calls_per_run * RUN_CALL_FACTOR
        if run_calls is not None and not run_calls.take(self.spec.id, cap):
            raise ToolResultError(
                f"Error: the call limit of {self.name} for the whole run is reached ({cap} calls over all steps, "
                "tool.call_limit); answer with what you have"
            )
        return ToolContext(
            spec=self.spec, params=self.params, connection=self._resolve_connection(), services=self.services
        )

    def _timed_out(self) -> ToolResultError:
        return ToolResultError(
            f"Error: {self.name} did not finish within {self.spec.timeout_seconds:g} seconds (tool.timeout)"
        )

    def execute(self, payload: dict[str, Any]) -> Any:
        context = self._context()
        snapshot = copy_context()

        def call() -> Any:
            with _bound(context):
                return self.inner.execute(payload)

        # A host-process tool cannot be stopped from outside: after the time limit the run goes on without its result
        # and the worker thread ends when the function returns (built-in tools are short and side-effect free). A tool
        # that stops itself (an extension tool, whose container is removed at the limit) gets a little longer, so its
        # own timeout answers.
        wait = self.spec.timeout_seconds + (STOP_GRACE_SECONDS if getattr(self.inner, "stops_itself", False) else 0)
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{self.spec.id}")
        try:
            future = pool.submit(snapshot.run, call)
            try:
                return future.result(timeout=wait)
            except FutureTimeout:
                raise self._timed_out() from None
        finally:
            pool.shutdown(wait=False)

    async def aexecute(self, payload: dict[str, Any]) -> Any:
        import asyncio

        if not self.inner.is_async:
            return await asyncio.to_thread(self.execute, payload)
        context = self._context()
        with _bound(context):
            try:
                return await asyncio.wait_for(self.inner.aexecute(payload), self.spec.timeout_seconds)
            except TimeoutError:
                raise self._timed_out() from None


def bind_tool(
    tool: Tool,
    spec: ToolSpec,
    settings: ToolSettings | None,
    *,
    resolve: Callable[[str, str], ResolvedConnection] | None,
    counter: CallCounter,
    services: RunServices | None = None,
) -> SpecTool:
    """``tool`` under ``spec`` with an agent's ``settings``. The connection is resolved at each call (a changed secret
    applies to the next call), through ``resolve(connection_id, connection_type)``, after the services'
    ``connection_check`` (a step under a memory consent refuses a connection changed since its check)."""
    connection_id = settings.connection_id if settings is not None else None

    def connection() -> ResolvedConnection | None:
        if spec.connection_type is None:
            return None
        if not connection_id or resolve is None:
            raise ToolResultError(
                f"Error: {tool.name} has no {spec.connection_type} connection (check.connection_missing)"
            )
        if services is not None and services.connection_check is not None:
            services.connection_check(tool.name or "")  # before the connection is read (a refusal raises)
        try:
            return resolve(connection_id, spec.connection_type)
        except Exception as exc:  # the code only: a service message could carry an address or a secret
            code = getattr(exc, "code", "connection.unavailable")
            raise ToolResultError(f"Error: the connection of {tool.name} cannot be used ({code})") from None

    return SpecTool(
        tool,
        spec,
        params=settings.params if settings is not None else None,
        connection=connection,
        counter=counter,
        services=services,
    )


__all__ = [
    "ALL_MODES",
    "BUILTIN_SPECS",
    "DRAFT_EMAIL_SPEC",
    "DRAFT_WEBHOOK_SPEC",
    "FETCH_WEB_PAGE_SPEC",
    "FOLDER_TOOL_IDS",
    "HOST_TOOL_TIMEOUT_SECONDS",
    "LIST_FOLDER_SPEC",
    "PLANNED_BUILTIN_NAMES",
    "READ_FILE_SPEC",
    "SEARCH_FILES_SPEC",
    "SEARCH_WEB_SPEC",
    "STOP_GRACE_SECONDS",
    "WEB_TOOLS_SERVER_ENV",
    "CallCounter",
    "RunServices",
    "SettingsProblem",
    "SpecTool",
    "ToolContext",
    "ToolSettings",
    "ToolSpec",
    "bind_tool",
    "builtin_spec",
    "current_tool_context",
    "host_tool_spec",
    "offered",
    "settings_problems",
]
