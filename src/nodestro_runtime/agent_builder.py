"""Turns an :class:`~nodestro_runtime.schemas.AgentConfig` into an ``orkexa.Agent``.

This is the one place where editor settings meet library arguments. The same
code path validates a configuration for the inspector (with a client that is
never called) and builds the agent for a run.

The agent's provider comes from its model (``provider:model``, see :mod:`nodestro_runtime.providers`).
Settings every provider shares are Orkexa's generic arguments; Anthropic's own options
(thinking, prompt caching, fallbacks, server tools) become ``orkexa.AnthropicOptions`` and
are refused for other providers instead of being dropped. Orkexa checks before anything is
sent that the model supports every feature the agent uses (tools, structured output,
streaming, reasoning effort ...); such errors appear in the inspector.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import keyword
import re
from typing import Any

from orkexa import (
    Agent,
    AnthropicOptions,
    ContentBlock,
    EphemeralMemory,
    MemoryBackend,
    Message,
    ModelCapabilities,
    Tool,
    UsageTracker,
)
from orkexa.dynamic import Sandbox
from orkexa.memory import MEMORY_TOOL_NAME
from pydantic import BaseModel, Field, create_model

from .catalog import SERVER_TOOLS
from .knowledge import KnowledgeBase
from .memory_spaces import MemorySpaces
from .problems import NodestroError, library_problem
from .providers import MAX_RETRIES, Connection, check_model, model_id, provider_of
from .schemas import AgentConfig, OutputConfig, OutputField
from .tool_library import ToolLibrary

_FIELD_TYPES: dict[str, Any] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "string_list": list[str],
    "number_list": list[float],
}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MODEL_NAME = re.compile(r"[^A-Za-z0-9_]")


class ConfigIssue(BaseModel):
    """A problem with an agent configuration; ``code``/``params`` are shown in the UI language."""

    level: str = "error"
    field: str | None = None
    code: str = "library"
    params: dict[str, Any] = Field(default_factory=dict)
    message: str


#: Defaults of the Anthropic-only settings: other providers accept a configuration only with these.
_ANTHROPIC_ONLY: dict[str, Any] = {
    "thinking": None,
    "thinking_display": None,
    "cache_control": True,
    "cache_ttl": "5m",
    "fallbacks": [],
    "server_tools": [],
}


@dataclass
class BuiltAgent:
    agent: Agent[Any]
    context_blocks: list[ContentBlock] = field(default_factory=list)
    memory: MemoryBackend | None = None


def output_model(config: OutputConfig) -> type[BaseModel] | None:
    """A Pydantic model for a structured output, or ``None`` for plain text."""
    if config.mode != "structured":
        return None
    if not config.fields:
        raise NodestroError("config.output_no_fields", "A structured output format needs at least one field")
    fields: dict[str, Any] = {}
    for item in config.fields:
        _check_field(item)
        annotation = _FIELD_TYPES[item.type]
        if item.required:
            fields[item.name] = (annotation, Field(description=item.description or item.name))
        else:
            fields[item.name] = (annotation | None, Field(default=None, description=item.description or item.name))
    name = _MODEL_NAME.sub("_", config.name.strip() or "Result") or "Result"
    if not name[0].isalpha():
        name = f"Result_{name}"
    return create_model(name, **fields)


def _check_field(item: OutputField) -> None:
    if not _IDENTIFIER.fullmatch(item.name) or keyword.iskeyword(item.name):
        raise NodestroError(
            "config.output_field_name",
            f"Invalid field name {item.name!r}: letters, digits and _ only",
            name=item.name,
        )


def _tool_choice(config: AgentConfig) -> str | dict[str, str]:
    """The generic tool choice of Orkexa (``any`` is ``required``)."""
    choice = config.tool_choice
    if choice.type == "tool":
        if not choice.name:
            raise NodestroError("config.tool_choice_missing", "Tool choice 'tool' needs a tool")
        return {"name": choice.name}
    return "required" if choice.type == "any" else choice.type


def _options(config: AgentConfig, provider: str, connection: Connection) -> Any:
    """Orkexa's options for the provider; Anthropic-only settings of another provider are an error."""
    if provider == "anthropic":
        for fallback in config.fallbacks:
            if provider_of(check_model(fallback)) != "anthropic":
                # Anthropic's fallbacks switch models within one request: only Anthropic models qualify.
                raise NodestroError(
                    "config.fallback_provider",
                    f"Fallback model {fallback!r} is not an Anthropic model; fallbacks stay with the agent's provider",
                    model=fallback,
                )
        return AnthropicOptions(
            thinking=config.thinking,
            thinking_display=config.thinking_display,
            cache_control=config.cache_control,
            cache_ttl=config.cache_ttl,
            fallbacks=[{"model": model_id(model)} for model in config.fallbacks] or None,
            request_options=dict(config.request_options),
        )
    for name, default in _ANTHROPIC_ONLY.items():
        if getattr(config, name) != default:
            raise NodestroError(
                "config.anthropic_only",
                f"{name} is an option of Anthropic models and cannot be used with provider {provider}",
                option=name,
                provider=provider,
            )
    return connection.options(config.request_options)


