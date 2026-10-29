"""Remove or pseudonymize sensitive information in a copy of a run trace.

Used for the privacy-friendly trace export. It works on a JSON copy of the run; the
stored run is never touched. Detection is pattern-based and cannot be complete:

- **Credentials** (always): API keys and tokens of common services, bearer tokens,
  private keys, ``password=...``-style assignments, and the values of JSON keys that
  name a secret (``api_key``, ``authorization``, ``password``, ...).
- **Personal data** (optional): e-mail addresses, phone numbers, IBANs, payment card
  numbers (Luhn-checked) and IP addresses. Equal values get the same placeholder
  (``[EMAIL_1]``), so a trace stays readable.
- **Contents** (optional): every free text a user or a model wrote is replaced by a
  note of its length: input, prompts, system prompts, answers, thinking, tool inputs
  and results, memory files and judge feedback. Structure, timing, models, tokens and
  costs stay.
"""

from __future__ import annotations

from collections import Counter
import re
from typing import Any

from .schemas import SanitizeReport

SECRET_KEY = re.compile(
    r"(api[_-]?key|apikey|authorization|auth[_-]?token|access[_-]?token|refresh[_-]?token|token|secret|"
    r"client[_-]?secret|password|passwd|passwort|pwd|cookie|session[_-]?id|private[_-]?key|x-api-key)$",
    re.IGNORECASE,
)

