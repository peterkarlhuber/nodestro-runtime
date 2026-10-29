"""Which Python packages a tool needs: imports, module names and package names.

Everything here is static. The code is parsed, never imported or run:

* ``import x`` and ``from x import y`` name a top-level module. Modules of the standard
  library, the tool module itself (``tool``, which the tests import) and the sandbox runtime
  (``pydantic``, ``typing_extensions``) need no package.
* Every other module needs a package from PyPI. The module name is not always the package
  name (``PIL`` comes from ``Pillow``, ``sklearn`` from ``scikit-learn``), so a curated table
  maps well-known modules. Anything else must be declared.
* Declarations use the standard inline metadata block (PEP 723) at the top of the tool code,
  which also carries version requirements::

      # /// script
      # dependencies = ["pandas>=2.2", "openpyxl"]
      # ///

* Imports Nodestro cannot resolve (an unknown module that no declared package explains, or a
  dynamic import by string) are reported instead of guessed: nothing is ever installed on a
  guess.

Imports inside ``try:`` blocks that handle ``ImportError`` are optional and not required.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
import importlib
import re
import sys
from typing import Literal

from pydantic import BaseModel, Field

#: The module the tests import the tool from (``from tool import f``).
LOCAL_MODULES = frozenset({"tool"})

#: Modules every sandbox environment provides; they come with Nodestro and need no approval.
RUNTIME_MODULES = frozenset({"pydantic", "pydantic_core", "typing_extensions", "annotated_types"})

#: Packages Nodestro installs into every environment so the sandbox runner works.
RUNTIME_PACKAGES = ("pydantic", "typing-extensions")

#: Import name -> package name where they differ, for well-known libraries.
MODULE_PACKAGES: dict[str, str] = {
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "cv2": "opencv-python-headless",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "dotenv": "python-dotenv",
    "Bio": "biopython",
    "attr": "attrs",
    "Crypto": "pycryptodome",
    "jose": "python-jose",
    "jwt": "PyJWT",
    "magic": "python-magic",
    "fitz": "PyMuPDF",
    "google.protobuf": "protobuf",
    "serial": "pyserial",
    "usb": "pyusb",
    "git": "GitPython",
    "Levenshtein": "levenshtein",
    "slugify": "python-slugify",
    "multipart": "python-multipart",
    "markdown_it": "markdown-it-py",
    "pkg_resources": "setuptools",
    "OpenSSL": "pyOpenSSL",
    "zmq": "pyzmq",
    "faiss": "faiss-cpu",
    "sentencepiece": "sentencepiece",
    "tzlocal": "tzlocal",
    "rapidfuzz": "rapidfuzz",
    "xlsxwriter": "XlsxWriter",
    "pdfminer": "pdfminer.six",
    "statsmodels": "statsmodels",
    "lxml": "lxml",
}

#: Well-known libraries whose import name is their package name.
KNOWN_PACKAGES = frozenset(
    {
        "numpy",
        "pandas",
        "scipy",
        "sympy",
        "matplotlib",
        "seaborn",
        "plotly",
        "networkx",
        "requests",
        "httpx",
        "openpyxl",
        "xlrd",
        "pyarrow",
        "polars",
        "duckdb",
        "shapely",
        "pyproj",
        "numexpr",
        "numba",
        "joblib",
        "tabulate",
        "jinja2",
        "markdown",
        "regex",
        "chardet",
        "charset_normalizer",
        "pytz",
        "tzdata",
        "arrow",
        "pendulum",
        "babel",
        "unidecode",
        "nltk",
        "spacy",
        "textblob",
        "emoji",
        "pint",
        "uncertainties",
        "mpmath",
        "cvxpy",
        "pulp",
        "ortools",
        "simpy",
        "xarray",
        "h5py",
        "netCDF4",
        "astropy",
        "pywt",
        "qrcode",
        "reportlab",
        "pypdf",
        "pdfplumber",
        "feedparser",
        "html5lib",
        "jsonschema",
        "toml",
        "tomli",
        "orjson",
        "ujson",
        "msgpack",
        "cryptography",
        "bcrypt",
        "xmltodict",
        "phonenumbers",
        "validators",
        "holidays",
        "workalendar",
        "geopy",
        "folium",
        "torch",
        "tensorflow",
        "transformers",
        "tiktoken",
        "lightgbm",
        "xgboost",
        "catboost",
        "prophet",
        "pmdarima",
        "filterpy",
        "more_itertools",
        "toolz",
        "cachetools",
        "sortedcontainers",
        "bitarray",
        "rich",
        "tqdm",
    }
)
# Fix the spelling of names whose package differs only in case or separators.
_KNOWN_SPELLING = {
    "jinja2": "Jinja2",
    "charset_normalizer": "charset-normalizer",
    "netCDF4": "netCDF4",
    "pywt": "PyWavelets",
    "more_itertools": "more-itertools",
    "babel": "Babel",
    "markdown": "Markdown",
}

_SCRIPT_BLOCK = re.compile(r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)[ \t]*$\s(?P<content>(^#(| .*)$\s)+)^# ///[ \t]*$")
_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?P<extras>\[[A-Za-z0-9._,\s-]*\])?"
    r"\s*(?P<spec>(?:(?:===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+\s*,?\s*)*)"
    r"(?:;\s*(?P<marker>[^#]+))?\s*$"
)

ModuleKind = Literal["stdlib", "local", "runtime", "third_party", "unresolved"]


def normalize(name: str) -> str:
    """A package name in its canonical form (PEP 503): ``Scikit_Learn`` -> ``scikit-learn``."""
    return re.sub(r"[-_.]+", "-", name).lower()


class Requirement(BaseModel):
    """A package a tool needs, with the version requirement from its header (if any)."""

    name: str
    #: The canonical name used for comparisons.
    key: str
    #: PEP 440 specifier, e.g. ``>=2.2,<3``; empty means any version.
    spec: str = ""
    extras: list[str] = Field(default_factory=list)
    marker: str = ""
    #: The modules of the code this package provides (empty for packages only declared).
    modules: list[str] = Field(default_factory=list)
    #: ``header``: declared in the ``# /// script`` block; ``import``: derived from an import.
    source: Literal["header", "import"] = "import"

    def line(self) -> str:
        """The requirement as a line for the resolver (``pandas[excel]>=2.2; python_version>='3.11'``)."""
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        marker = f"; {self.marker}" if self.marker else ""
        return f"{self.name}{extras}{self.spec}{marker}"


class ImportedModule(BaseModel):
    name: str
    kind: ModuleKind
    line: int
    optional: bool = False
    #: The package providing a third-party module.
    package: str | None = None


class DependencyIssue(BaseModel):
    """Something Nodestro cannot decide on its own; ``code``/``params`` are shown in the UI language."""

    code: str
    message: str
    params: dict[str, str | int] = Field(default_factory=dict)
    line: int | None = None


class ToolDependencies(BaseModel):
    """The result of :func:`analyze` for one tool."""

    requirements: list[Requirement] = Field(default_factory=list)
    modules: list[ImportedModule] = Field(default_factory=list)
    issues: list[DependencyIssue] = Field(default_factory=list)

    @property
    def third_party(self) -> bool:
        return bool(self.requirements)

    def import_names(self) -> set[str]:
        """Top-level modules of third-party packages, for the static check's ``extra_imports``."""
        return {module.name for module in self.modules if module.kind == "third_party"}


