"""Local MCP servers over stdio, only in an isolated container (WP-8, D-5, ADR-0006).

A local server is third-party code of class ``untrusted``. It runs with ``python -m <module>`` in a container of
Nodestro's sandbox image for its approved, hash-pinned packages (:mod:`nodestro_runtime.container_images`): no
network, read-only root file system, no capabilities, user ``nobody``, memory/CPU/process limits, never pulled from a
registry. There is no host fallback: without the Docker sandbox a stdio server is refused with
``sandbox.isolation_required``.

Before every container start the daemon must prove the sandbox for that image and exactly this container shape, as
for a tool call (:func:`prove`): Orkexa's preflight (``docker info``: limits, seccomp, cgroups) and its in-container
self-test of the image (:meth:`orkexa.dynamic.DockerSandbox.self_test`, remembered per daemon, image id and shape).
The container then runs the image by the id the self-test checked, never by its tag, so a tag moved afterwards is not
run unchecked. A daemon that fails either check (cgroup v1, limits it does not apply) starts nothing.

Between the MCP SDK's stdio transport and ``docker run`` sits a small relay (``python -m
nodestro_runtime.mcp.stdio``, Nodestro's own code). The SDK buffers a line without limit; the relay ends the
connection when the server writes a line longer than :data:`MAX_LINE_BYTES`, so an untrusted server cannot exhaust
Nodestro's memory. The server's stderr is discarded (it may echo arguments or data and is never logged). On exit the
relay and the hub both remove the container.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import contextlib
from dataclasses import dataclass, field
import os
import re
import signal
import subprocess
import sys
import threading
from typing import Any
import uuid

from orkexa.dynamic import SandboxError, SandboxProbe

from ..container_images import MCP_OWNER, StreamedSandbox, sandbox_run_args
from ..docker_status import MCP_LIFETIME, reason_of
from ..problems import NodestroError

#: The longest JSON-RPC line a stdio server may write (a tool result is cut to ``max_output_chars`` later anyway).
MAX_LINE_BYTES = 8 * 1024 * 1024
#: Processes and CPUs of a stdio server's container.
PIDS_LIMIT = 64
CPUS = 1.0
#: The container name prefix; the sandbox gateway accepts only names of the sandbox's pattern.
NAME_PREFIX = "nodestro-sandbox-mcp-"
#: The deadline label of a stdio server's container (it lives as long as its connection; the tool containers' reaper
#: never removes containers of an MCP owner, :func:`~nodestro_runtime.docker_status.mcp_owner`, the MCP reaper
#: :meth:`~nodestro_runtime.docker_status.DockerMonitor.reap_mcp_once` those past it or left behind).
LIFETIME = MCP_LIFETIME
#: Seconds the self-test container of :func:`prove` may take (Orkexa gives it at least 60).
SELF_TEST_TIMEOUT = 60.0
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class StdioLaunch:
    """How the hub starts one connection to a stdio server: the relay command, its environment, the container."""

    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)
    container: str = ""
    docker: str = "docker"


#: Starts the relay with ``python -I`` (no working directory or PYTHONPATH on the import path) and Nodestro's own
#: import path from ``NODESTRO_RELAY_PATH``.
_RELAY_BOOTSTRAP = (
    "import os, sys\n"
    "sys.path[:0] = [p for p in os.environ.get('NODESTRO_RELAY_PATH', '').split(os.pathsep) if p]\n"
    "from nodestro_runtime.mcp.stdio import main\n"
    "sys.exit(main())\n"
)


def container_name() -> str:
    return f"{NAME_PREFIX}{uuid.uuid4().hex[:12]}"


def proof_sandbox(
    image: str,
    *,
    memory_mb: int,
    docker: str = "docker",
    docker_env: Mapping[str, str] | None = None,
    owner: str = MCP_OWNER,
) -> StreamedSandbox:
    """The sandbox whose self-test proves the daemon for a stdio server's container: the same docker CLI and
    environment, owner and limits (memory, :data:`CPUS`, :data:`PIDS_LIMIT`) as :func:`launch`, hence the same
    container shape (the deadline aside)."""
    return StreamedSandbox(
        image=image,
        timeout=SELF_TEST_TIMEOUT,
        memory_mb=memory_mb,
        cpus=CPUS,
        pids_limit=PIDS_LIMIT,
        docker=docker,
        environ=dict(docker_env) if docker_env is not None else None,
        owner=owner,
    )


def prove(
    image: str,
    *,
    memory_mb: int,
    docker: str = "docker",
    docker_env: Mapping[str, str] | None = None,
    owner: str = MCP_OWNER,
    sandbox: Callable[..., Any] = proof_sandbox,
) -> str:
    """The id of ``image`` once the daemon passed Orkexa's preflight and the self-test of this image in the stdio
    container's shape; raises ``sandbox.isolation_required`` (with the Docker backend's ``reason``) otherwise.

    The preflight runs on every call (``docker info``); a passed self-test is remembered per daemon fingerprint,
    image id and shape (Orkexa), so a changed daemon or image is tested again."""
    checker = sandbox(image, memory_mb=memory_mb, docker=docker, docker_env=docker_env, owner=owner)
    try:
        probe: SandboxProbe = checker.self_test(image=image)
    except SandboxError as exc:
        probe = SandboxProbe("docker", False, "self-test", str(exc))
    image_id = str(dict(probe.facts).get("image_id", ""))
    if not probe.available or not _IMAGE_ID.fullmatch(image_id):
        reason = reason_of(probe.reason) if not probe.available else "docker-image"
        detail = probe.detail or "the self-test reported no image id"
        raise NodestroError(
            "sandbox.isolation_required",
            "A local MCP server runs only in a container on a Docker daemon that passed the sandbox's preflight and "
            f"self-test; this one did not ({reason}): {detail[:500]}",
            reason=reason,
        )
    return image_id


def launch(
    *,
    image: str,
    module: str,
    args: list[str],
    memory_mb: int,
    docker: str = "docker",
    docker_env: Mapping[str, str] | None = None,
    python: str = sys.executable,
    owner: str = MCP_OWNER,
) -> StdioLaunch:
    """The relay command that runs ``python -m module args`` in a sandbox container of ``image``.

    ``image`` is the id :func:`prove` returned (a tag is refused: only a checked image is run)."""
    if not _IMAGE_ID.fullmatch(image):
        raise NodestroError(
            "sandbox.isolation_required",
            "A local MCP server's container runs only a proven image by its id",
            reason="docker-image",
        )
    name = container_name()
    run = [
        *sandbox_run_args(
            docker,
            name,
            image,
            memory_mb=memory_mb,
            cpus=CPUS,
            pids_limit=PIDS_LIMIT,
            owner=owner,
            lifetime_s=LIFETIME,
        ),
        "python",
        "-I",
        "-B",
        "-m",
        module,
        *args,
    ]
    relay = [
        "-I",
        "-c",
        _RELAY_BOOTSTRAP,
        "--max-line",
        str(MAX_LINE_BYTES),
        "--container",
        name,
        "--docker",
        docker,
        "--",
        *run,
    ]
    # The relay needs Nodestro's import path (``-I`` drops PYTHONPATH and the working directory).
    env = dict(docker_env or {})
    env["NODESTRO_RELAY_PATH"] = os.pathsep.join(path for path in sys.path if path and os.path.isabs(path))
    return StdioLaunch(command=python, args=relay, env=env, container=name, docker=docker)


def remove_container(docker: str, name: str, env: Mapping[str, str] | None = None) -> None:
    """Remove the container if it still exists (quietly; ``--rm`` usually did it already)."""
    if not name:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [docker, "rm", "-f", name],
            capture_output=True,
            check=False,
            timeout=15,
            env=dict(env) if env is not None else None,
        )


# ---- the relay (runs as its own process) ---------------------------------------------------------------------


def relay(
    command: list[str],
    *,
    max_line: int,
    container: str,
    docker: str,
    stdin: Any = None,
    stdout: Any = None,
    on_exit: Callable[[], None] | None = None,
) -> int:
    """Copy stdin to the server and the server's stdout back, line length bounded; returns the exit code."""
    source: Any = stdin if stdin is not None else sys.stdin.buffer
    target: Any = stdout if stdout is not None else sys.stdout.buffer
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
    )
    assert process.stdin is not None and process.stdout is not None

    def cleanup() -> None:
        if process.poll() is None:
            process.kill()
        remove_container(docker, container, os.environ)
        if on_exit is not None:
            on_exit()

    def forward_stdin() -> None:
        assert process.stdin is not None
        try:
            while True:
                chunk = source.read1(65536)
                if not chunk:
                    break
                process.stdin.write(chunk)
                process.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()

    def stop(_signum: int, _frame: object) -> None:
        cleanup()
        os._exit(143)

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=forward_stdin, name="stdio-relay-in", daemon=True).start()
    pending = 0
    code = 0
    try:
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            newline = chunk.rfind(b"\n")
            pending = len(chunk) - newline - 1 if newline >= 0 else pending + len(chunk)
            if pending > max_line:
                code = 3  # the server wrote an overlong line: end the connection
                break
            target.write(chunk)
            target.flush()
    except (OSError, ValueError):
        code = 1
    finally:
        cleanup()
    if code == 0:
        code = process.wait()
    return code


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="nodestro_runtime.mcp.stdio", description="Relay for a local MCP server")
    parser.add_argument("--max-line", type=int, default=MAX_LINE_BYTES)
    parser.add_argument("--container", default="")
    parser.add_argument("--docker", default="docker")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    command = options.command[1:] if options.command[:1] == ["--"] else options.command
    if not command:
        parser.error("a command is required")
    return relay(command, max_line=options.max_line, container=options.container, docker=options.docker)


if __name__ == "__main__":
    sys.exit(main())
