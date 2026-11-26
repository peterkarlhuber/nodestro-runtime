"""Offline per agent and per conversation (N-09): the strict offline mode's rules for one agent or one conversation.

The strict offline mode (D12, ``Settings.strict_offline``) holds for the whole installation. ``AgentConfig.offline``
and ``Conversation.offline`` apply the same rules to one agent or to the answers of one conversation, so another
agent of the same workspace still uses cloud models and internet tools. Both default to off; with both off nothing
here changes a run.

What an offline agent or conversation may not use:

- **Cloud models.** Every provider but the local model server (``ollama``) counts as a cloud model, as in the strict
  offline mode (Nodestro cannot tell where an OpenAI-compatible endpoint sends data), and the local model server must
  be on this computer or in Nodestro Local Docker's network (:func:`is_local_endpoint`). The judge of a quality check
  and the orchestrator's decisions are model calls like every other.
- **Server tools** of a provider (they run at the provider).
- **Tools with external reach** (:func:`tool_state`): a built-in tool whose spec is not ``offline``, an integration's
  tool without a spec, an MCP tool of a server that is neither ``stdio`` nor on this computer, and an own or
  generated tool whose capability reaches outside (an unstated capability counts as external; a tool in a sandbox
  without network is internal). The run's own tools (input files, run memory, knowledge search, skills) stay.
- **Shared memory** (:func:`shared_memory`): a persistent memory space is read by every agent of the space, cloud
  agents too, and a knowledge proposal becomes knowledge any agent searches. An offline agent gets run memory only and
  no knowledge proposals (``offline.shared_memory``), as an answer in a conversation.

Where it is enforced, at call time (``engine.RunManager``):

- every model connection of a step (agent turns, retries, the quality judge, an orchestrator's decision) goes through
  :func:`check_connection`;
- every tool call is refused before it runs (``offline.tool_unavailable``), before any approval question;
- an agent with server tools does not start (``offline.server_tools``), nor one with a persistent memory space or
  knowledge proposals (``offline.shared_memory``).

An offline conversation makes every run below its answers offline (sub-workflows and team runs too), and the
conversation's flag is read at every call, so switching it on applies at the next call of a running answer. Run chat
about a run of an offline agent or conversation uses local models only. The agent test applies the same rules.

Before a run, the agent's validation reports the same as issues (:func:`config_issues`): a cloud model or judge and
server tools are errors, and each tool that is not available offline is a warning (``offline.tool_unavailable`` with
the reason), so the agent keeps its tools and the run tells the model that the tool is not available offline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from .mcp_access import is_mcp_tool
from .problems import NodestroError
from .providers import provider_of
from .tool_policy import NO_NETWORK
from .tool_spec import host_tool_spec

if TYPE_CHECKING:
    from .providers import Connection
    from .schemas import AgentConfig

#: Who made a call offline: the agent's own setting or the conversation it answers in.
OfflineScope = Literal["agent", "conversation"]
#: Why a tool is not available offline.
OfflineReason = Literal["network", "remote_mcp", "http_mcp", "external_reach", "unknown"]
#: The provider of local models, the only one an offline agent may use.
LOCAL_PROVIDER = "ollama"

_REASON_TEXTS: dict[str, str] = {
    "network": "it needs the internet",
    "remote_mcp": "its MCP server is not on this computer",
    "http_mcp": "its MCP server is reached over HTTP and may reach the internet itself",
    "external_reach": "it may reach outside this computer",
    "unknown": "it is not known whether it needs the network",
}


class OfflineToolState(BaseModel):
    """Whether one tool can be used offline, and why not."""

    name: str
    available: bool
    reason: OfflineReason | None = None

    @property
    def text(self) -> str:
        """``not available offline`` and the reason, for the model and the issue's message."""
        if self.available:
            return f"{self.name} is available offline"
        return f"{self.name} is not available offline: {_REASON_TEXTS[self.reason or 'unknown']}"


