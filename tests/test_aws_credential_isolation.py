"""AWS credentials (Amazon Bedrock) never reach tool code, package builds, sandboxes or MCP servers.

Nodestro's own process may hold AWS credentials (the desktop's default chain or profile, a server operator's base
credentials). Every child process that runs code Nodestro did not write gets an empty or allowlisted environment;
these tests set every AWS variable boto3 reads and check each path's child environment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

from orkexa.dynamic import DockerSandbox, SubprocessSandbox
import pytest

from nodestro_runtime import builtin_sandbox, python_envs
from nodestro_runtime.builtin_sandbox import LinuxSandbox
from nodestro_runtime.container_images import ContainerImages, RuntimeImageSandbox, sandbox_run_args
from nodestro_runtime.mcp import stdio
from nodestro_runtime.python_envs import SANDBOX_ENV, UvRunner, uv_environment
from nodestro_runtime.sandbox_policy import HOST_AWS_ENV
from nodestro_runtime.tool_library import make_sandbox

#: Every variable boto3/botocore reads credentials, roles, profiles or credential files from.
AWS_SECRETS = {
    "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "AWS_SESSION_TOKEN": "IQoJb3JpZ2luX2VjEXAMPLETOKEN",
    "AWS_SECURITY_TOKEN": "IQoJb3JpZ2luX2VjEXAMPLETOKEN",
    "AWS_PROFILE": "bedrock-admin",
    "AWS_DEFAULT_PROFILE": "bedrock-admin",
    "AWS_BEARER_TOKEN_BEDROCK": "ABSKQmVkcm9ja0FQSUtleUVYQU1QTEU=",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.2/creds",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN": "container-token",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": "/var/run/secrets/token",
    "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/secrets/eks/token",
    "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/operator",
    "AWS_ROLE_SESSION_NAME": "nodestro",
    "AWS_SHARED_CREDENTIALS_FILE": "/home/someone/.aws/credentials",
    "AWS_CONFIG_FILE": "/home/someone/.aws/config",
    "AWS_REGION": "eu-central-1",
    "AWS_DEFAULT_REGION": "eu-central-1",
    "AWS_ENDPOINT_URL": "https://attacker.example",
    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": "https://attacker.example",
    "NODESTRO_BEDROCK_TENANT_ROLES": '{"t1": {"role_arn": "arn:aws:iam::123456789012:role/t1", "external_id": "x"}}',
}
#: The values that must never appear in a child environment (HOST_AWS_ENV's own settings are allowed).
SECRET_VALUES = {value for value in AWS_SECRETS.values()}


@pytest.fixture(autouse=True)
def aws_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in AWS_SECRETS.items():
        monkeypatch.setenv(key, value)


def _assert_clean(env: dict[str, str] | None) -> None:
    assert env is not None, "the child would inherit Nodestro's whole environment"
    leaked = {key for key, value in env.items() if value in SECRET_VALUES}
    assert not leaked, leaked
    aws = {key for key in env if key.upper().startswith("AWS_") or key.startswith("NODESTRO_BEDROCK")}
    assert aws <= set(HOST_AWS_ENV), aws - set(HOST_AWS_ENV)
    for key in aws:
        assert env[key] == HOST_AWS_ENV[key]


_ENV_TOOL = (
    "def environment() -> str:\n"
    '    """The variables of the process.\n\n    Returns:\n        JSON of the environment.\n    """\n'
    "    import json, os\n"
    "    return json.dumps(dict(os.environ))\n"
)


# ---- uv and the package builds it runs ---------------------------------------------------------------------


def test_uv_gets_no_aws_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _assert_clean(uv_environment())
    seen: dict[str, Any] = {}

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "uv 0.0.0", "")

    monkeypatch.setattr(python_envs.subprocess, "run", run)
    UvRunner(tmp_path, binary="/usr/bin/uv")._run(["--version"], log=lambda _line: None)
    _assert_clean(seen["env"])
    # An AWS SDK in a build script finds neither the user's credential files nor an instance metadata service.
    assert {key: seen["env"][key] for key in HOST_AWS_ENV} == HOST_AWS_ENV


# ---- the subprocess sandbox (host, tools with and without a Python environment) -----------------------------


@pytest.mark.parametrize(
    "sandbox",
    [
        pytest.param(lambda: make_sandbox("subprocess"), id="tool-library"),
        pytest.param(
            lambda: SubprocessSandbox(timeout=30.0, env=SANDBOX_ENV, acknowledge_unsafe=True), id="python-env"
        ),
    ],
)
def test_the_subprocess_sandbox_runs_tools_without_aws_credentials(sandbox: Any) -> None:
    result = sandbox().call(_ENV_TOOL, "environment", {})
    env = json.loads(result)
    _assert_clean(env)
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull and env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert "HOME" not in env


def test_the_python_environment_sandbox_uses_the_isolating_variables() -> None:
    assert {key: SANDBOX_ENV[key] for key in HOST_AWS_ENV} == HOST_AWS_ENV


# ---- the built-in Linux sandbox ---------------------------------------------------------------------------


def test_the_builtin_sandbox_starts_tools_with_an_empty_environment_and_no_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def popen(args: list[str], **kwargs: Any) -> Any:
        seen["args"], seen["env"] = args, kwargs.get("env")
        raise OSError("not started in this test")

    monkeypatch.setattr(builtin_sandbox.subprocess, "Popen", popen)
    sandbox = LinuxSandbox(env=dict(SANDBOX_ENV), protected=(str(tmp_path / "data"),))
    workdir = tmp_path / "call"
    workdir.mkdir()
    with pytest.raises(Exception, match="not started"):
        sandbox._execute(workdir, "{}")
    _assert_clean(seen["env"])
    assert seen["env"]["HOME"] == str(workdir / "tmp")  # never the user's home directory
    config = json.loads(seen["args"][-1])
    # The user's home directory (and so ~/.aws) is never readable; Landlock grants only the listed paths.
    assert str(Path.home()) in config["forbidden"]
    assert not any((Path.home() / ".aws").is_relative_to(path) for path in config["read"])


# ---- container sandboxes ------------------------------------------------------------------------------------


def test_container_sandboxes_pass_nothing_into_the_container(tmp_path: Path) -> None:
    images = ContainerImages(docker="docker")
    cli = images._cli_environment()
    assert not {key for key in cli if key.startswith("AWS_")}
    streamed = images.sandbox("nodestro-env:abc")
    runtime = RuntimeImageSandbox(lambda: "nodestro-runtime:abc", docker="docker", environ=cli)
    for sandbox in (streamed, runtime, DockerSandbox(image="orkexa-sandbox")):
        command = sandbox.command(tmp_path, "nodestro-sandbox-test")
        assert not {"-e", "--env", "--env-file"} & set(command), command
        assert not any(part.startswith(("--env=", "--env-file=")) for part in command)
        # No bind mount of the user's home directory (Orkexa's own sandbox mounts only the call and its package).
        assert not any(str(Path.home() / ".aws") in part for part in command)
    for sandbox in (streamed, runtime):
        assert sandbox.environ is not None and not {key for key in sandbox.environ if key.startswith("AWS_")}
    run = sandbox_run_args("docker", "n", "img", memory_mb=128, cpus=1.0, pids_limit=8)
    assert "-v" not in run and "--volume" not in run and "--mount" not in run


# ---- MCP servers over stdio ---------------------------------------------------------------------------------


def test_mcp_stdio_servers_get_no_aws_credentials() -> None:
    from mcp.client.stdio import get_default_environment

    cli = ContainerImages(docker="docker")._cli_environment()
    launch = stdio.launch(image="img", module="server", args=[], memory_mb=256, docker_env=cli)
    # The MCP SDK starts the relay with its default variables plus the launch's: this is the relay's environment.
    _assert_clean(get_default_environment() | launch.env)
    assert not {"-e", "--env", "--env-file"} & set(launch.args)


# ---- trace exports and previews -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "hidden"),
    [
        ("key ASIAIOSFODNN7EXAMPLE in use", "ASIAIOSFODNN7EXAMPLE"),
        ("key AKIAIOSFODNN7EXAMPLE in use", "AKIAIOSFODNN7EXAMPLE"),
        ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
        ("token IQoJb3JpZ2luX2VjEXAMPLETOKENabcdefghij", "IQoJb3JpZ2lu"),
        ("bedrock key ABSKQmVkcm9ja0FQSUtleUVYQU1QTEU=", "ABSKQmVk"),
        ("bedrock-api-key-YmVkcm9jay5hbWF6b25hd3MuY29t", "YmVkcm9jay5h"),
    ],
)
def test_exported_traces_hide_aws_credentials(raw: str, hidden: str) -> None:
    from nodestro_runtime.sanitize import Sanitizer

    assert hidden not in Sanitizer(personal_data=False).text(raw)
    redacted = Sanitizer(personal_data=False).value({"aws_secret_access_key": "plain-value-123"})
    assert redacted != {"aws_secret_access_key": "plain-value-123"}
