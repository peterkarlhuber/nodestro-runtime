"""Every Levuvo dependency follows the edge list K-33 (TABRION AC-2, QA-17).

The policy is ``platform/dependency-policy.json``: the components with their kind, and the allowed edges
``{from, to, kind, extra, status}``. ``to`` is a distribution with the extras it may be required with
(``tabrion[postgres]``); ``kind`` is ``required`` (core dependency, ``extra`` is null), ``optional`` (the extra
``extra``) or ``dev`` (a development or test extra); ``status`` is ``active`` or ``pending:<decision>``.

The script checks the policy itself:

- a ``foundation`` (tabrion) depends on no Levuvo package (P6),
- a ``library`` depends only on a foundation (orkexa and axentra never on each other or on a platform),
- nothing but a studio's own components may be a studio's target, and no component depends on a studio,
- the edges (active and pending) form no cycle (P5),

and then each manifest (``pyproject.toml``: core dependencies, every extra and every dependency group): each
requirement of a Levuvo package (a name in one of the policy's namespaces, or a URL into the owner's GitHub) must be
an active edge of that component with the same kind and extra, and its extras must be ones the edge names. A
library never requires a component marked ``no_direct_url`` (tabrion) by a direct URL, only by a version range
(RC-1); nodestro-runtime's extras and nodestro-api's ``dev`` may carry its git pin (V-B0, RC-2). A package that refers
to itself (``axentra[all]`` -> ``axentra[server]``) is not an edge.

Usage::

    python scripts/check_dependency_policy.py                    # the policy and this studio's pyproject.toml
    python scripts/check_dependency_policy.py --pinned           # ... and every component of platform/versions.json
    python scripts/check_dependency_policy.py --local DIR        # ... read from sibling checkouts in DIR
    python scripts/check_dependency_policy.py --manifest NAME=PATH [--policy FILE]

Only the standard library is used, so a library carries an unchanged copy of this file in ``scripts/`` next to its
copy of the policy, ``dependency-policy.json`` at its root (``--copy NAME`` prints it: the components, the
namespaces and only NAME's outgoing edges, with ``"component": NAME``). Without ``platform/dependency-policy.json``
the script reads that copy and checks the repository's own ``pyproject.toml`` as that component.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "platform" / "dependency-policy.json"
#: A library's copy of its own outgoing edges.
LOCAL_POLICY = ROOT / "dependency-policy.json"
VERSIONS = ROOT / "platform" / "versions.json"
OWNER = "peterkarlhuber"
KINDS = {"foundation", "library", "platform", "studio"}
EDGE_KINDS = {"required", "optional", "dev"}
#: PEP 508: the name, its extras and, after "@", a direct URL.
REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[(?P<extras>[^\]]*)\])?\s*(?P<rest>.*)$",
    re.DOTALL,
)
OWNER_URL = re.compile(r"github\.com[/:]" + OWNER + r"/", re.IGNORECASE)


def normalize(name: str) -> str:
    """PEP 503 normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Requirement:
    name: str
    extras: frozenset[str]
    url: bool
    owner_url: bool
    text: str


def parse_requirement(text: str) -> Requirement | None:
    match = REQUIREMENT.match(text)
    if match is None:
        return None
    extras = frozenset(normalize(item.strip()) for item in (match["extras"] or "").split(",") if item.strip())
    rest = match["rest"].split(";", 1)[0].strip()
    url = rest.startswith("@")
    return Requirement(normalize(match["name"]), extras, url, bool(OWNER_URL.search(rest)), text.strip())


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    target_extras: frozenset[str]
    kind: str
    extra: str | None
    status: str

    def describe(self) -> str:
        where = "core" if self.extra is None else f"extra {self.extra!r}"
        extras = f"[{','.join(sorted(self.target_extras))}]" if self.target_extras else ""
        return f"{self.source} -> {self.target}{extras} ({self.kind}, {where}, {self.status})"