#: The host name of the Ollama container on Nodestro Local Docker's internal network (``local-container`` profile).
DOCKER_OLLAMA_HOST = "ollama"


def is_local_endpoint(url: str, *, docker: bool = False) -> bool:
    """Whether a model server address is on this computer: ``localhost`` or a loopback IP address (``127.0.0.0/8``,
    ``::1``). A host name that merely starts with ``127.`` (``127.0.0.1.nip.io``) is a DNS name anywhere and does not
    count. ``docker`` (the ``local-container`` profile, Nodestro Local Docker) also accepts the Ollama container's
    host name on the internal network, ``ollama``; nowhere else is that name local."""
    import ipaddress
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if host == "localhost" or (docker and host == DOCKER_OLLAMA_HOST):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def remote_model_server(connection: Connection, *, docker: bool = False) -> bool:
    """Whether ``connection`` (a local model server's) has an address that is not on this computer
    (:func:`is_local_endpoint`). A connection without an address (an injected client) counts as local, as in
    :func:`check_connection`. N-08 treats such a server as a recipient outside the computer."""
    return bool(connection.base_url) and not is_local_endpoint(str(connection.base_url), docker=docker)


def mcp_available_offline(server: Any) -> OfflineReason | None:
    """``None`` when an MCP server can be used offline, otherwise why not. Only a local ``stdio`` server counts: it
    runs in a sandbox container without network. An HTTP server is refused even on this computer, because the
    server itself may reach the internet (the strict offline gate also counts only ``stdio`` servers as local)."""
    if getattr(server, "transport", "http") == "stdio":
        return None
    return "http_mcp" if is_local_endpoint(str(getattr(server, "url", "") or "")) else "remote_mcp"


def _unavailable(name: str, reason: OfflineReason) -> OfflineToolState:
    return OfflineToolState(name=name, available=False, reason=reason)


def tool_state(library: Any, name: str, *, sandbox: Any = None, at_call: bool = False) -> OfflineToolState:
    """Whether the library tool ``name`` can be used offline (see the module documentation).

    ``library`` is a :class:`~nodestro_runtime.tool_library.ToolLibrary`. Fails closed: a tool that cannot be found
    counts as not available (``unknown``).

    An own or generated tool counts as internal only because its code runs without network (the reach is derived
    from the sandbox). ``at_call``: judged by the sandbox the call really runs in, with ``sandbox`` (the workflow's
    Python environment, ``None``: the library's own), not by the library's default: a workflow's environment can be a
    host subprocess with network."""
    if is_mcp_tool(name):
        mcp = getattr(library, "mcp", None)
        find = getattr(mcp, "find", None)
        found = find(name) if find is not None else None
        if found is None:
            return _unavailable(name, "unknown")
        reason = mcp_available_offline(found[0])
        return OfflineToolState(name=name, available=True) if reason is None else _unavailable(name, reason)
    if name in getattr(library, "builtin", {}):
        spec = library.specs.get(name) or host_tool_spec(name)
        if spec is None:
            return _unavailable(name, "unknown")
        return OfflineToolState(name=name, available=True) if spec.offline else _unavailable(name, "network")
    try:
        capability = library.get(name).capability
    except (KeyError, ValueError, AttributeError):
        return _unavailable(name, "unknown")
    if capability.reach != "internal":
        return _unavailable(name, "external_reach")
    if at_call and capability.source == "derived":
        isolation = getattr(library, "call_isolation", lambda _name, _sandbox: None)(name, sandbox)
        if isolation not in NO_NETWORK:
            return _unavailable(name, "external_reach")
    return OfflineToolState(name=name, available=True)


def tool_states(library: Any, names: list[str]) -> list[OfflineToolState]:
    """:func:`tool_state` of each name, once each, in order."""
    return [tool_state(library, name) for name in dict.fromkeys(names)]


