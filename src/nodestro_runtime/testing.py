"""Controlled test adapters: scripted model answers and a stand-in for uv, never calling a paid API.

They are part of the public runtime so that every Nodestro component (API, studio, examples,
SDK tests) simulates models the same way. Nothing here is used outside tests.

- :class:`ScriptedClient` replaces a provider's SDK client: each request is answered from the
  script entry whose key occurs in the request's system prompt (``Workspace(client_factory=...)``).
- :func:`text`, :func:`tool_use`, :func:`parsed` build the answers.
- :class:`AnswerAllClient` answers every request without a script (every example workflow runs with it).
- :class:`FakeUv` replaces uv for tools with Python libraries (``Workspace(python_runner=...)``).
- :class:`ContainerDouble` stands in for Docker in tests of flows that need an isolating sandbox.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from copy import deepcopy
from pathlib import Path
import re
import threading
import types
from types import SimpleNamespace
from typing import Any, Literal, Union, get_args, get_origin

from anthropic.types import Message
from orkexa.dynamic import DockerSandbox, SubprocessSandbox, ValidationResult
from pydantic import BaseModel


def message(
    content: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-sonnet-5-5",
    usage: dict[str, Any] | None = None,
) -> Message:
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage or {"input_tokens": 1000, "output_tokens": 200},
        }
    )


def text(value: str, **kwargs: Any) -> Message:
    return message([{"type": "text", "text": value}], **kwargs)


def tool_use(name: str, payload: dict[str, Any], *, call_id: str = "toolu_1", **kwargs: Any) -> Message:
    return message(
        [{"type": "tool_use", "id": call_id, "name": name, "input": payload}], stop_reason="tool_use", **kwargs
    )


class Parsed:
    """Stand-in for the SDK's ParsedMessage."""

    def __init__(self, base: Message, parsed_output: Any) -> None:
        self._message = base
        self.parsed_output = parsed_output

    def __getattr__(self, name: str) -> Any:
        return getattr(self._message, name)