_SECRETS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}")),
    ("api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("api_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("api_key", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("api_key", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b")),
    ("api_key", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("token", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{16,}=*")),
]
# "password: hunter2", "api_key=abc", "Passwort = geheim": the name stays, the value goes.
_ASSIGNMENT = re.compile(
    r"(?i)([\"']?)\b(password|passwort|passwd|pwd|secret|api[_-]?key|token|access[_-]?key|client[_-]?secret)\1"
    r"(\s*[:=]\s*)([\"']?)([^\s\"',;}]{4,})\4"
)
_PERSONAL: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")),
    ("card", re.compile(r"\b(?:\d[ \-]?){12,18}\d\b")),
    ("phone", re.compile(r"(?<![\w.])(?:\+|00)\d{1,3}[ \-/]?(?:\(?\d{1,5}\)?[ \-/]?){2,5}\d{2,}(?![\w.])")),
    ("phone", re.compile(r"(?<![\w.])0\d{2,5}[ \-/]\d{3,}(?:[ \-]\d{2,})*(?![\w.])")),
    ("ip", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
]

#: Run fields that hold free text, removed in the "contents" mode (paths in the run JSON).
_RUN_TEXT = ("input", "output", "error")
_NODE_TEXT = ("prompt", "output", "text", "thinking", "error")
_AGENT_TEXT = ("system_prompt", "task_template", "description")


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


class Sanitizer:
    """Replaces sensitive text; one instance per export keeps placeholders consistent."""

    def __init__(self, *, personal_data: bool = True, contents: bool = False) -> None:
        self.personal_data = personal_data
        self.contents = contents
        self.counts: Counter[str] = Counter()
        self._pseudonyms: dict[tuple[str, str], str] = {}
        #: Credentials found so far; they are removed wherever else they appear (e.g. in error texts).
        self._known: set[str] = set()

    def _placeholder(self, kind: str, value: str) -> str:
        self.counts[kind] += 1
        if kind in ("secret", "api_key", "token") and len(value) >= 4:
            self._known.add(value)
        key = (kind, value)
        if key not in self._pseudonyms:
            number = sum(1 for known in self._pseudonyms if known[0] == kind) + 1
            self._pseudonyms[key] = f"[{kind.upper()}_{number}]"
        return self._pseudonyms[key]

    def text(self, value: str) -> str:
        """Credentials (and personal data, if enabled) in one text."""
        for kind, pattern in _SECRETS:

            def secret(match: re.Match[str], kind: str = kind) -> str:
                return self._placeholder(kind, match.group(0))

            value = pattern.sub(secret, value)

        def assignment(match: re.Match[str]) -> str:
            name_quote, name, separator, quote, secret = match.groups()
            if secret.startswith("[") and secret.endswith("]"):
                return match.group(0)  # already replaced
            replaced = self._placeholder("secret", secret)
            return f"{name_quote}{name}{name_quote}{separator}{quote}{replaced}{quote}"

        value = _ASSIGNMENT.sub(assignment, value)
        if self.personal_data:
            for kind, pattern in _PERSONAL:

                def personal(match: re.Match[str], kind: str = kind) -> str:
                    found = match.group(0)
                    if kind == "card" and not _luhn(re.sub(r"\D", "", found)):
                        return found
                    if kind == "phone" and len(re.sub(r"\D", "", found)) < 7:
                        return found
                    return self._placeholder(kind, found)

                value = pattern.sub(personal, value)
        return value

    def value(self, value: Any, *, key: str | None = None) -> Any:
        """Any JSON value: secret-named keys lose their value, strings are scanned."""
        if key is not None and SECRET_KEY.search(key) and isinstance(value, (str, int, float)) and value != "":
            return self._placeholder("secret", str(value))
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {name: self.value(item, key=str(name)) for name, item in value.items()}
        return value

    def known(self, value: Any) -> Any:
        """Replace every credential found so far wherever it appears again."""
        if isinstance(value, str):
            for secret in sorted(self._known, key=len, reverse=True):
                if secret in value:
                    kind = next((key[0] for key in self._pseudonyms if key[1] == secret), "secret")
                    value = value.replace(secret, self._placeholder(kind, secret))
            return value
        if isinstance(value, list):
            return [self.known(item) for item in value]
        if isinstance(value, dict):
            return {name: self.known(item) for name, item in value.items()}
        return value

    def removed(self, value: Any) -> Any:
        """The "contents" mode: free text becomes a note of its size."""
        if value is None or value == "" or value == {} or value == []:
            return value
        self.counts["content"] += 1
        size = len(value) if isinstance(value, str) else len(str(value))
        return f"[REMOVED: {size} characters]"


def sanitize_run(run: dict[str, Any], *, personal_data: bool = True, contents: bool = False) -> dict[str, Any]:
    """A sanitized copy of a run as JSON (``Run.model_dump(mode="json")``), with its report."""
    sanitizer = Sanitizer(personal_data=personal_data, contents=contents)
    copy: dict[str, Any] = sanitizer.known(sanitizer.value(run))
    if contents:
        for field in _RUN_TEXT:
            copy[field] = sanitizer.removed(copy.get(field))
        for state in (copy.get("nodes") or {}).values():
            for field in _NODE_TEXT:
                state[field] = sanitizer.removed(state.get(field))
            for call in state.get("tool_calls") or []:
                call["input"] = {name: sanitizer.removed(item) for name, item in (call.get("input") or {}).items()}
                call["output"] = sanitizer.removed(call.get("output"))
            if state.get("memory"):
                state["memory"] = {path: sanitizer.removed(text) for path, text in state["memory"].items()}
            if state.get("evaluation"):
                state["evaluation"]["feedback"] = sanitizer.removed(state["evaluation"].get("feedback"))
        for node in (copy.get("workflow") or {}).get("nodes") or []:
            agent = node.get("agent")
            if agent:
                for field in _AGENT_TEXT:
                    agent[field] = sanitizer.removed(agent.get(field))
                agent["messages"] = [
                    {**message, "content": sanitizer.removed(message.get("content"))}
                    for message in agent.get("messages") or []
                ]
            if node.get("start"):
                node["start"]["default_input"] = sanitizer.removed(node["start"].get("default_input"))
    report = SanitizeReport(
        personal_data=personal_data, contents=contents, replaced=dict(sorted(sanitizer.counts.items()))
    )
    copy["sanitized"] = report.model_dump(mode="json")
    return copy


def redact_secrets(value: Any) -> Any:
    """Only credentials, e.g. for showing request options in the context preview."""
    return Sanitizer(personal_data=False).value(value)
