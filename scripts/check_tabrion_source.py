"""An install command never asks a package index for ``tabrion``: the negative dependency-confusion test (DC-NS-2).

Usage::

    python -I scripts/check_tabrion_source.py --lock LOCK [--name NAME ...] [--python PY | --path DIR] \\
        [--upstream URL] [--no-control] -- COMMAND [ARG ...]

The script

1. writes a poisoned ``NAME`` 0.1.99 (default ``tabrion``) into a temporary directory: a wheel whose ``.pth`` file
   and package write a marker file when Python starts or imports it, and an sdist whose build writes the marker and
   then fails,
2. serves a PEP 503/691 index on ``127.0.0.1`` with that release for ``NAME``; every other project is redirected to
   the real index (``--upstream``, default ``https://pypi.org/simple``), so the install under test runs unchanged,
3. runs a positive control (unless ``--no-control``): ``pip install --dry-run "NAME"`` against the index must pick
   0.1.99, so the poisoned index is live and the test is not vacuous,
4. runs ``COMMAND`` with ``PIP_INDEX_URL``, ``UV_INDEX_URL`` and ``CHECK_TABRION_SOURCE_INDEX`` set to the index,
5. fails (exit 1, RED) unless ``COMMAND`` succeeded, the index got no request for ``NAME`` (neither its project
   page nor a file), no marker file exists, and the ``NAME`` installed in ``--python`` (default: this interpreter)
   or ``--path`` was installed from the git URL of ``LOCK``'s ``NAME @ git+URL@SHA`` line at exactly that ``SHA``
   (its ``direct_url.json``).

A direct-URL candidate (``-r`` or ``-c`` on pip >= 26.1, uv ``--override``) makes pip and uv skip the index for the
name, so step 5 holds; a plain range (``pip install -e ".[dev]"`` without the lock) asks the index and installs
0.1.99, so it fails. Detection after an install is not prevention (an sdist runs its build code first): only the
zero-request assertion shows that the pinned path never fetched the poisoned release.

Standard library only. Canonical copy: nodestro-studio ``scripts/check_tabrion_source.py``; the consumers carry an
unchanged copy whose SHA-256 their tests pin (as for ``check_tabrion_api.py``, TABRION QA-21). Every line of output
starts with ``check_tabrion_source:``; the last one says GREEN or RED.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
from typing import Any
import zipfile

PREFIX = "check_tabrion_source:"
POISON_VERSION = "0.1.99"
#: Extras the consumers request; the poisoned release declares them so no installer refuses it for a missing extra.
POISON_EXTRAS = ("all", "dev", "migrations", "postgres", "sqlalchemy", "testing")
UPLOAD_TIME = "2020-01-01T00:00:00Z"  # before every --uploaded-prior-to cutoff
DEFAULT_UPSTREAM = "https://pypi.org/simple"
#: A lock or pin line: ``name[extras] @ git+URL@SHA`` (comments and options after it are ignored).
GIT_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*@\s*git\+(?P<url>\S+?)@(?P<ref>[0-9a-f]{40})\b"
)
#: A distribution file name: ``name-version...`` (wheel) or ``name-version.tar.gz`` (sdist).
FILE_NAME = re.compile(r"^(?P<name>[A-Za-z0-9._]+?)-\d")
INSPECT = """
import json, re, sys
from importlib import metadata
paths = json.loads(sys.argv[1])
names = sys.argv[2:]
out = {}
for dist in (metadata.distributions(path=paths) if paths is not None else metadata.distributions()):
    name = dist.metadata["Name"]
    key = re.sub(r"[-_.]+", "-", name).lower() if name else ""
    if key in names and key not in out:
        text = dist.read_text("direct_url.json")
        out[key] = {"version": dist.version, "direct_url": json.loads(text) if text else None}
