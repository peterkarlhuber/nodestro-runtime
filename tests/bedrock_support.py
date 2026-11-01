"""Amazon Bedrock in the tests: stubbed boto3 clients, fake sessions and tenant storage, never a network call.

Every client is a real botocore client with fake credentials behind ``botocore.stub.Stubber`` (which validates each
request against the service model and answers it); ConverseStream, which Stubber cannot answer, is replaced by a
recording function. :func:`no_network` makes any HTTP send of botocore fail the test.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.stub import Stubber
import pytest

from nodestro_runtime.bedrock import BedrockServerPolicy, TenantRole
from nodestro_runtime.knowledge import KnowledgeTables
from nodestro_runtime.memory_spaces import _SQLiteMemoryTables
from nodestro_runtime.sqlite_tables import KnowledgeChunks
from nodestro_runtime.storage import Store
from nodestro_runtime.workspace import WorkspaceStorage

FAKE_AWS = {"aws_access_key_id": "AKIAIOSFODNN7EXAMPLE", "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/EXAMPLEKEY"}
ACCOUNT = "123456789012"
ROLE_A = TenantRole(f"arn:aws:iam::{ACCOUNT}:role/nodestro-tenant-alpha", "ext-alpha-0001")
ROLE_B = TenantRole("arn:aws:iam::210987654321:role/nodestro-tenant-beta", "ext-beta-0002")
#: Credentials STS hands out in the tests (distinct from FAKE_AWS and from any ambient variable).
STS_KEY = "ASIASTSFAKEALPHA0001"
STS_SECRET = "sts-secret-alpha"
STS_TOKEN = "IQoJb3JpZ2luX2VjFAKE-SESSION-TOKEN-ALPHA-0000000000000000"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any HTTP request of botocore fails the test (the stubs answer before a request would be sent)."""
    from botocore.httpsession import URLLib3Session

    def refuse(self: Any, request: Any) -> Any:
        raise AssertionError(f"a test tried to reach AWS: {request.method} {request.url}")

    monkeypatch.setattr(URLLib3Session, "send", refuse)
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/nodestro-test-aws-config")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/nodestro-test-aws-credentials")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


def stubbed(service: str, region: str = "eu-central-1", **kwargs: Any) -> tuple[Any, Stubber]:
    """A real client with fake credentials behind an activated Stubber."""
    client = boto3.client(service, region_name=region, **FAKE_AWS, **kwargs)
    stubber = Stubber(client)
    stubber.activate()
    return client, stubber


class FakeSession:
    """Stands in for ``boto3.Session``: hands out prepared clients and records what was asked for."""

    def __init__(self, clients: dict[str, Any], region: str = "eu-central-1") -> None:
        self.clients = clients
        self.region_name = region
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def client(self, service: str, **kwargs: Any) -> Any:
        self.calls.append((service, kwargs))
        return self.clients[service]


def assume_role_response(key: str = STS_KEY) -> dict[str, Any]:
    return {
        "Credentials": {
            "AccessKeyId": key,
            "SecretAccessKey": STS_SECRET,
            "SessionToken": STS_TOKEN,
            "Expiration": datetime.now(timezone.utc) + timedelta(minutes=30),
        },
        "AssumedRoleUser": {
            "AssumedRoleId": "AROAEXAMPLEID:nodestro-alpha",
            "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/nodestro-tenant-alpha/nodestro-alpha",
        },
    }


class StsFactory:
    """``sts_client_factory`` of a policy: one stubbed STS client per call, regions recorded."""

    def __init__(self) -> None:
        self.regions: list[str] = []
        self.stubbers: list[Stubber] = []
        self.queue: list[tuple[str, Any]] = []

    def expect(self, role: TenantRole, tenant: str, response: dict[str, Any] | None = None) -> None:
        self.queue.append(
            (
                "ok",
                (
                    response or assume_role_response(),
                    {
                        "RoleArn": role.role_arn,
                        "RoleSessionName": f"nodestro-{tenant}",
                        "ExternalId": role.external_id,
                        "DurationSeconds": 1800,
                    },
                ),
            )
        )

    def fail(self, code: str, message: str) -> None:
        self.queue.append(("error", (code, message)))

    def __call__(self, region: str) -> Any:
        self.regions.append(region)
        client, stubber = stubbed("sts", region)
        self.stubbers.append(stubber)
        if self.queue:
            kind, value = self.queue.pop(0)
            if kind == "ok":
                stubber.add_response("assume_role", value[0], value[1])
            else:
                stubber.add_client_error("assume_role", service_error_code=value[0], service_message=value[1])
        return client


def tenant_storage(root: Path, tenant: str, bedrock: BedrockServerPolicy | None = None) -> WorkspaceStorage:
    """A tenant's storage on SQLite (the workspace treats it like a server tenant: handed-in storage)."""
    root.mkdir(parents=True, exist_ok=True)
    database = root / f"{tenant}-server.db"
    store = Store(database)
    store.migrate(before_migration=lambda current, target: None)
    from orkexa import SQLiteFileStore, SQLiteIndex

    index = SQLiteIndex(database, namespace="nodestro")
    return WorkspaceStorage(
        tenant_id=tenant,
        store=store,
        knowledge=KnowledgeTables(SQLiteFileStore(database, namespace="nodestro"), index, KnowledgeChunks(index)),
        memory=_SQLiteMemoryTables(str(database)),
        bedrock=bedrock,
    )


def converse_response(content: list[dict[str, Any]], stop: str = "end_turn") -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop,
        "usage": {"inputTokens": 80, "outputTokens": 20, "totalTokens": 100},
        "metrics": {"latencyMs": 12},
        "ResponseMetadata": {"RequestId": "req-bedrock", "HTTPStatusCode": 200},
    }


def stream_events(content: list[dict[str, Any]], stop: str = "end_turn") -> list[dict[str, Any]]:
    """The ConverseStream events of an answer with text and tool calls."""
    events: list[dict[str, Any]] = [{"messageStart": {"role": "assistant"}}]
    for index, block in enumerate(content):
        if "text" in block:
            events.append({"contentBlockDelta": {"contentBlockIndex": index, "delta": {"text": block["text"]}}})
        else:
            use = block["toolUse"]
            events.append(
                {
                    "contentBlockStart": {
                        "contentBlockIndex": index,
                        "start": {"toolUse": {"toolUseId": use["toolUseId"], "name": use["name"]}},
                    }
                }
            )
            import json

            events.append(
                {
                    "contentBlockDelta": {
                        "contentBlockIndex": index,
                        "delta": {"toolUse": {"input": json.dumps(use["input"])}},
                    }
                }
            )
        events.append({"contentBlockStop": {"contentBlockIndex": index}})
    events.append({"messageStop": {"stopReason": stop}})
    events.append({"metadata": {"usage": {"inputTokens": 80, "outputTokens": 20, "totalTokens": 100}, "metrics": {}}})
    return events


class ScriptedRuntime:
    """A stubbed ``bedrock-runtime`` client whose ConverseStream answers come from a script."""

    def __init__(self, answers: list[list[dict[str, Any]]], region: str = "eu-central-1") -> None:
        self.client, self.stubber = stubbed("bedrock-runtime", region)
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []
        self.client.converse_stream = self._stream

    def _stream(self, **params: Any) -> Any:
        self.calls.append(params)
        content = self.answers.pop(0)
        stop = "tool_use" if any("toolUse" in block for block in content) else "end_turn"
        return {"stream": iter(stream_events(content, stop)), "ResponseMetadata": {"RequestId": "req-stream"}}


def iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from iter_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from iter_strings(child)
