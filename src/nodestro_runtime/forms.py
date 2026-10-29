"""Input forms of workflows: typed fields instead of (or next to) the free-text input.

A form is optional and lives on the start node (``StartConfig.fields``). When a run
starts, :func:`prepare` checks the entered values (required fields, types, choices),
converts them (numbers, dates, booleans) and builds two things from them:

- ``values``: the typed values, stored with the run and passed on as the start node's
  result, so decision nodes can compare them and agents receive them as clear JSON;
- the run input text: one ``label: value`` line per field, followed by the free text if
  one was given. ``{{run.input}}`` keeps working, ``{{form.<name>}}`` names one value.

File fields hold ids of input files uploaded for the run (``in_…``, see
:mod:`nodestro_runtime.input_files`) or, as before, of knowledge files; they are added to the
run's files, so agents fed by the start node receive them (:mod:`nodestro_runtime.file_delivery`).
A file field checks the accepted formats, one or several files, the number of files and
their size.

Every field is either required or optional. A missing optional value is stored as
``null`` (``None``): placeholders become empty, decision nodes can test it with "is
empty", and a missing optional file never becomes a file reference. Workflows without a
form are unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import re
from typing import Any, Literal

from pydantic import BaseModel

from .file_formats import DEFAULT_FIELD_MB, FORMATS, accept_known, accepted
from .problems import NodestroError
from .schemas import InputField

FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_TEXT = 20_000


class FieldError(BaseModel):
    field: str
    code: str
    message: str
    params: dict[str, Any] = {}


class FormError(NodestroError):
    """The entered values do not fit the form."""


class FileRef(BaseModel):
    """What a form needs to know about a referenced file."""

    name: str
    #: The format key (``pdf`` ...); ``None`` when unknown (older knowledge files).
    format: str | None = None
    size: int | None = None
    source: Literal["input", "knowledge"] = "knowledge"


#: Known files: a set of ids (existence only), or id -> :class:`FileRef` (formats and sizes too) or name.
KnownFiles = set[str] | Mapping[str, FileRef] | Mapping[str, str]


def name_of(value: FileRef | str | None, fallback: str) -> str:
    if isinstance(value, FileRef):
        return value.name
    return value if isinstance(value, str) and value else fallback


class Prepared(BaseModel):
    input: str
    values: dict[str, Any]
    file_ids: list[str]


def field_label(field: InputField) -> str:
    return field.label.strip() or field.name


def _coerce(field: InputField, raw: Any) -> tuple[Any, FieldError | None]:
    """The typed value of one field, or the reason it is invalid."""

    def error(code: str, message: str, **params: Any) -> tuple[Any, FieldError]:
        return None, FieldError(field=field.name, code=code, message=message, params=params)

    if field.type in ("text", "textarea"):
        text = "" if raw is None else str(raw)
        if len(text) > _MAX_TEXT:
            return error("form.too_long", f"{field_label(field)} is too long", limit=_MAX_TEXT)
        return text, None
    if field.type == "number":
        if isinstance(raw, bool):
            return error("form.not_number", f"{field_label(field)} must be a number")
        if isinstance(raw, (int, float)):
            return raw, None
        text = str(raw).strip().replace(",", ".")
        try:
            number = float(text)
        except ValueError:
            return error("form.not_number", f"{field_label(field)} must be a number")
        return int(number) if number.is_integer() and "." not in text else number, None
    if field.type == "date":
        if isinstance(raw, date):
            return raw.isoformat(), None
        try:
            return date.fromisoformat(str(raw).strip()[:10]).isoformat(), None
        except ValueError:
            return error("form.not_date", f"{field_label(field)} must be a date (YYYY-MM-DD)")
    if field.type == "boolean":
        if isinstance(raw, bool):
            return raw, None
        text = str(raw).strip().casefold()
        if text in ("true", "1", "yes", "ja", "on", "sí", "si", "oui", "sim"):
            return True, None
        if text in ("false", "0", "no", "nein", "off", "non", "não", "nao"):
            return False, None
        return error("form.not_boolean", f"{field_label(field)} must be yes or no")
    if field.type == "select":
        text = str(raw).strip()
        for option in field.options:
            if option.casefold() == text.casefold():
                return option, None
        return error("form.not_option", f"{field_label(field)}: {text!r} is not one of the choices", value=text)
    if field.type == "file":
        ids = raw if isinstance(raw, list) else [raw]
        found = [str(item.get("id") if isinstance(item, dict) else item).strip() for item in ids]
        return list(dict.fromkeys(item for item in found if item)), None
    return error("form.unknown_type", f"Unknown field type {field.type}")  # pragma: no cover - Literal


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def file_problems(field: InputField, ids: list[str], known_files: KnownFiles | None) -> list[FieldError]:
    """Problems of the files of one file field: missing files, formats, number and size."""
    label = field_label(field)
    found: list[FieldError] = []

    def add(code: str, message: str, **params: Any) -> None:
        found.append(FieldError(field=field.name, code=code, message=message, params=params))

    limit = 1 if not field.multiple else field.max_files
    if limit is not None and len(ids) > limit:
        add("form.too_many_files", f"{label}: at most {limit} files", limit=limit, count=len(ids))
    if known_files is None:
        return found
    missing = [item for item in ids if item not in known_files]
    if missing:
        add("form.file_missing", f"{label}: {len(missing)} files no longer exist", count=len(missing))
    if not isinstance(known_files, Mapping):
        return found
    allowed = accepted(field.accept)
    max_mb = field.max_size_mb or DEFAULT_FIELD_MB
    for item in ids:
        ref = known_files.get(item)
        if not isinstance(ref, FileRef):
            continue
        if field.accept and ref.format is not None and ref.format not in allowed:
            add(
                "form.file_type",
                f"{label}: {ref.name} is not an accepted file type",
                name=ref.name,
                accepted=", ".join(sorted(FORMATS[key].extensions[0] for key in allowed)),
            )
        if ref.source == "input" and ref.size is not None and ref.size > max_mb * 1024 * 1024:
            add(
                "form.file_too_large", f"{label}: {ref.name} is larger than {max_mb} MB", name=ref.name, limit_mb=max_mb
            )
    return found


def check_values(
    fields: list[InputField], values: dict[str, Any], *, known_files: KnownFiles | None = None
) -> tuple[dict[str, Any], list[FieldError]]:
    """Typed values (defaults filled in) and every problem; unknown keys are ignored.

    A missing optional field is ``None`` in the typed values, so later steps can tell it is missing.
    """
    typed: dict[str, Any] = {}
    errors: list[FieldError] = []
    for field in fields:
        raw = values.get(field.name)
        if _blank(raw):
            raw = field.default
        if _blank(raw):
            if field.required:
                errors.append(
                    FieldError(field=field.name, code="form.required", message=f"{field_label(field)} is required")
                )
            else:
                typed[field.name] = None
            continue
        value, problem = _coerce(field, raw)
        if problem is not None:
            errors.append(problem)
            continue
        if field.type == "file":
            problems = file_problems(field, value, known_files)
            if problems:
                errors.extend(problems)
                continue
            if not value:
                if field.required:
                    errors.append(
                        FieldError(field=field.name, code="form.required", message=f"{field_label(field)} is required")
                    )
                else:
                    typed[field.name] = None
                continue
        typed[field.name] = value
    return typed, errors


def file_ids_of(fields: list[InputField], values: dict[str, Any]) -> list[str]:
    """The ids in the file fields of typed values, in field order."""
    found: list[str] = []
    for field in fields:
        value = values.get(field.name)
        if field.type == "file" and isinstance(value, list):
            found += [str(item) for item in value if str(item) not in found]
    return found


def _show(value: Any, file_names: Mapping[str, Any]) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(name_of(file_names.get(str(item)), str(item)) for item in value)
    return str(value)


#: What an agent's task shows for an optional field that was left empty.
NOT_PROVIDED = "(not provided)"


def prompt_values(
    fields: list[InputField], values: dict[str, Any], file_names: Mapping[str, Any], *, mark_empty: bool = True
) -> dict[str, Any]:
    """Form values as task placeholders show them: files by name (with their id), empty fields marked.

    The stored values keep the file ids and ``None``; decisions compare those. ``mark_empty=False``
    keeps empty fields empty (for values passed on to a sub-workflow's form).
    """
    shown = dict(values)
    for field in fields:
        value = values.get(field.name)
        if value is None or value == []:
            if mark_empty:
                shown[field.name] = NOT_PROVIDED
        elif field.type == "file" and isinstance(value, list):
            shown[field.name] = ", ".join(f"{name_of(file_names.get(str(item)), str(item))} [{item}]" for item in value)
    return shown


def input_text(fields: list[InputField], values: dict[str, Any], free_text: str, file_names: Mapping[str, Any]) -> str:
    """The run input as text: ``label: value`` per filled field, then the free text."""
    lines = []
    for field in fields:
        if values.get(field.name) is None:
            continue
        shown = _show(values[field.name], file_names)
        if "\n" in shown:
            lines.append(f"{field_label(field)}:\n{shown}")
        else:
            lines.append(f"{field_label(field)}: {shown}")
    text = "\n".join(lines)
    if free_text.strip():
        text = f"{text}\n\n{free_text.strip()}" if text else free_text.strip()
    return text


def prepare(
    fields: list[InputField],
    values: dict[str, Any],
    free_text: str,
    file_ids: list[str],
    *,
    file_names: Mapping[str, str] | Mapping[str, FileRef] | None = None,
) -> Prepared:
    """Check the values of a run and build its input; raises :class:`FormError` on invalid values.

    ``file_names`` are the known files (id -> name, or id -> :class:`FileRef` to check formats and sizes).
    """
    if not fields:
        return Prepared(input=free_text, values={}, file_ids=list(file_ids))
    names: Mapping[str, Any] = file_names if file_names is not None else {}
    typed, errors = check_values(fields, values, known_files=file_names)
    if errors:
        raise FormError(
            "form.invalid",
            "; ".join(error.message for error in errors),
            errors=[error.model_dump() for error in errors],
            count=len(errors),
        )
    files = list(file_ids)
    files += [item for item in file_ids_of(fields, typed) if item not in files]
    return Prepared(input=input_text(fields, typed, free_text, names), values=typed, file_ids=files)


def field_problems(fields: list[InputField]) -> list[tuple[str, str, dict[str, Any]]]:
    """Configuration errors of a form: ``(code, English message, params)``."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for field in fields:
        if not FIELD_NAME.match(field.name):
            found.append(
                (
                    "form.field_name_invalid",
                    f"Field name {field.name!r} must be letters, digits and _",
                    {"name": field.name},
                )
            )
        elif field.name.casefold() in seen:
            found.append(("form.field_duplicate", f"Two fields are called {field.name}", {"name": field.name}))
        seen.add(field.name.casefold())
        if field.type == "select" and not [option for option in field.options if option.strip()]:
            found.append(("form.select_no_options", f"Field {field.name} has no choices", {"name": field.name}))
        if field.type == "file":
            unknown = [item for item in field.accept if not accept_known(item)]
            if unknown:
                found.append(
                    (
                        "form.accept_unknown",
                        f"Field {field.name} accepts no known file type: {', '.join(unknown)}",
                        {"name": field.name, "types": ", ".join(unknown)},
                    )
                )
        if not _blank(field.default) and field.type != "file":
            _, problem = _coerce(field, field.default)
            if problem is not None:
                found.append(
                    ("form.default_invalid", f"The default of {field.name} does not fit its type", {"name": field.name})
                )
    return found
