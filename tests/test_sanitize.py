"""Sanitizing secrets and personal data (copies of runs, result files, exports)."""

from __future__ import annotations

import pytest

from nodestro_runtime.sanitize import Sanitizer, sanitize_run


@pytest.mark.parametrize(
    ("raw", "hidden"),
    [
        ("Key sk-ant-api03-abcdefghijklmnopqrstuvwx", "sk-ant-api03"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123", "abcdefghijklmnop"),
        ("password = hunter2geheim", "hunter2geheim"),
        ("Passwort: Sommer2026!", "Sommer2026"),
        ("Mail an anna.schmidt@example.com bitte", "anna.schmidt"),
        ("Ruf +49 170 1234567 an", "1234567"),
        ("IBAN DE89 3704 0044 0532 0130 00", "0532"),
        ("Karte 4111 1111 1111 1111", "4111 1111"),
        ("Server 192.168.10.24", "192.168.10.24"),
    ],
)
def test_sanitizer_finds_secrets_and_personal_data(raw: str, hidden: str) -> None:
    assert hidden not in Sanitizer().text(raw)


def test_sanitizer_keeps_ordinary_text_and_numbers() -> None:
    sanitizer = Sanitizer()
    for value in ("Umsatz 2026: 1.234.567 Euro", "Version 1.2.3", "am 2026-10-27T21:40:43Z", "Bestellnummer 12345"):
        assert sanitizer.text(value) == value
    assert sanitizer.value({"input_tokens": 5, "max_tokens": 10}) == {"input_tokens": 5, "max_tokens": 10}


def test_equal_values_get_equal_placeholders() -> None:
    sanitizer = Sanitizer()
    first = sanitizer.text("a@example.com und b@example.com und a@example.com")
    assert first == "[EMAIL_1] und [EMAIL_2] und [EMAIL_1]"


def test_sanitize_run_without_personal_data_keeps_emails() -> None:
    run = {"input": "a@example.com sk-ant-api03-abcdefghijklmnopqrst", "nodes": {}, "workflow": {"nodes": []}}
    copy = sanitize_run(run, personal_data=False)
    assert copy["input"].startswith("a@example.com [API_KEY_1]")
    assert run["input"].startswith("a@example.com sk-ant")