def _stdlib() -> frozenset[str]:
    names = set(getattr(sys, "stdlib_module_names", ())) | set(sys.builtin_module_names)
    return frozenset(name for name in names if not name.startswith("_") or name == "__future__")


STDLIB_MODULES = _stdlib()


def package_for(module: str) -> str | None:
    """The package of a well-known third-party module, or ``None`` if Nodestro does not know it."""
    if module in MODULE_PACKAGES:
        return MODULE_PACKAGES[module]
    if module in KNOWN_PACKAGES:
        return _KNOWN_SPELLING.get(module, module)
    return None


def classify(module: str) -> ModuleKind:
    if module in LOCAL_MODULES:
        return "local"
    if module in RUNTIME_MODULES:
        return "runtime"
    if module in STDLIB_MODULES:
        return "stdlib"
    return "third_party" if package_for(module) is not None else "unresolved"


def parse_requirement(text: str) -> Requirement | None:
    """A requirement line (name, extras, version specifier, marker) or ``None`` if it is invalid.

    URLs, paths and other direct references are refused: packages come from the index only.
    """
    if "@" in text or "://" in text:
        return None
    match = _REQUIREMENT.match(text)
    if match is None:
        return None
    extras = [item.strip() for item in (match.group("extras") or "").strip("[]").split(",") if item.strip()]
    spec = re.sub(r"\s+", "", match.group("spec") or "").rstrip(",")
    return Requirement(
        name=match.group("name"),
        key=normalize(match.group("name")),
        spec=spec,
        extras=extras,
        marker=(match.group("marker") or "").strip(),
        source="header",
    )