print(json.dumps(out))
"""


def normalize(name: str) -> str:
    """A project name normalized as in PEP 503 (``Tabrion_Studio`` -> ``tabrion-studio``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def say(line: str) -> None:
    print(f"{PREFIX} {line}", flush=True)


@dataclass(frozen=True)
class Pin:
    """What a lock pins for one name."""

    url: str
    commit: str


def read_pins(text: str, names: Iterable[str]) -> dict[str, Pin]:
    """The ``NAME @ git+URL@SHA`` pins of ``names`` in a lock or requirements file (the first line of a name wins)."""
    wanted = {normalize(name) for name in names}
    pins: dict[str, Pin] = {}
    for line in text.splitlines():
        found = GIT_REQUIREMENT.match(line.split("#", 1)[0])
        if found and normalize(found["name"]) in wanted:
            pins.setdefault(normalize(found["name"]), Pin(found["url"], found["ref"]))
    return pins


def same_url(found: str, expected: str) -> bool:
    """Git URLs compared without a ``git+`` prefix, a trailing slash or ``.git``."""

    def strip(url: str) -> str:
        url = url.removeprefix("git+").rstrip("/")
        return url.removesuffix(".git")

    return strip(found) == strip(expected)


def check_installed(name: str, found: Mapping[str, Any] | None, pin: Pin) -> list[str]:
    """Problems of the installed ``name`` (``{"version", "direct_url"}`` or None) against its pin."""
    if found is None:
        return [f"{name}: not installed, the lock pins {pin.url}@{pin.commit[:12]}"]
    direct = found.get("direct_url")
    version = found.get("version")
    if not direct:
        return [f"{name} {version}: not installed from a pinned commit (no direct_url.json, e.g. from a package index)"]
    url = str(direct.get("url", ""))
    vcs = direct.get("vcs_info") or {}
    commit = str(vcs.get("commit_id", ""))
    if vcs.get("vcs") != "git":
        return [f"{name} {version}: not a git install ({url}), the lock pins {pin.url}@{pin.commit[:12]}"]
    if not same_url(url, pin.url):
        return [f"{name}: installed from {url}, the lock pins {pin.url}"]
    if commit != pin.commit:
        return [f"{name}: installed commit {commit[:12] or '-'}, the lock pins {pin.commit[:12]}"]
    return []


# --- the poisoned release ------------------------------------------------------------------------------------------


def _record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{path},sha256={digest},{len(data)}"


def _metadata(name: str) -> str:
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {POISON_VERSION}", "Summary: POISONED test release"]
    lines += [f"Provides-Extra: {extra}" for extra in POISON_EXTRAS]
    return "\n".join(lines) + "\n\n"


def _marker_code(marker: Path, what: str) -> str:
    """One line of Python that appends ``what`` to ``marker`` (silently nothing once the check's temp dir is gone)."""
    write = f"open({str(marker)!r}, 'a', encoding='utf-8').write({what!r} + chr(10))"
    return f"exec({f'try: {write}{chr(10)}except OSError: pass'!r})"


def build_wheel(name: str, marker: Path, directory: Path) -> Path:
    """A pure wheel of ``name`` 0.1.99 that writes ``marker`` when any Python starts (``.pth``) or imports it."""
    module = normalize(name).replace("-", "_")
    dist_info = f"{module}-{POISON_VERSION}.dist-info"
    files = {
        f"{module}/__init__.py": f"POISONED = True\n{_marker_code(marker, f'import {name}')}\n".encode(),
        f"{module}_poison.pth": f"import builtins; {_marker_code(marker, f'{name} .pth')}\n".encode(),
        f"{dist_info}/METADATA": _metadata(name).encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: check_tabrion_source\nRoot-Is-Purelib: true\n"
        b"Tag: py3-none-any\n",
    }
    record = [_record_line(path, data) for path, data in files.items()] + [f"{dist_info}/RECORD,,"]
    files[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode()
    path = directory / f"{module}-{POISON_VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as wheel:
        for member, data in files.items():
            wheel.writestr(member, data)
    return path


def build_sdist(name: str, marker: Path, directory: Path) -> Path:
    """An sdist of ``name`` 0.1.99 whose in-tree build backend writes ``marker`` and then fails (no build deps)."""
    module = normalize(name).replace("-", "_")
    root = f"{module}-{POISON_VERSION}"
    backend = (
        f"{_marker_code(marker, f'{name} sdist build')}\n"
        "def _refuse(*args, **kwargs):\n"
        "    raise RuntimeError('check_tabrion_source: the POISONED sdist was built')\n"
        "get_requires_for_build_wheel = get_requires_for_build_editable = lambda config_settings=None: []\n"
        "prepare_metadata_for_build_wheel = build_wheel = build_sdist = build_editable = _refuse\n"
    )
    files = {
        f"{root}/PKG-INFO": _metadata(name).encode(),
        f"{root}/pyproject.toml": (
            b'[build-system]\nrequires = []\nbuild-backend = "poison_backend"\nbackend-path = ["."]\n'
        ),
        f"{root}/poison_backend.py": backend.encode(),
    }
    path = directory / f"{module}-{POISON_VERSION}.tar.gz"
    with tarfile.open(path, "w:gz") as sdist:
        for member, data in files.items():
            info = tarfile.TarInfo(member)
            info.size = len(data)
            info.mtime = 1577836800
            sdist.addfile(info, io.BytesIO(data))
    return path


# --- the index -----------------------------------------------------------------------------------------------------


@dataclass
class PoisonedIndex:
    """A threaded index on 127.0.0.1 that serves the poisoned files and logs every request for a poisoned name."""

    files: Mapping[str, list[Path]]  # normalized name -> its poisoned files
    upstream: str = DEFAULT_UPSTREAM
    hits: list[str] = field(default_factory=list)
    requests: int = 0

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/simple"

    def __enter__(self) -> PoisonedIndex:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def reset(self) -> None:
        with self._lock:
            self.hits.clear()

    def _record(self, line: str, poisoned: bool) -> None:
        with self._lock:
            self.requests += 1
            if poisoned:
                self.hits.append(line)

    def _page(self, name: str, json_api: bool) -> tuple[bytes, str]:
        entries = [(path.name, hashlib.sha256(path.read_bytes()).hexdigest()) for path in self.files[name]]
        base = self.url.removesuffix("/simple") + "/files"
        if json_api:
            body = {
                "meta": {"api-version": "1.1"},
                "name": name,
                "versions": [POISON_VERSION],
                "files": [
                    {
                        "filename": file,
                        "url": f"{base}/{file}",
                        "hashes": {"sha256": digest},
                        "upload-time": UPLOAD_TIME,
                    }
                    for file, digest in entries
                ],
            }
            return json.dumps(body).encode(), "application/vnd.pypi.simple.v1+json"
        links = "".join(f'<a href="{base}/{file}#sha256={digest}">{file}</a><br>\n' for file, digest in entries)
        html = f"<!DOCTYPE html>\n<html><body><h1>{name}</h1>\n{links}</body></html>\n"
        return html.encode(), "text/html"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        index = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain", **headers: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_HEAD(self) -> None:
                self.do_GET()

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0].split("#", 1)[0]
                parts = [part for part in path.split("/") if part]
                if len(parts) == 2 and parts[0] == "simple":
                    name = normalize(parts[1])
                    poisoned = name in index.files
                    index._record(f"{self.command} {self.path}", poisoned)
                    if not poisoned:
                        self._send(302, Location=f"{index.upstream.rstrip('/')}/{parts[1]}/")
                        return
                    body, content_type = index._page(name, "vnd.pypi.simple.v1+json" in self.headers.get("Accept", ""))
                    self._send(200, body, content_type)
                    return
                if len(parts) == 2 and parts[0] == "files":
                    found = FILE_NAME.match(parts[1])
                    name = normalize(found["name"]) if found else ""
                    index._record(f"{self.command} {self.path}", name in index.files)
                    for file in index.files.get(name, []):
                        if file.name == parts[1]:
                            self._send(200, file.read_bytes(), "application/octet-stream")
                            return
                index._record(f"{self.command} {self.path}", False)
                self._send(404, b"not found\n")

        return Handler


# --- running -------------------------------------------------------------------------------------------------------


def child_environment(index_url: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of the command under test: every installer's default index is the poisoned one."""
    env = dict(os.environ if base is None else base)
    env.update(PIP_INDEX_URL=index_url, UV_INDEX_URL=index_url, CHECK_TABRION_SOURCE_INDEX=index_url)
    env.update(PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    for key in ("NO_PROXY", "no_proxy"):  # the index is local; a configured proxy must not see it
        hosts = [host for host in env.get(key, "").split(",") if host]
        env[key] = ",".join([*hosts, "127.0.0.1", "localhost"])
    return env


def inspect(python: str, paths: list[str] | None, names: Sequence[str]) -> dict[str, Any]:
    """``{name: {"version", "direct_url"}}`` of the installed ``names`` in ``python`` (or in ``paths``)."""
    result = subprocess.run(
        [python, "-I", "-c", INSPECT, json.dumps(paths), *names], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot read the installed distributions of {python}: {result.stderr.strip()}")
    found: dict[str, Any] = json.loads(result.stdout)
    return found


def control(index: PoisonedIndex, name: str, env: Mapping[str, str], workdir: Path) -> list[str]:
    """Problems of the positive control: ``pip install --dry-run NAME`` against the index must choose 0.1.99."""
    report = workdir / f"control-{name}.json"
    command = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--no-deps", "--quiet"]
    result = subprocess.run(
        [*command, "--report", str(report), name], env=env, capture_output=True, text=True, check=False
    )
    problems: list[str] = []
    try:
        chosen = [item["metadata"]["version"] for item in json.loads(report.read_text(encoding="utf-8"))["install"]]
    except (OSError, ValueError, KeyError, TypeError):
        chosen = []
    if result.returncode != 0 or chosen != [POISON_VERSION]:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        problems.append(f"control: pip chose {chosen or 'nothing'} for {name}, expected {POISON_VERSION} ({tail[0]})")
    if not index.hits:
        problems.append(f"control: the index logged no request for {name}")
    return problems


def run(
    command: Sequence[str],
    pins: Mapping[str, Pin],
    *,
    python: str,
    paths: list[str] | None,
    upstream: str,
    with_control: bool,
    execute: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    """The whole check (see the module docstring); 0 is GREEN, 1 is RED."""
    names = sorted(pins)
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="check-tabrion-source-") as temp:
        workdir = Path(temp)
        marker = workdir / "POISONED-MARKER"
        (workdir / "dist").mkdir()
        files = {
            name: [build_wheel(name, marker, workdir / "dist"), build_sdist(name, marker, workdir / "dist")]
            for name in names
        }
        with PoisonedIndex(files, upstream) as index:
            env = child_environment(index.url)
            say(f"poisoned index {index.url} serves {', '.join(f'{n} {POISON_VERSION}' for n in names)}")
            if with_control:
                for name in names:
                    found = control(index, name, env, workdir)
                    problems += found
                    if not found:
                        say(f"ok   control: pip install --dry-run {name} chooses the poisoned {POISON_VERSION}")
                    index.reset()
                if marker.exists():
                    problems.append("control: the marker exists before the command ran")
                    marker.unlink()
            say(f"run  {' '.join(command)}")
            result = execute(list(command), env=env, check=False)
            if result.returncode != 0:
                problems.append(f"the command exited with {result.returncode}")
            hits = list(index.hits)
            if hits:
                problems += [f"the index was asked for a poisoned name: {hit}" for hit in hits]
            else:
                say(f"ok   no request for {', '.join(names)} reached the index ({index.requests} other request(s))")
        try:
            installed = inspect(python, paths, names)
        except RuntimeError as exc:
            installed = {}
            problems.append(str(exc))
        for name in names:
            found_problems = check_installed(name, installed.get(name), pins[name])
            problems += found_problems
            if not found_problems:
                say(
                    f"ok   {name} {installed[name]['version']} installed from {pins[name].url}@{pins[name].commit[:12]}"
                )
        if marker.exists():
            written = marker.read_text(encoding="utf-8").splitlines()
            problems.append(f"the poisoned release ran code: marker {written}")
        else:
            say("ok   no poisoned code ran (no marker)")
    for line in problems:
        say(f"FAIL {line}")
    say(f"{'RED' if problems else 'GREEN'} ({len(problems)} problem(s))")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--lock", type=Path, required=True, help="the lock or pin file with NAME @ git+URL@SHA")
    parser.add_argument("--name", action="append", help="a poisoned name (repeatable; default: tabrion)")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--python", default=sys.executable, help="the interpreter the command installs into")
    where.add_argument("--path", action="append", help="read the installed distributions in this directory instead")
    parser.add_argument(
        "--upstream", default=DEFAULT_UPSTREAM, help=f"index for other names (default {DEFAULT_UPSTREAM})"
    )
    parser.add_argument("--no-control", action="store_true", help="skip the positive control")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARG ...]")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command: add -- COMMAND [ARG ...]")
    names = [normalize(name) for name in (args.name or ["tabrion"])]
    try:
        pins = read_pins(args.lock.read_text(encoding="utf-8"), names)
    except OSError as exc:
        say(f"FAIL cannot read {args.lock}: {exc}")
        say("RED (1 problem(s))")
        return 1
    missing = [name for name in names if name not in pins]
    if missing:
        say(f"FAIL {args.lock} pins no {', '.join(missing)} to a commit (NAME @ git+URL@<40 hex>)")
        say("RED (1 problem(s))")
        return 1
    return run(
        command, pins, python=args.python, paths=args.path, upstream=args.upstream, with_control=not args.no_control
    )


if __name__ == "__main__":
    sys.exit(main())
