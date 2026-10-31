"""Local MCP servers over stdio, only in an isolated container (WP-8, D-5, ADR-0006).

A local server is third-party code of class ``untrusted``. It runs with ``python -m <module>`` in a container of
Nodestro's sandbox image for its approved, hash-pinned packages (:mod:`nodestro_runtime.container_images`): no
network, read-only root file system, no capabilities, user ``nobody``, memory/CPU/process limits, never pulled from a
registry. There is no host fallback: without the Docker sandbox a stdio server is refused with
``sandbox.isolation_required``.

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
import signal
import subprocess
import sys
import threading
from typing import Any
import uuid

from ..container_images import sandbox_run_args

#: The longest JSON-RPC line a stdio server may write (a tool result is cut to ``max_output_chars`` later anyway).
MAX_LINE_BYTES = 8 * 1024 * 1024
#: Processes and CPUs of a stdio server's container.
PIDS_LIMIT = 64
CPUS = 1.0
#: The container name prefix; the sandbox gateway accepts only names of the sandbox's pattern.
NAME_PREFIX = "nodestro-sandbox-mcp-"


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


def launch(
    *,
    image: str,
    module: str,
    args: list[str],
    memory_mb: int,
    docker: str = "docker",
    docker_env: Mapping[str, str] | None = None,
    python: str = sys.executable,
) -> StdioLaunch:
    """The relay command that runs ``python -m module args`` in a sandbox container of ``image``."""
    name = container_name()
    run = [
        *sandbox_run_args(docker, name, image, memory_mb=memory_mb, cpus=CPUS, pids_limit=PIDS_LIMIT),
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