class Policy:
    """The parsed policy; ``errors`` lists everything wrong with the file itself."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.errors: list[str] = []
        self.namespaces = tuple(normalize(item) for item in data.get("namespaces", []))
        self.components: dict[str, dict[str, Any]] = dict(data.get("components", {}))
        self.distributions: dict[str, str] = {}
        for name, entry in self.components.items():
            if entry.get("kind") not in KINDS:
                self.errors.append(f"component {name}: kind must be one of {sorted(KINDS)}")
            distribution = normalize(entry.get("distribution", name))
            self.distributions[distribution] = name
            if not distribution.startswith(self.namespaces):
                # Otherwise the manifest check would not see the package by its name.
                self.errors.append(f"component {name}: {distribution} is in none of the namespaces")
        self.edges: list[Edge] = []
        for index, raw in enumerate(data.get("edges", [])):
            edge = self._edge(index, raw)
            if edge is not None:
                self.edges.append(edge)
        self.component: str | None = data.get("component")
        if self.component is not None:
            if self.component not in self.components:
                self.errors.append(f"copy for {self.component}: not a component of the policy")
            for edge in self.edges:
                if edge.source != self.component:
                    self.errors.append(f"{edge.describe()}: a copy holds only {self.component}'s outgoing edges")
        self.errors.extend(self._rules())
        self.errors.extend(self._cycles())

    def _edge(self, index: int, raw: Mapping[str, Any]) -> Edge | None:
        label = f"edge {index} ({raw.get('from')} -> {raw.get('to')})"
        target = parse_requirement(str(raw.get("to", "")))
        source = raw.get("from")
        problems = []
        if source not in self.components:
            problems.append(f"{label}: unknown source component")
        if target is None or target.name not in self.distributions:
            problems.append(f"{label}: unknown target")
        kind, extra, status = raw.get("kind"), raw.get("extra"), str(raw.get("status", ""))
        if kind not in EDGE_KINDS:
            problems.append(f"{label}: kind must be one of {sorted(EDGE_KINDS)}")
        if (kind == "required") != (extra is None):
            problems.append(f"{label}: a required edge has no extra, an optional or dev edge names one")
        if status != "active" and not re.fullmatch(r"pending:\S+", status):
            problems.append(f"{label}: status must be 'active' or 'pending:<decision>'")
        self.errors.extend(problems)
        if problems or target is None:
            return None
        return Edge(
            str(source),
            self.distributions[target.name],
            target.extras,
            str(kind),
            None if extra is None else normalize(str(extra)),
            status,
        )

    def _rules(self) -> Iterable[str]:
        for edge in self.edges:
            source, target = self.components[edge.source], self.components[edge.target]
            if edge.source == edge.target:
                yield f"{edge.describe()}: a component is no edge to itself"
            elif source["kind"] == "foundation":
                yield f"{edge.describe()}: {edge.source} depends on no Levuvo package (P6)"
            elif source["kind"] == "library" and target["kind"] != "foundation":
                yield f"{edge.describe()}: a library depends only on {self._foundations()}"
            elif target["kind"] == "studio":
                yield f"{edge.describe()}: no component depends on a studio"
            elif source["kind"] == "studio" and edge.target not in source.get("studio_of", []):
                yield f"{edge.describe()}: {edge.source} may only use {source.get('studio_of', [])} (no private edge)"

    def _foundations(self) -> list[str]:
        return sorted(name for name, entry in self.components.items() if entry.get("kind") == "foundation")

    def _cycles(self) -> Iterable[str]:
        graph: dict[str, set[str]] = {}
        for edge in self.edges:
            if edge.source != edge.target:
                graph.setdefault(edge.source, set()).add(edge.target)
        state: dict[str, int] = {}  # 1 on the stack, 2 done
        found: list[str] = []

        def visit(node: str, path: list[str]) -> None:
            state[node] = 1
            for nxt in sorted(graph.get(node, ())):
                if state.get(nxt) == 1:
                    cycle = [*path[path.index(nxt) :], nxt]
                    found.append("cycle: " + " -> ".join(cycle) + " (P5)")
                elif nxt not in state:
                    visit(nxt, [*path, nxt])
            state[node] = 2

        for node in sorted(graph):
            if node not in state:
                visit(node, [node])
        return found

    def is_levuvo(self, requirement: Requirement) -> bool:
        return requirement.owner_url or requirement.name.startswith(self.namespaces)

    def check_manifest(self, component: str, pyproject: Mapping[str, Any]) -> list[str]:
        if component not in self.components:
            return [f"{component}: not a component of the policy"]
        own = normalize(self.components[component].get("distribution", component))
        project = pyproject.get("project", {})
        sections: list[tuple[str | None, list[Any]]] = [(None, list(project.get("dependencies", [])))]
        sections += [(normalize(name), list(items)) for name, items in project.get("optional-dependencies", {}).items()]
        groups = pyproject.get("dependency-groups", {})
        sections += [(normalize(name), [i for i in items if isinstance(i, str)]) for name, items in groups.items()]
        errors = []
        for extra, items in sections:
            where = "core" if extra is None else f"extra {extra!r}"
            for text in items:
                requirement = parse_requirement(str(text))
                if requirement is None:
                    errors.append(f"{component} ({where}): cannot parse {text!r}")
                    continue
                if requirement.name == own or not self.is_levuvo(requirement):
                    continue
                problem = self._match(component, extra, requirement)
                if problem:
                    errors.append(f"{component} ({where}): {requirement.text!r}: {problem}")
        return errors

    def _match(self, component: str, extra: str | None, requirement: Requirement) -> str | None:
        target = self.distributions.get(requirement.name)
        if target is None:
            return "unknown Levuvo package (add it to the policy first)"
        library = self.components[component].get("kind") == "library"
        if requirement.url and library and self.components[target].get("no_direct_url"):
            return f"a library requires {target} by a version range, never by a direct URL (RC-1)"
        candidates = [edge for edge in self.edges if edge.source == component and edge.target == target]
        if not candidates:
            return f"no edge {component} -> {target} in the policy (K-33)"
        placed = [edge for edge in candidates if edge.extra == extra]
        if not placed:
            allowed = ", ".join(edge.describe() for edge in candidates)
            return f"{component} -> {target} is not allowed here; the policy has: {allowed}"
        active = [edge for edge in placed if edge.status == "active"]
        if not active:
            return f"the edge is {placed[0].status}, not active yet"
        if not any(requirement.extras <= edge.target_extras for edge in active):
            allowed = " or ".join(f"[{','.join(sorted(edge.target_extras))}]" for edge in active)
            return f"extras [{','.join(sorted(requirement.extras))}] are not in the policy's {allowed}"
        return None


def load_policy(path: Path = POLICY) -> Policy:
    return Policy(json.loads(path.read_text(encoding="utf-8")))


def policy_copy(data: Mapping[str, Any], component: str) -> dict[str, Any]:
    """A library's copy: everything but the edges of other components."""
    if component not in data.get("components", {}):
        raise SystemExit(f"{component} is not a component of the policy")
    rest = {key: value for key, value in data.items() if key not in {"comment", "component", "edges"}}
    return {
        "comment": f"Copy of nodestro-studio platform/dependency-policy.json for {component} (TABRION K-33, "
        "QA-17): only its outgoing edges. Never edit by hand; regenerate with "
        f"python scripts/check_dependency_policy.py --copy {component} in nodestro-studio.",
        "component": component,
        **rest,
        "edges": [edge for edge in data.get("edges", []) if edge.get("from") == component],
    }