def script_dependencies(code: str) -> tuple[list[str], list[DependencyIssue]]:
    """The ``dependencies`` of a ``# /// script`` block (PEP 723), with problems found in it."""
    issues: list[DependencyIssue] = []
    blocks = [match for match in _SCRIPT_BLOCK.finditer(code) if match.group("type") == "script"]
    if not blocks:
        return [], issues
    if len(blocks) > 1:
        issues.append(DependencyIssue(code="deps.header_multiple", message="More than one '# /// script' block"))
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:] for line in blocks[0].group("content").splitlines(keepends=True)
    )
    line_number = code[: blocks[0].start()].count("\n") + 1
    try:
        try:
            tomllib = importlib.import_module("tomllib")  # Python 3.11+
            data = tomllib.loads(content)
            dependencies = data.get("dependencies", [])
        except ModuleNotFoundError:  # Python 3.10: the block is simple TOML; read the one key we need
            match = re.search(r"(?ms)^dependencies\s*=\s*(\[.*?\])\s*$", content)
            if match is None and re.search(r"(?m)^dependencies\s*=", content):
                raise ValueError("'dependencies' is not a closed list") from None
            dependencies = ast.literal_eval(match.group(1)) if match else []
    except Exception as exc:  # any syntax error in the block
        issues.append(
            DependencyIssue(
                code="deps.header_invalid",
                message=f"The '# /// script' block is not valid TOML: {exc}",
                params={"error": str(exc)[:200]},
                line=line_number,
            )
        )
        return [], issues
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        issues.append(
            DependencyIssue(
                code="deps.header_invalid",
                message="'dependencies' must be a list of strings",
                params={"error": "dependencies must be a list of strings"},
                line=line_number,
            )
        )
        return [], issues
    return dependencies, issues


def _handles_import_error(handler: ast.ExceptHandler) -> bool:
    names: list[ast.expr] = []
    if handler.type is None:
        return True
    names = list(handler.type.elts) if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(name, ast.Name) and name.id in ("ImportError", "ModuleNotFoundError") for name in names)