def cloud_model_error(model: str, scope: OfflineScope) -> NodestroError:
    """The refusal of a cloud model for an offline agent or conversation."""
    return NodestroError(
        "offline.cloud_model",
        f"{model} needs the internet; this {scope} is offline and uses local models only",
        model=model,
        scope=scope,
    )


def is_cloud(model: str) -> bool:
    """Whether ``model`` is not a model of the local model server (an invalid name counts as cloud)."""
    try:
        return provider_of(model) != LOCAL_PROVIDER
    except NodestroError:
        return True


def check_provider(provider: str, scope: OfflineScope) -> None:
    """Refuse a cloud provider before its connection is even resolved."""
    if provider != LOCAL_PROVIDER:
        raise cloud_model_error(provider, scope)


def check_connection(connection: Connection, scope: OfflineScope, *, docker: bool = False) -> Connection:
    """``connection``, unless it is a cloud provider or a local model server that is not on this computer
    (:func:`is_local_endpoint`; ``docker``: the ``local-container`` profile)."""
    check_provider(connection.provider, scope)
    if connection.base_url and not is_local_endpoint(connection.base_url, docker=docker):
        raise NodestroError(
            "offline.remote_endpoint",
            f"The local model server is not on this computer; this {scope} is offline and uses local servers only",
            scope=scope,
        )
    return connection


def server_tools_error(names: list[str], scope: OfflineScope) -> NodestroError:
    return NodestroError(
        "offline.server_tools",
        f"Server tools run at a cloud provider; this {scope} is offline and cannot use them",
        names=", ".join(names),
        scope=scope,
    )


def shared_memory(config: AgentConfig) -> list[str]:
    """What of ``config`` keeps content beyond its run where other agents (cloud agents too) read it: a persistent
    memory space (``memory``) and knowledge proposals (``knowledge_proposals``). An offline agent may use neither."""
    found = []
    if config.memory.mode == "persistent":
        found.append("memory")
    if config.knowledge_proposals.enabled:
        found.append("knowledge_proposals")
    return found


def shared_memory_error(fields: list[str], scope: OfflineScope) -> NodestroError:
    return NodestroError(
        "offline.shared_memory",
        f"A persistent memory space and knowledge proposals are read by other agents, cloud agents too; this {scope} "
        "is offline and uses run memory only",
        fields=", ".join(fields),
        scope=scope,
    )


def config_issues(config: AgentConfig, library: Any) -> list[dict[str, Any]]:
    """What an offline agent cannot use, as issue fields for :class:`~nodestro_runtime.agent_builder.ConfigIssue`
    (``code``, ``message``, ``level``, ``field``, ``params``); empty while ``config.offline`` is off."""
    if not config.offline:
        return []
    found: list[dict[str, Any]] = []

    def issue(code: str, message: str, *, level: str, field: str, **params: Any) -> None:
        found.append({"code": code, "message": message, "level": level, "field": field, "params": params})

    for field, model in (("model", config.model), ("quality", config.quality.judge_model)):
        if field == "quality" and not config.quality.enabled:
            continue
        if is_cloud(model):
            issue(
                "offline.cloud_model",
                str(cloud_model_error(model, "agent")),
                level="error",
                field=field,
                model=model,
                scope="agent",
            )
    if config.server_tools:
        names = ", ".join(dict.fromkeys(config.server_tools))
        issue(
            "offline.server_tools",
            str(server_tools_error([names], "agent")),
            level="error",
            field="server_tools",
            names=names,
            scope="agent",
        )
    for field in shared_memory(config):
        issue(
            "offline.shared_memory",
            str(shared_memory_error([field], "agent")),
            level="error",
            field=field,
            fields=field,
            scope="agent",
        )
    for state in tool_states(library, list(config.tools)):
        if not state.available:
            issue(
                "offline.tool_unavailable",
                state.text,
                level="warning",
                field="tools",
                name=state.name,
                reason=state.reason,
            )
    return found