def _messages(config: AgentConfig) -> list[dict[str, Any]]:
    messages = [{"role": m.role, "content": m.content} for m in config.messages if m.content.strip()]
    for index, message in enumerate(messages):
        expected = "user" if index % 2 == 0 else "assistant"
        if message["role"] != expected:
            raise NodestroError("config.examples_order", "Example messages must start with the user and alternate")
    if messages and messages[-1]["role"] != "assistant":
        raise NodestroError("config.examples_end", "Example messages must end with an answer of the assistant")
    return messages


class AgentBuilder:
    """Builds agents from configurations against the tool library, knowledge and memory."""

    def __init__(self, tools: ToolLibrary, knowledge: KnowledgeBase, memory_spaces: MemorySpaces) -> None:
        self.tools = tools
        self.knowledge = knowledge
        self.memory_spaces = memory_spaces
        #: The capabilities declared in a provider's settings (OpenAI-compatible endpoints), for validation.
        self.declared: Callable[[str], dict[str, Any]] = lambda _provider: {}

    def capabilities(self, model: str) -> ModelCapabilities:
        """What ``model`` supports (Orkexa's values plus declared ones); all unknown for invalid names."""
        try:
            provider = provider_of(check_model(model))
            return Connection(provider=provider, capabilities=self.declared(provider)).model_capabilities(model)
        except (NodestroError, ValueError):
            return ModelCapabilities()

    def build(
        self,
        config: AgentConfig,
        *,
        connection: Connection,
        name: str | None = None,
        usage_tracker: UsageTracker | None = None,
        run_memory: Callable[[], MemoryBackend] | None = None,
        hooks: dict[str, Any] | None = None,
        extra_context: list[ContentBlock] | None = None,
        sandbox: Sandbox | None = None,
        extra_tools: list[Tool] | None = None,
    ) -> BuiltAgent:
        """Build the agent; raises ``ValueError``/``TypeError``/``KeyError`` for invalid settings.

        ``connection`` belongs to the provider of ``config.model`` (credentials, endpoint, declared
        capabilities). ``sandbox`` runs custom and generated tools, e.g. in the workflow's Python
        environment. ``extra_tools`` are tools of the run (the input files tool, knowledge proposals).
        """
        model = check_model(config.model)
        provider = provider_of(model)
        if connection.provider != provider:
            raise NodestroError(
                "config.provider_mismatch",
                f"The connection of {connection.provider} cannot run a model of {provider}",
                provider=provider,
            )
        options = _options(config, provider, connection)
        unknown = [tool for tool in config.tools if tool not in self.tools]
        if unknown:
            raise NodestroError(
                "config.unknown_tools", f"Unknown tools: {', '.join(unknown)}", names=", ".join(unknown)
            )
        tools = [self.tools.tool(tool_name, sandbox=sandbox) for tool_name in dict.fromkeys(config.tools)]
        taken = {tool.name for tool in tools}
        tools += [tool for tool in extra_tools or [] if tool.name not in taken]
        server_tools = []
        for key in dict.fromkeys(config.server_tools):
            if key not in SERVER_TOOLS:
                raise NodestroError("config.unknown_server_tool", f"Unknown server tool: {key}", name=key)
            server_tools.append(SERVER_TOOLS[key].definition)

        memory: MemoryBackend | None = None
        if config.memory.mode == "persistent":
            if not config.memory.space:
                raise NodestroError("config.memory_space_missing", "Persistent memory needs a memory space")
            memory = self.memory_spaces.open(config.memory.space)
        elif config.memory.mode == "run":
            memory = EphemeralMemory() if run_memory is None else run_memory()

        files, index = self.knowledge.views(config.knowledge)
        context_blocks: list[ContentBlock] = []
        if config.knowledge.mode == "context":
            for file_id in config.knowledge.file_ids:
                if file_id in self.knowledge.store:
                    context_blocks.append(self.knowledge.store.block(file_id, citations=config.knowledge.citations))

        context_blocks.extend(extra_context or [])
        messages = _messages(config)
        if context_blocks:
            # Background knowledge goes first, so it is part of the cached prefix.
            messages = [
                {
                    "role": "user",
                    "content": [*context_blocks, {"type": "text", "text": "Background material for the next tasks."}],
                },
                {"role": "assistant", "content": "Understood. I will use this material."},
                *messages,
            ]

        tool_choice = _tool_choice(config)

        agent: Agent[Any] = Agent(
            name=name or config.name,
            model=model_id(model),
            system_prompt=config.system_prompt.strip() or "You are a helpful assistant.",
            messages=[Message.model_validate(message) for message in messages],
            tools=tools,
            server_tools=server_tools,
            memory=memory,
            files=files,
            index=index,
            output_format=output_model(config.output),
            effort=config.effort,
            tool_choice=tool_choice,
            max_tokens=config.max_tokens,
            max_tool_rounds=config.max_tool_rounds,
            max_parallel_tools=config.max_parallel_tools,
            raise_tool_errors=config.raise_tool_errors,
            stream=True,
            on_refusal=config.on_refusal,
            options=options,
            max_retries=MAX_RETRIES,
            usage_tracker=usage_tracker,
            **connection.agent_kwargs(),
            **(hooks or {}),
        )
        _check_context(agent, context_blocks)
        names = {*agent.tools, *(str(item.get("name")) for item in server_tools)}
        if memory is not None:
            names.add(MEMORY_TOOL_NAME)
        if config.tool_choice.type == "tool" and config.tool_choice.name not in names:
            raise NodestroError(
                "config.tool_choice_unknown",
                f"Tool choice names a tool the agent does not have: {config.tool_choice.name}",
                name=config.tool_choice.name,
            )
        return BuiltAgent(agent=agent, context_blocks=context_blocks, memory=memory)

    def validate(self, config: AgentConfig) -> list[ConfigIssue]:
        """Problems the inspector shows; uses the library's own model rules."""
        issues: list[ConfigIssue] = []

        def issue(code: str, message: str, *, level: str = "error", field: str | None = None, **params: Any) -> None:
            issues.append(ConfigIssue(level=level, field=field, code=code, params=params, message=message))

        try:
            self.build(config, connection=_validation_connection(self.declared, config))
        except NodestroError as exc:
            field = "tools" if exc.code in ("config.unknown_tools", "config.unknown_server_tool") else None
            issue(exc.code, str(exc), field=field, **exc.params)
        except KeyError as exc:
            found = library_problem(exc)
            issue(found.code, found.message, field="tools", **found.params)
        except (ValueError, TypeError) as exc:
            found = library_problem(exc)
            issue(found.code, found.message, **found.params)
        web = {"web_search", "web_fetch"} & set(config.server_tools)
        if web and "code_execution" in config.server_tools:
            issue(
                "config.code_execution_with_web",
                "Do not combine code execution with web search or web fetch",
                level="warning",
                field="server_tools",
            )
        if config.output.mode == "structured" and config.knowledge.citations:
            issue(
                "config.citations_structured",
                "Citations cannot be combined with a structured output format",
                field="knowledge",
            )
        if config.memory.mode != "none" and MEMORY_TOOL_NAME in config.tools:
            issue("config.memory_name_reserved", "The name 'memory' is reserved for the memory tool", field="tools")
        if config.quality.enabled and not config.quality.criteria:
            issue("config.quality_no_criteria", "The quality check needs at least one criterion", field="quality")
        if "{{" not in config.task_template and config.task_template.strip():
            issue(
                "config.inputs_appended",
                "The predecessors' outputs are appended below the task text",
                level="info",
                field="task_template",
            )
        return issues


class _NeverCalled:
    """A client stand-in for validation; building an agent makes no request."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("validation must not call the API")


def _validation_connection(declared: Callable[[str], dict[str, Any]], config: AgentConfig) -> Connection:
    """The agent's connection with a client that is never called: no key needed, no request made.

    The declared capabilities of the provider's settings count, so the inspector shows what a run
    would refuse.
    """
    provider = provider_of(check_model(config.model))
    return Connection(provider=provider, capabilities=declared(provider), client=_NeverCalled())


def _check_context(agent: Agent[Any], blocks: list[ContentBlock]) -> None:
    """Documents and images placed into the context need the model's media input (checked before a run)."""
    caps = agent.capabilities
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        source = block.get("source") if isinstance(block, dict) else None
        if kind == "image":
            caps.require("image_input", provider=agent.provider, model=agent.model)
        elif kind == "document" and not (isinstance(source, dict) and source.get("type") == "text"):
            caps.require("document_input", provider=agent.provider, model=agent.model)