def dump(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def pinned_manifests(local: Path | None) -> dict[str, str]:
    """The pyproject.toml of every component of platform/versions.json at its pinned commit."""
    components = json.loads(VERSIONS.read_text(encoding="utf-8"))["components"]
    texts = {}
    with tempfile.TemporaryDirectory() as cache:
        for repo, entry in components.items():
            path = entry.get("pyproject", "pyproject.toml")
            checkout = (local / repo) if local else Path(cache) / repo
            if local is None:
                url = f"https://github.com/{OWNER}/{repo}.git"
                git("clone", "--quiet", "--filter=blob:none", "--no-checkout", url, str(checkout))
            texts[repo] = git("show", f"{entry['commit']}:{path}", cwd=checkout)
    return texts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--policy", type=Path, help="default: platform/dependency-policy.json or a library's copy")
    parser.add_argument("--pinned", action="store_true", help="also check every component of versions.json")
    parser.add_argument("--local", type=Path, help="read the pinned components from sibling checkouts in DIR")
    parser.add_argument("--manifest", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--no-studio", action="store_true", help="do not check this repository's pyproject.toml")
    parser.add_argument("--copy", metavar="NAME", help="print the policy copy for component NAME and exit")
    args = parser.parse_args(argv)

    path = args.policy or (POLICY if POLICY.is_file() else LOCAL_POLICY)
    if args.copy:
        sys.stdout.write(dump(policy_copy(json.loads(path.read_text(encoding="utf-8")), args.copy)))
        return 0
    policy = load_policy(path)
    errors = list(policy.errors)
    manifests: dict[str, str] = {}
    if not args.no_studio and not args.manifest and (ROOT / "pyproject.toml").is_file():
        own = policy.component or "nodestro-studio"
        manifests[own] = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if args.pinned or args.local:
        manifests.update(pinned_manifests(args.local))
    for item in args.manifest:
        name, sep, path = item.partition("=")
        if not sep:
            parser.error(f"--manifest takes NAME=PATH, not {item!r}")
        manifests[name] = Path(path).read_text(encoding="utf-8")
    for name, text in manifests.items():
        errors.extend(policy.check_manifest(name, tomllib.loads(text)))

    for error in errors:
        print(f"dependency policy: {error}", file=sys.stderr)
    if errors:
        return 1
    checked = ", ".join(manifests) or "no manifest"
    print(f"dependency policy: ok ({len(policy.edges)} edges, no cycle; {checked})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
