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
import re
import threading
from typing import Any, Literal

from orkexa import Tool, ToolResultError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .connections import ResolvedConnection
from .schemas import ToolSettings
from .tool_policy import BUILTIN_CAPABILITIES, ToolCapability

Category = Literal["compute", "data", "files", "web", "communication"]
Network = Literal["none", "external"]
SpecMode = Literal["desktop", "local-container", "server", "export"]
ALL_MODES: frozenset[SpecMode] = frozenset({"desktop", "local-container", "server", "export"})

#: Names of built-in tools that later packages add (NAL-A2): no own or generated tool may take them now, so a stored
#: tool never shadows them when they arrive.
PLANNED_BUILTIN_NAMES: frozenset[str] = frozenset({"search_web", "fetch_web_page"})

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


#: The specs of Nodestro's built-in tools (``builtin_tools.BUILTIN_TOOLS``), by tool name.
BUILTIN_SPECS: dict[str, ToolSpec] = {
    spec.id: spec
    for spec in (
        _builtin("calculator", "compute"),
        _builtin("current_datetime", "compute"),
        _builtin("days_between", "compute"),
        _builtin("text_statistics", "data"),
        _builtin("table_totals", "data"),
    )
}


#: The time limit of an integration's tool without a spec (contract 1): generous, since such tools (e.g. recognising a
#: scanned document) may take a while, but a hanging one no longer holds the run.
HOST_TOOL_TIMEOUT_SECONDS = 300.0


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
class ToolContext:
    """What a built-in tool sees about its binding while it runs (never part of the model's input)."""

    spec: ToolSpec
    params: Mapping[str, Any]
    connection: ResolvedConnection | None = None


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
    ) -> None:
        super().__init__(name=inner.name, strict=inner.strict)
        self.inner = inner
        self.spec = spec
        self.params: Mapping[str, Any] = dict(params or {})
        self._resolve_connection = connection or (lambda: None)
        self._counter = counter or CallCounter()
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

    def _context(self) -> ToolContext:
        if not self._counter.take(self.spec.id, self.spec.max_calls_per_run):
            raise ToolResultError(
                f"Error: the call limit of {self.name} is reached ({self.spec.max_calls_per_run} per run, "
                "tool.call_limit); answer with what you have"
            )
        return ToolContext(spec=self.spec, params=self.params, connection=self._resolve_connection())

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
        # and the worker thread ends when the function returns (built-in tools are short and side-effect free).
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{self.spec.id}")
        try:
            future = pool.submit(snapshot.run, call)
            try:
                return future.result(timeout=self.spec.timeout_seconds)
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
) -> SpecTool:
    """``tool`` under ``spec`` with an agent's ``settings``. The connection is resolved at each call (a changed secret
    applies to the next call), through ``resolve(connection_id, connection_type)``."""
    connection_id = settings.connection_id if settings is not None else None

    def connection() -> ResolvedConnection | None:
        if spec.connection_type is None:
            return None
        if not connection_id or resolve is None:
            raise ToolResultError(
                f"Error: {tool.name} has no {spec.connection_type} connection (check.connection_missing)"
            )
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
    )


__all__ = [
    "ALL_MODES",
    "BUILTIN_SPECS",
    "HOST_TOOL_TIMEOUT_SECONDS",
    "PLANNED_BUILTIN_NAMES",
    "CallCounter",
    "SettingsProblem",
    "SpecTool",
    "ToolContext",
    "ToolSettings",
    "ToolSpec",
    "bind_tool",
    "builtin_spec",
    "current_tool_context",
    "host_tool_spec",
    "settings_problems",
]
