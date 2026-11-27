"""Runs *inside* the sandbox container (N-22). Never imported in the host process.

Protocol: one JSON request on stdin, one JSON line on stdout. Request keys: ``mode`` (``describe`` or ``call``),
``entry`` (the entry point's ``module:attribute``), ``tool`` and ``payload`` (``call``). The bootstrap
(:data:`nodestro_runtime.extension_sandbox.BOOTSTRAP`) has written the package's files to ``<work>/pkg``, Orkexa's
runner modules and the contract module (:mod:`nodestro_runtime._extension_contract`) below ``<tmp>/nodestro``.

Only the standard library is used until the limits are applied; then the contract module is registered as
``nodestro_runtime.extensions``, ``nodestro_runtime.tool_spec`` and ``nodestro_runtime.tool_policy``, Orkexa's
``Tool`` as ``orkexa.Tool``, and the package is imported. Runs on Python 3.10+.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import traceback
import types
from typing import Any

_MAX_DIAGNOSTIC_CHARS = 4000


def _apply_limits() -> None:
    """No child processes, no core files, small files (the container's own limits apply as well)."""
    try:
        import resource
    except ImportError:  # pragma: no cover - not in a Linux container
        return
    for name, value in (("RLIMIT_NPROC", 0), ("RLIMIT_CORE", 0), ("RLIMIT_FSIZE", 1024 * 1024)):
        kind = getattr(resource, name, None)
        if kind is not None:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(kind, (value, value))


def _short(text: str) -> str:
    return text if len(text) <= _MAX_DIAGNOSTIC_CHARS else text[-_MAX_DIAGNOSTIC_CHARS:]


def _error(exc: BaseException) -> str:
    return _short("".join(traceback.format_exception_only(type(exc), exc)).strip())


def _prepare(root: str) -> None:
    """Make ``orkexa.Tool`` and the contract importable without the real packages' ``__init__``."""
    orkexa = types.ModuleType("orkexa")
    orkexa.__path__ = [os.path.join(root, "orkexa")]
    sys.modules["orkexa"] = orkexa
    from orkexa.errors import ToolResultError
    from orkexa.tools import Tool

    orkexa.Tool = Tool  # type: ignore[attr-defined]
    orkexa.ToolResultError = ToolResultError  # type: ignore[attr-defined]
    runtime = types.ModuleType("nodestro_runtime")
    runtime.__path__ = [os.path.join(root, "nodestro_runtime")]
    sys.modules["nodestro_runtime"] = runtime
    contract = importlib.import_module("nodestro_runtime._contract")
    for name in ("extensions", "tool_spec", "tool_policy"):
        sys.modules[f"nodestro_runtime.{name}"] = contract
        setattr(runtime, name, contract)


def _integration(entry: str) -> Any:
    module_name, _, attribute = entry.partition(":")
    value: Any = importlib.import_module(module_name)
    for part in attribute.split(".") if attribute else ():
        value = getattr(value, part)
    contract = sys.modules["nodestro_runtime.extensions"]
    if callable(value) and not isinstance(value, contract.Integration):
        value = value()
    if not isinstance(value, contract.Integration):
        raise TypeError(f"{entry} does not provide an Integration")
    return value


def _pairs(integration: Any) -> list[tuple[Any, Any]]:
    pairs = []
    for item in integration.tools or ():
        spec, tool = item if isinstance(item, tuple) and len(item) == 2 else (None, item)
        pairs.append((spec, tool))
    return pairs


def _stated(spec: Any) -> dict[str, Any]:
    """What a spec states that the host may use to raise the floor (capability) or lower the time limit."""
    if spec is None:
        return {}
    capability = getattr(spec, "capability", None)
    found: dict[str, Any] = {
        "access": getattr(capability, "access", None),
        "reach": getattr(capability, "reach", None),
    }
    timeout = getattr(spec, "timeout_seconds", None)
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        found["timeout_seconds"] = float(timeout)
    return {key: value for key, value in found.items() if isinstance(value, (str, float))}


def _describe(entry: str) -> dict[str, Any]:
    tools = []
    for spec, tool in _pairs(_integration(entry)):
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "stated": _stated(spec),
            }
        )
    return {"ok": True, "tools": tools}


def _call(entry: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    for _spec, tool in _pairs(_integration(entry)):
        if tool.name == name:
            result = tool.execute(payload)
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, default=str)
            return {"ok": True, "result": result}
    raise LookupError(f"the integration has no tool {name!r}")


def main() -> None:
    real_stdout = sys.stdout
    request = json.loads(sys.stdin.read())
    _apply_limits()
    root = request.pop("root")
    sys.path[:0] = [os.path.join(os.getcwd(), "pkg")]
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            _prepare(root)
            if request["mode"] == "describe":
                response = _describe(request["entry"])
            elif request["mode"] == "call":
                response = _call(request["entry"], request["tool"], request["payload"])
            else:
                raise ValueError(f"unknown mode {request['mode']!r}")
    except BaseException as exc:
        response = {"ok": False, "error": _error(exc)}
    response["output"] = _short(captured.getvalue())
    real_stdout.write("\n" + json.dumps(response) + "\n")
    real_stdout.flush()


if __name__ == "__main__":
    main()
