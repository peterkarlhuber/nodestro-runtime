"""Live reasoning of agents and built-in AI functions: what the provider transmits, passed on unchanged.

Orkexa reports a model's reasoning while it streams as two events (``orkexa.ReasoningStatus``: a reasoning phase
started or finished; ``orkexa.ReasoningDelta``: reasoning text the provider sent, of kind ``thinking`` or
``summary``). This module turns them into what Nodestro records and streams to the interface:

- :func:`reasoning_hook` gives ``on_event`` hook for ``orkexa.Agent`` that calls ``on_delta(text, kind)`` and
  ``on_status(state, kind, redacted)``; every other event is ignored.
- :class:`ReasoningTrace` accumulates them for a stored record (a run step, a test result, a chat answer).
- :func:`stream_info` says, per model, what the provider can transmit (``orkexa.ReasoningStream``), for the editor
  and the interface's explanation when no text arrives.

Nothing here generates or completes reasoning text: a provider that sends none leaves ``text`` empty, and the
interface shows the status only (and explains why, from :func:`stream_info`).

Showing the text is the interface's choice (a preference of the person, globally and per conversation); asking a
provider for it where it sends it only on request is the workspace setting ``reasoning_summaries`` (OpenAI reasoning
summaries, Gemini thought summaries; see :meth:`nodestro_runtime.providers.Connection.options`). Anthropic agents
keep their own ``thinking_display`` option.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from orkexa import ModelCapabilities, ReasoningDelta, ReasoningStatus
from pydantic import BaseModel, PrivateAttr

ReasoningKind = Literal["thinking", "summary"]
#: ``thinking``, ``summary``, ``none`` or ``unknown`` (see ``orkexa.ReasoningStream``).
ReasoningText = Literal["thinking", "summary", "none", "unknown"]


class ReasoningStreamInfo(BaseModel):
    """What a model's stream transmits of its reasoning, for the interface (``orkexa.ReasoningStream``)."""

    #: ``thinking`` (the model's reasoning text), ``summary`` (the provider's summary of it), ``none`` (no text: the
    #: model does not reason or the provider does not expose it) or ``unknown`` (depends on the server and model).
    text: ReasoningText = "unknown"
    #: Whether the stream marks a reasoning phase even when no text is transmitted.
    status: bool = False
    #: The provider option that asks for the text where the provider sends it only on request: ``reasoning_summary``
    #: (OpenAI), ``include_thoughts`` (Gemini), ``thinking_display`` (Anthropic, per agent); ``None`` otherwise.
    request_option: str | None = None
    #: Whether this workspace asks for it (the setting ``reasoning_summaries`` for ``reasoning_summary`` and
    #: ``include_thoughts``); ``None`` where no request is needed or possible.
    requested: bool | None = None
    #: Why no text is transmitted (``text == "none"``), in English.
    reason: str = ""


class ReasoningTrace(BaseModel):
    """The reasoning of one model call sequence as it arrived (a run step, an agent test, a chat answer)."""

    #: The transmitted text, joined as it arrived (summary parts and later phases separated by blank lines).
    text: str = ""
    #: The kind of the transmitted text (the last one seen); ``None`` before any text.
    kind: ReasoningKind | None = None
    #: Reasoning phases the stream marked (with or without text).
    phases: int = 0
    #: A phase was transmitted only encrypted or omitted (the model reasoned, there is no text to show).
    redacted: bool = False
    #: A phase is open right now.
    active: bool = False

    _new_phase: bool = PrivateAttr(default=False)

    def status(self, state: str, redacted: bool) -> None:
        if state == "started":
            self.phases += 1
            self.active = True
            self.redacted = self.redacted or redacted
            self._new_phase = True
        else:
            self.active = False

    def delta(self, text: str, kind: ReasoningKind) -> str:
        """Add ``text``; returns what was added: the text of a later phase starts a new paragraph."""
        if self.phases == 0:
            self.phases = 1  # text without a status event (a provider that only sends text)
            self.active = True
        if self._new_phase and self.text and not self.text.endswith("\n\n"):
            text = "\n\n" + text
        self._new_phase = False
        self.text += text
        self.kind = kind
        return text


def reasoning_hook(
    on_delta: Callable[[str, ReasoningKind], Any],
    on_status: Callable[[str, ReasoningKind | None, bool], Any] | None = None,
) -> Callable[[Any], None]:
    """An ``on_event`` hook for ``orkexa.Agent`` that passes on reasoning events only."""

    def on_event(event: Any) -> None:
        if isinstance(event, ReasoningDelta):
            if event.text:
                on_delta(event.text, event.kind)
        elif isinstance(event, ReasoningStatus) and on_status is not None:
            on_status(event.state, event.kind, event.redacted)

    return on_event


def trace_hook(trace: ReasoningTrace, also: Callable[[Any], Any] | None = None) -> Callable[[Any], None]:
    """An ``on_event`` hook that records reasoning into ``trace`` (and calls ``also`` with every event)."""
    record = reasoning_hook(trace.delta, lambda state, _kind, redacted: trace.status(state, redacted))

    def on_event(event: Any) -> None:
        record(event)
        if also is not None:
            also(event)

    return on_event


def stream_info(
    adapter: Any, model: str, capabilities: ModelCapabilities | None, *, summaries: bool
) -> ReasoningStreamInfo:
    """What ``model`` transmits of its reasoning through ``adapter`` (an Orkexa provider), with the workspace's
    ``reasoning_summaries`` setting (``summaries``) applied to the options it controls."""
    declared = adapter.reasoning_stream(model, capabilities)
    requested: bool | None = None
    if declared.request_option in ("reasoning_summary", "include_thoughts") and declared.text != "none":
        requested = summaries
    return ReasoningStreamInfo(
        text=declared.text,
        status=declared.status,
        request_option=declared.request_option,
        requested=requested,
        reason=declared.reason,
    )


def model_reasoning(builder: Any, model: str, *, summaries: bool) -> ReasoningStreamInfo | None:
    """What ``model`` transmits of its reasoning in this workspace (``builder``: the workspace's ``AgentBuilder``, for
    the declared and reported capabilities), with the workspace setting ``reasoning_summaries``; ``None`` for an
    invalid model id. Free and local: nothing is sent to the provider."""
    from .problems import NodestroError
    from .providers import Connection, check_model, provider_of

    try:
        provider = provider_of(check_model(model))
        reported = builder.reported if provider == "ollama" else None
        connection = Connection(
            provider=provider,
            capabilities=builder.declared(provider),
            reported=reported,
            reasoning_summaries=summaries,
        )
        info: ReasoningStreamInfo = connection.reasoning_stream(model)
        return info
    except (NodestroError, ValueError):
        return None