def parsed(output_factory: Callable[[type[Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    """A response that builds ``parsed_output`` from the request's ``output_format`` model."""

    def respond(request: dict[str, Any]) -> Any:
        model = request["output_format"]
        return Parsed(text("{}"), output_factory(model))

    return respond


class _Stream:
    def __init__(self, response: Any) -> None:
        self.response = response

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Iterator[SimpleNamespace]:
        for block in self.response.content:
            if block.type == "text":
                yield SimpleNamespace(type="text", text=block.text)

    def get_final_message(self) -> Any:
        return self.response


class ScriptedMessages:
    """Answers each request from the script of the first key found in its system prompt."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {key: list(values) for key, values in script.items()}
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def _next(self, request: dict[str, Any]) -> Any:
        with self._lock:
            self.calls.append(deepcopy({k: v for k, v in request.items() if k != "output_format"}))
            system = str(request.get("system", ""))
            for key, responses in self.script.items():
                if key in system:
                    if not responses:
                        raise AssertionError(f"No scripted response left for {key!r}")
                    response = responses.pop(0)
                    break
            else:
                raise AssertionError(f"No script for system prompt {system[:80]!r}")
        return response(request) if callable(response) else response

    def create(self, **request: Any) -> Any:
        return self._next(request)

    def parse(self, **request: Any) -> Any:
        return self._next(request)

    def stream(self, **request: Any) -> _Stream:
        return _Stream(self._next(request))

    def count_tokens(self, **request: Any) -> Any:
        return SimpleNamespace(input_tokens=100)


class ScriptedClient:
    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.messages = ScriptedMessages(script)
        self.beta = SimpleNamespace(messages=self.messages)


# ---- a client that answers every request ----------------------------------------------


def sample_value(annotation: Any, request: dict[str, Any]) -> Any:
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin in (Union, types.UnionType):
        return sample_value(next(arg for arg in get_args(annotation) if arg is not type(None)), request)
    if origin is list:
        return [sample_value(get_args(annotation)[0], request)]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return sample_instance(annotation, request)
    return {str: "Beispiel", int: 7, float: 1234.5, bool: True}.get(annotation, "Beispiel")


def sample_instance(model: type[BaseModel], request: dict[str, Any]) -> BaseModel:
    if model.__name__ == "_Verdict":
        # The judge passes every criterion it was given, so no revision is needed.
        names = re.findall(r"^\d+\. ([a-z_]+)(?: \(optional\))?:", _user_text(request), re.MULTILINE)
        return model(
            criteria=[{"name": name, "reasoning": "ok", "passed": True} for name in names],
            summary="ok",
            suggestions=[],
        )
    return model(**{name: sample_value(field.annotation, request) for name, field in model.model_fields.items()})


def _user_text(request: dict[str, Any]) -> str:
    parts = []
    for message in request.get("messages", []):
        content = message["content"]
        if isinstance(content, str):
            parts.append(content)
        else:
            parts.extend(block.get("text", "") for block in content if isinstance(block, dict))
    return "\n".join(parts)


class AnswerAll:
    """Answers every request: text for text agents, a filled model for structured output, and every
    quality check as passed (used to run every example workflow without a script)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def _answer(self, request: dict[str, Any]) -> Any:
        with self._lock:
            self.calls.append({key: value for key, value in request.items() if key != "output_format"})
        model = request.get("output_format")
        if model is not None:
            return Parsed(text("{}"), sample_instance(model, request))
        # Sample answers are test data; tests of other components assert this exact text.
        return text("Ergebnis des Agenten.")

    def create(self, **request: Any) -> Any:
        return self._answer(request)

    parse = create

    def stream(self, **request: Any) -> Any:
        return _Stream(self._answer(request))

    def count_tokens(self, **request: Any) -> Any:
        return types.SimpleNamespace(input_tokens=100)


class AnswerAllClient:
    def __init__(self) -> None:
        self.messages = AnswerAll()
        self.beta = types.SimpleNamespace(messages=self.messages)


# ---- a stand-in for uv ------------------------------------------------------------------


class FakeUv:
    """Resolves from a small package index and installs packages as plain modules.

    ``index`` maps a package name to its versions, newest last; each version has the
    packages it needs and the module source it installs. Environments are real virtual
    environments (``python -m venv``) that see the test interpreter's packages (pydantic),
    so the sandbox runs real code in them.
    """

    def __init__(self, index: dict[str, list[tuple[str, list[str], str]]]) -> None:
        self.index = index
        self.binary: str | None = "fake-uv"
        self.compiles: list[list[str]] = []
        self.syncs = 0
        self.fail_sync: str | None = None
        #: Packages without a wheel: resolving them with ``builds=False`` fails like ``uv --no-build``.
        self.source_only: set[str] = set()
        self.builds: list[bool] = []

    def version(self) -> str | None:
        return "0.0-fake" if self.binary else None

    def compile(
        self,
        requirements: list[str],
        *,
        python: str,
        previous: str,
        log: Callable[[str], None],
        builds: bool = True,
    ) -> str:
        import re

        from packaging.specifiers import SpecifierSet

        from .python_envs import UvError, parse_lock

        self.compiles.append(list(requirements))
        self.builds.append(builds)
        pins = {item.key: item.version for item in parse_lock(previous)}
        chosen: dict[str, tuple[str, list[str]]] = {}
        via: dict[str, list[str]] = {}
        queue = [(line, "-r requirements.in") for line in requirements]
        while queue:
            line, source = queue.pop(0)
            match = re.match(r"([A-Za-z0-9._-]+)(?:\[[^\]]*\])?(.*)", line.split(";")[0].strip())
            assert match is not None
            name, spec = match.group(1).lower().replace("_", "-"), match.group(2).strip()
            via.setdefault(name, []).append(source)
            if name in ("pydantic", "typing-extensions"):
                chosen.setdefault(name, ("2.0.0", []))
                continue
            if name not in self.index:
                raise UvError(f"No solution found: Because {name} was not found in the package registry")
            if not builds and name in self.source_only:
                raise UvError(
                    f"No solution found: hint: Wheels are required for `{name}` because building from source "
                    "is disabled for all packages (i.e., with `--no-build`)"
                )
            versions = [v for v in self.index[name] if v[0] in SpecifierSet(spec)]
            if name in chosen:
                if chosen[name][0] not in SpecifierSet(spec):
                    raise UvError(
                        f"No solution found: you require {name}{spec} and {name}=={chosen[name][0]}, unsatisfiable"
                    )
                continue
            if not versions:
                raise UvError(f"No solution found: there is no version of {name}{spec} and you require {name}{spec}")
            pinned = [v for v in versions if v[0] == pins.get(name)]
            version, needs, _module = (pinned or versions)[-1]
            chosen[name] = (version, needs)
            queue += [(need, name) for need in needs]
        lines = []
        for name in sorted(chosen):
            lines.append(f"{name}=={chosen[name][0]}")
            sources = via[name]
            if len(sources) == 1:
                lines.append(f"    # via {sources[0]}")
            else:
                lines.append("    # via")
                lines += [f"    #   {item}" for item in sources]
        return "\n".join(lines) + "\n"

    def create(self, target: Path, *, log: Callable[[str], None]) -> None:
        import subprocess
        import sys
        import sysconfig

        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(target)], check=True)
        site = next((target / "lib").glob("python*/site-packages"))
        (site / "_host.pth").write_text(sysconfig.get_paths()["purelib"] + "\n", encoding="utf-8")
        log(f"created {target}")

    def sync(self, interpreter: Path, lock: str, *, log: Callable[[str], None]) -> None:
        from .python_envs import UvError, parse_lock

        self.syncs += 1
        if self.fail_sync:
            raise UvError(self.fail_sync)
        site = next((interpreter.parent.parent / "lib").glob("python*/site-packages"))
        for package in parse_lock(lock):
            for version, _needs, module in self.index.get(package.key, []):
                if version == package.version and module:
                    name, _, source = module.partition("\n")
                    (site / f"{name}.py").write_text(source, encoding="utf-8")
            log(f" + {package.name}=={package.version}")

    def prune_cache(self, *, log: Callable[[str], None]) -> None:
        log("pruned")


class ContainerDouble(DockerSandbox):
    """**Tests only, not isolated.** Counts as a container for the sandbox policy but runs the code in a host
    subprocess, so tests can exercise flows that need Docker (tool generation, drafts, AI-generated tools)
    on machines without Docker. Never offered by a setting; the settings create a real ``DockerSandbox``.

    ``calls`` records what ran (``("validate", name)`` or ``("call", name)``)."""

    def __init__(self, *, timeout: float = 15.0) -> None:
        super().__init__(timeout=timeout)
        self.host = SubprocessSandbox(timeout=timeout, acknowledge_unsafe=True)
        self.calls: list[tuple[str, str]] = []

    def validate(self, code: str, tests: str, function_name: str) -> ValidationResult:
        self.calls.append(("validate", function_name))
        return self.host.validate(code, tests, function_name)

    def call(self, code: str, function_name: str, payload: dict[str, Any]) -> str:
        self.calls.append(("call", function_name))
        return self.host.call(code, function_name, payload)
