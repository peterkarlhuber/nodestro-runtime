"""What the Ollama registry says about a model before it is downloaded (RT-062, RT-064).

A model that is neither in Nodestro's catalog nor installed was known to lack a capability only after its download
(often gigabytes). Before a download the readiness check (:mod:`.local_readiness`) now reads the model's registry
manifest, its config and its prompt template (a few kilobytes, never the weights) and reports a capability the
model certainly lacks up front.

Only what the manifest proves is reported; everything else stays unknown and the check after the download decides:

- **tools** is certainly missing when the model has no built-in parser (``parser``/``renderer`` in its config, which
  Ollama uses for tool calls of e.g. ``qwen3.5``) and its prompt template never uses ``.Tools`` (or it has no
  template). That is how Ollama itself derives the capability.
- **vision** is present with a projector layer. Without one it is *not* reported missing: several architectures
  carry vision in the model file itself, which only its GGUF metadata shows.
- **thinking**, **completion** and **embedding** need the GGUF metadata or Ollama's built-in parsers: unknown.

Only names of Ollama's public registry are read (``qwen3:8b``, ``user/model:tag``, ``registry.ollama.ai/...``); a
model of another host (``hf.co/...``) stays unknown. Nothing is read in the strict offline mode or for a server's
operator-managed model server (the caller decides that). Every read is HTTPS, bounded in size and time, and checked
against the digest the manifest names; any failure means "unknown", never an error of the action. Results are kept
for an hour (failures for ten minutes) per process.

:func:`download_sizes` reads only the manifest: the size of every file of a download, so its progress can give a
remaining time for the whole download from its start (RT-064, :class:`~.local_progress.PullProgress`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any
import urllib.error
import urllib.request

from orkexa.local import OllamaError, check_model_name

logger = logging.getLogger(__name__)

REGISTRY = "registry.ollama.ai"
_MANIFEST_TYPES = "application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json"
_LAYER = "application/vnd.ollama.image."
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: Size limits of what is read: the manifest, the config and the template are small JSON and text files.
MAX_MANIFEST = 64 * 1024
MAX_CONFIG = 64 * 1024
MAX_TEMPLATE = 256 * 1024
#: Seconds per request; three requests at most per model.
TIMEOUT = 4.0
#: Seconds a result (or a failure) is kept.
TTL_FOUND = 3600.0
TTL_FAILED = 600.0
#: A template that uses the tool list (Go template field ``.Tools`` / ``$.Tools``), as Ollama's ``Template.Vars``.
_TOOLS_VAR = re.compile(r"\.Tools\b", re.IGNORECASE)

#: ``get(url, accept, limit)``: the body of an HTTPS GET, at most ``limit`` bytes (more is an error).
Getter = Callable[[str, str, int], bytes]


class RegistryError(Exception):
    """A registry read that did not give a usable answer (network, status, size, digest, format)."""


@dataclass(frozen=True)
class RegistryFacts:
    """What the registry manifest proves about a model (Ollama capability names)."""

    name: str
    #: Capabilities the model certainly has (``vision`` with a projector layer).
    present: frozenset[str]
    #: Capabilities the model certainly lacks (``tools``).
    absent: frozenset[str]


class _NoInsecureRedirect(urllib.request.HTTPRedirectHandler):
    """Blob downloads redirect to a CDN; only HTTPS targets are followed."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if not newurl.lower().startswith("https://"):
            raise RegistryError(f"refused a redirect to a non-HTTPS URL (status {code})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get(url: str, accept: str, limit: int) -> bytes:
    opener = urllib.request.build_opener(_NoInsecureRedirect())
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "nodestro-runtime"})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            body: bytes = response.read(limit + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RegistryError(f"{type(exc).__name__}: {str(exc)[:200]}") from None
    if len(body) > limit:
        raise RegistryError(f"answer larger than {limit} bytes")
    return body


#: The getter used for registry reads (tests replace it; nothing else should).
get: Getter = _http_get

_cache: dict[str, tuple[float, RegistryFacts | None]] = {}
_lock = threading.Lock()


def registry_path(name: str) -> tuple[str, str] | None:
    """(repository, tag) of ``name`` in Ollama's public registry, ``None`` for another host or an invalid name."""
    try:
        text = check_model_name(name)
    except OllamaError:
        return None
    last = text.rsplit("/", 1)[-1]
    base, tag = (text.rsplit(":", 1)) if ":" in last else (text, "latest")
    pieces = base.split("/")
    if len(pieces) == 3:  # host/namespace/model, as Ollama parses names
        if pieces[0].lower() != REGISTRY:
            return None  # another registry host (hf.co/..., a mirror)
        pieces = pieces[1:]
    if len(pieces) == 1:
        pieces = ["library", pieces[0]]
    if len(pieces) != 2 or not all(_PART.fullmatch(part) for part in (*pieces, tag)):
        return None
    return "/".join(pieces), tag


def _blob(repository: str, layer: dict[str, Any], limit: int) -> bytes:
    digest = layer.get("digest")
    size = layer.get("size")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise RegistryError("a layer without a valid digest")
    if not isinstance(size, int) or size < 0 or size > limit:
        raise RegistryError(f"layer {digest[:19]} is larger than {limit} bytes")
    body = get(f"https://{REGISTRY}/v2/{repository}/blobs/{digest}", "*/*", limit)
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise RegistryError(f"layer {digest[:19]} does not match its digest")
    return body


def _json(body: bytes, what: str) -> dict[str, Any]:
    try:
        found = json.loads(body)
    except ValueError:
        raise RegistryError(f"the {what} is not JSON") from None
    if not isinstance(found, dict):
        raise RegistryError(f"the {what} is not a JSON object")
    return found


def _manifest(name: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """(repository, layers, config layer) of ``name``'s registry manifest; :class:`RegistryError` if unusable."""
    path = registry_path(name)
    if path is None:
        raise RegistryError("not a model of Ollama's public registry")
    repository, tag = path
    manifest = _json(
        get(f"https://{REGISTRY}/v2/{repository}/manifests/{tag}", _MANIFEST_TYPES, MAX_MANIFEST), "manifest"
    )
    layers = manifest.get("layers")
    config_layer = manifest.get("config")
    if not isinstance(layers, list) or not all(isinstance(item, dict) for item in layers):
        raise RegistryError("the manifest has no layer list (a manifest list is not supported)")
    if not isinstance(config_layer, dict):
        raise RegistryError("the manifest has no config")
    return repository, layers, config_layer


def download_sizes(name: str) -> dict[str, int]:
    """The files Ollama downloads for ``name`` (every layer and the config) by digest with their sizes, from the
    registry manifest (one request, RT-064); :class:`RegistryError` if the registry cannot tell."""
    _, layers, config_layer = _manifest(name)
    sizes: dict[str, int] = {}
    for item in (*layers, config_layer):
        digest, size = item.get("digest"), item.get("size")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or not isinstance(size, int) or size < 0:
            raise RegistryError("a layer without a valid digest or size")
        sizes[digest] = size
    if not sizes:
        raise RegistryError("the manifest names no files")
    return sizes


def read_facts(name: str) -> RegistryFacts:
    """Read the facts of ``name`` from the registry now (no cache); :class:`RegistryError` if that fails."""
    repository, layers, config_layer = _manifest(name)
    kinds = {str(item.get("mediaType", "")).removeprefix(_LAYER) for item in layers}
    if "model" not in kinds:
        raise RegistryError("the manifest has no model layer")
    present: set[str] = set()
    absent: set[str] = set()
    if "projector" in kinds:
        present.add("vision")
    config = _json(_blob(repository, config_layer, MAX_CONFIG), "config")
    if not config.get("parser") and not config.get("renderer"):
        templates = [item for item in layers if item.get("mediaType") == _LAYER + "template"]
        uses_tools = False
        for layer in templates:
            text = _blob(repository, layer, MAX_TEMPLATE).decode("utf-8", errors="replace")
            uses_tools = uses_tools or bool(_TOOLS_VAR.search(text))
        (present if uses_tools else absent).add("tools")
    return RegistryFacts(name=name, present=frozenset(present), absent=frozenset(absent))


def registry_facts(name: str, *, now: Callable[[], float] = time.monotonic) -> RegistryFacts | None:
    """The cached facts of ``name`` (read once per hour), ``None`` when the registry could not tell."""
    key = name.strip()
    moment = now()
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] > moment:
            return cached[1]
    try:
        facts: RegistryFacts | None = read_facts(key)
    except RegistryError as exc:
        logger.info("The Ollama registry could not tell the capabilities of %s: %s", key[:120], exc)
        facts = None
    with _lock:
        if len(_cache) > 512:
            _cache.clear()
        _cache[key] = (moment + (TTL_FOUND if facts is not None else TTL_FAILED), facts)
    return facts


def clear_cache() -> None:
    with _lock:
        _cache.clear()


__all__ = [
    "REGISTRY",
    "RegistryError",
    "RegistryFacts",
    "clear_cache",
    "download_sizes",
    "read_facts",
    "registry_facts",
    "registry_path",
]