class _Imports(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found: list[tuple[str, int, bool]] = []
        self.dynamic: list[int] = []
        self._optional = 0

    def visit_Try(self, node: ast.Try) -> None:
        optional = any(_handles_import_error(handler) for handler in node.handlers)
        self._optional += optional
        for statement in node.body:
            self.visit(statement)
        self._optional -= optional
        for part in (*node.handlers, *node.orelse, *node.finalbody):
            self.visit(part)

    visit_TryStar = visit_Try

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.found.append((alias.name, node.lineno, self._optional > 0))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            self.found.append((node.module, node.lineno, self._optional > 0))

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        name = (
            function.attr
            if isinstance(function, ast.Attribute)
            else function.id
            if isinstance(function, ast.Name)
            else ""
        )
        if name in ("import_module", "__import__"):
            self.dynamic.append(node.lineno)
        self.generic_visit(node)


def _modules(sources: Iterable[str]) -> tuple[list[ImportedModule], list[DependencyIssue]]:
    modules: dict[str, ImportedModule] = {}
    issues: list[DependencyIssue] = []
    for source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue  # the static check reports syntax errors
        visitor = _Imports()
        visitor.visit(tree)
        for line in visitor.dynamic:
            issues.append(
                DependencyIssue(
                    code="deps.dynamic_import",
                    message=f"Line {line}: a module imported by name at run time cannot be resolved",
                    params={"line": line},
                    line=line,
                )
            )
        for dotted, line, optional in visitor.found:
            root = dotted.split(".")[0]
            # Some packages are known by a dotted name (google.protobuf).
            key = dotted if dotted in MODULE_PACKAGES else root
            kind = classify(key)
            existing = modules.get(key)
            if existing is not None:
                existing.optional = existing.optional and optional
                continue
            modules[key] = ImportedModule(name=root, kind=kind, line=line, optional=optional, package=package_for(key))
    return list(modules.values()), issues


def analyze(code: str, tests: str = "") -> ToolDependencies:
    """The packages a tool needs, from its imports and its ``# /// script`` header."""
    modules, issues = _modules([code, tests])
    declared_lines, header_issues = script_dependencies(code)
    issues += header_issues
    requirements: dict[str, Requirement] = {}
    for text in declared_lines:
        requirement = parse_requirement(text)
        if requirement is None:
            issues.append(
                DependencyIssue(
                    code="deps.requirement_invalid",
                    message=f"{text!r} is not a valid package requirement (name and optional version only)",
                    params={"requirement": text},
                )
            )
            continue
        if requirement.key in {normalize(name) for name in RUNTIME_PACKAGES}:
            continue  # always installed in the version Nodestro needs
        requirements.setdefault(requirement.key, requirement)

    unresolved: list[ImportedModule] = []
    for module in modules:
        if module.kind == "unresolved":
            # A declared package with the module's own name provides it.
            declared = requirements.get(normalize(module.name))
            if declared is not None:
                module.kind, module.package = "third_party", declared.name
                declared.modules.append(module.name)
            else:
                unresolved.append(module)
            continue
        if module.kind != "third_party" or module.package is None:
            continue
        key = normalize(module.package)
        if key in requirements:
            requirements[key].modules.append(module.name)
        elif not module.optional:
            requirements[key] = Requirement(name=module.package, key=key, modules=[module.name], source="import")

    # Declared packages no import matched: they may provide the unknown modules. That is checked
    # after the installation; without such a declaration the module stays unresolved.
    spare = [item for item in requirements.values() if item.source == "header" and not item.modules]
    for module in unresolved:
        if spare and not module.optional:
            module.kind = "third_party"
            module.package = spare[0].name if len(spare) == 1 else None
            if len(spare) == 1:
                spare[0].modules.append(module.name)
            issues.append(
                DependencyIssue(
                    code="deps.module_assumed",
                    message=f"Module {module.name!r} is assumed to come from the declared packages; "
                    "this is verified after the installation",
                    params={"module": module.name, "packages": ", ".join(item.name for item in spare)},
                    line=module.line,
                )
            )
        elif not module.optional:
            issues.append(
                DependencyIssue(
                    code="deps.module_unknown",
                    message=f"No package is known for module {module.name!r}; declare it in a '# /// script' "
                    'block, e.g. # dependencies = ["package-name"]',
                    params={"module": module.name},
                    line=module.line,
                )
            )
    return ToolDependencies(
        requirements=sorted(requirements.values(), key=lambda item: item.key), modules=modules, issues=issues
    )


def blocking(dependencies: ToolDependencies) -> list[DependencyIssue]:
    """Issues that must be solved before anything is resolved or installed."""
    return [issue for issue in dependencies.issues if issue.code != "deps.module_assumed"]
