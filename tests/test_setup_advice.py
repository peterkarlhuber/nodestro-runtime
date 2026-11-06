"""Setup advice depends on where the runtime runs (RT-057): an exported project has no settings, so a missing key
or endpoint is advised as an environment variable or a line of the ``.env`` file."""

from __future__ import annotations

import pytest

from nodestro_runtime.providers import MissingApiKeyError, environment_connection


def test_the_studio_advises_the_settings() -> None:
    error = MissingApiKeyError("anthropic")
    assert error.code == "api_key_missing"
    assert "enter it in the settings" in str(error)
    assert "where" not in error.params


def test_an_exported_project_advises_the_environment() -> None:
    with pytest.raises(MissingApiKeyError) as caught:
        environment_connection("anthropic", environ={})
    error = caught.value
    assert error.code == "api_key_missing"
    assert "ANTHROPIC_API_KEY" in str(error) and ".env" in str(error) and "settings" not in str(error)
    assert error.params["where"] == "environment"
    assert "ANTHROPIC_API_KEY" in error.params["variables"]


def test_an_exported_endpoint_advises_its_base_url_variable() -> None:
    with pytest.raises(MissingApiKeyError) as caught:
        environment_connection("compatible", environ={})
    error = caught.value
    assert error.code == "provider.not_configured"
    assert "COMPATIBLE_BASE_URL" in str(error) and ".env" in str(error)
    assert error.params == {"provider": "compatible", "where": "environment", "variables": ["COMPATIBLE_BASE_URL"]}
