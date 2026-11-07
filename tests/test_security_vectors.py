"""Shared security test vectors: Nodestro's egress check and credential redaction (MUP-ONA MP-A8, VB-7).

``tests/vectors/security-vectors.json`` is the canonical corpus (maintenance: ``tests/vectors/README.md``). Axentra runs
the same vectors against its webhook address check, OIDC client and redaction from a checksummed copy, nodestro-api the
JWT vectors against its OIDC client; this repository has no ID token check of its own, so it runs the egress and
redaction vectors. A vector a platform judges differently is listed in ``known_differences`` with its reason, and the
test asserts the listed verdict: a difference that disappears fails until its entry is removed.

Egress: an ``address`` goes to :func:`~nodestro_runtime.mcp.netguard.address_problem`; a ``url`` goes the way an MCP
server address does when it is saved and connected (``EgressPolicy.check_url``, ``url_secret_problem``, then
``GuardedBackend.resolve`` with a fake resolver). Redaction: :func:`~nodestro_runtime.sanitize.redact_secrets`.
"""

from __future__ import annotations

import asyncio
from collections import Counter
import ipaddress
import json
from pathlib import Path
import socket
from typing import Any

import pytest

from nodestro_runtime.mcp.netguard import EgressPolicy, GuardedBackend, address_problem, url_secret_problem
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.sanitize import redact_secrets

PLATFORM = "nodestro"
CORPUS_PATH = Path(__file__).parent / "vectors" / "security-vectors.json"
CORPUS: dict[str, Any] = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
CATEGORIES = ("egress", "jwt", "redaction")
#: What this repository checks: no ID token check lives in the runtime (nodestro-api runs the jwt vectors).
CHECKED = ("egress", "redaction")
VERDICTS = {"egress": {"block", "allow"}, "jwt": {"accept", "reject"}, "redaction": {"not_redacted"}}


DIFFERENCES = {
    (item["category"], item["id"]): item for item in CORPUS["known_differences"] if item["platform"] == PLATFORM
}


def _vectors(category: str) -> list[Any]:
    return [pytest.param(vector, id=vector["id"]) for vector in CORPUS[category]]


# ---- the corpus itself ---------------------------------------------------------------------------------------------


# MUP-ONA T-VEC-1
def test_the_corpus_is_well_formed() -> None:
    assert isinstance(CORPUS["version"], int) and CORPUS["version"] >= 1
    ids = Counter(vector["id"] for category in CATEGORIES for vector in CORPUS[category])
    assert [name for name, count in ids.items() if count > 1] == []
    for vector in CORPUS["egress"]:
        assert ("address" in vector) != ("url" in vector), vector["id"]
        assert vector["expect"] in VERDICTS["egress"] and vector["note"], vector["id"]
    for vector in CORPUS["jwt"]:
        assert {"header", "claims", "key_ref", "expect"} <= vector.keys(), vector["id"]
        assert vector["key_ref"] in CORPUS["jwt_context"]["key_refs"], vector["id"]
        assert vector["expect"] in VERDICTS["jwt"], vector["id"]
    for vector in CORPUS["redaction"]:
        assert isinstance(vector["input"], (str, dict)) and vector["must_not_contain"], vector["id"]
    # Every category has cases that must pass and cases that must be refused.
    assert {vector["expect"] for vector in CORPUS["egress"]} == VERDICTS["egress"]
    assert {vector["expect"] for vector in CORPUS["jwt"]} == VERDICTS["jwt"]


# MUP-ONA T-VEC-1
def test_every_known_difference_names_a_vector_and_says_why() -> None:
    known = {(category, vector["id"]): vector for category in CATEGORIES for vector in CORPUS[category]}
    seen = set()
    for item in CORPUS["known_differences"]:
        key = (item["category"], item["id"], item["platform"])
        assert key not in seen, key
        seen.add(key)
        assert (item["category"], item["id"]) in known, item
        assert item["platform"] in ("nodestro", "axentra"), item
        assert item["actual"] in VERDICTS[item["category"]], item
        assert item["actual"] != known[(item["category"], item["id"])].get("expect"), item
        assert len(item["reason"]) > 30, item


# ---- egress --------------------------------------------------------------------------------------------------------


def _resolver(vector: dict[str, Any]) -> Any:
    async def resolve(host: str, port: int) -> list[str]:
        if "resolves_to" in vector:
            return list(vector["resolves_to"])
        try:  # the C library's reading of a numeric host (2130706433, 0x7f.1, 0177.0.0.1, 127.1)
            return [str(ipaddress.IPv4Address(socket.inet_aton(host)))]
        except OSError:
            raise AssertionError(f"{vector['id']}: the host {host} needs resolves_to") from None

    return resolve


def nodestro_egress(vector: dict[str, Any]) -> str:
    """``block`` or ``allow``: Nodestro's verdict on an egress vector."""
    if "address" in vector:
        return "block" if address_problem(ipaddress.ip_address(vector["address"])) is not None else "allow"
    url = vector["url"]
    try:
        _scheme, host, port = EgressPolicy().check_url(url)
        if url_secret_problem(url) is not None:
            return "block"
        asyncio.run(GuardedBackend(resolver=_resolver(vector)).resolve(host, port))
    except NodestroError:
        return "block"
    return "allow"


# MUP-ONA T-VEC-1
@pytest.mark.parametrize("vector", _vectors("egress"))
def test_egress_vector(vector: dict[str, Any]) -> None:
    difference = DIFFERENCES.get(("egress", vector["id"]))
    expected = difference["actual"] if difference else vector["expect"]
    assert nodestro_egress(vector) == expected, vector["note"]


# ---- redaction -----------------------------------------------------------------------------------------------------


def nodestro_redacted(value: Any) -> str:
    """The credential redaction's result for ``value``, as text."""
    out = redact_secrets(value)
    return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


# MUP-ONA T-VEC-1
@pytest.mark.parametrize("vector", _vectors("redaction"))
def test_redaction_vector(vector: dict[str, Any]) -> None:
    out = nodestro_redacted(vector["input"])
    left = [secret for secret in vector["must_not_contain"] if secret in out]
    if ("redaction", vector["id"]) in DIFFERENCES:
        assert left, f"{vector['id']} is redacted now: remove its known_differences entry for {PLATFORM}"
    else:
        assert left == [], out


# MUP-ONA T-VEC-1
def test_known_differences_of_this_platform_are_checked_here() -> None:
    assert {category for category, _ in DIFFERENCES} <= set(CHECKED)
