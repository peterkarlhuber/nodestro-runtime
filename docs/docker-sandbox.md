# The Docker sandbox: status, reasons and the reaper

Docker is the sandbox for AI-generated and other untrusted tool code wherever the built-in Linux sandbox is not
released (macOS, Windows, servers, Nodestro Local Docker, Linux systems outside the tested list). Nodestro starts
without Docker; only the secure execution is blocked, and the settings say why.

## When Docker counts

The Docker backend (`Settings.sandbox_backends`, name `docker`) is `approved` only when both of Orkexa's checks passed
for the daemon the `docker` CLI reaches (`DOCKER_HOST`, `DOCKER_CONTEXT`):

1. **Preflight** (`orkexa.dynamic.docker.preflight`): the CLI exists, the daemon answers, runs Linux containers,
   enforces memory, process and CPU limits and filters system calls with seccomp. Repeated at most every 5 seconds.
2. **Self-test** (`DockerSandbox.self_test`): one container of exactly the sandbox's shape, in Nodestro's runtime
   image, shows what the daemon really applied (user 65534, no capabilities, `NoNewPrivs`, seccomp, only the loopback
   interface, read-only root, `noexec` temporary file systems, cgroup v2 limits, open-file and file-size limits, no
   foreign environment variables). It runs once per daemon (host fingerprint); a changed daemon or setting runs it
   again, a failed one is retried after 5 minutes or when asked (`Workspace.docker_status(refresh=True)`).

Both run in a background thread (`nodestro_runtime.docker_status.DockerMonitor`, one per daemon and process). Reading
the settings never waits for `docker info`. Until the first check finished the state is `checking` (reason
`docker-checking`) and nothing runs in Docker. A container sandbox counts as isolating for the sandbox policy only for
a proven daemon; before that untrusted code is refused with `sandbox.isolation_required`, and every container call
still checks the daemon and its image again itself before it starts.

The first self-test needs the runtime image; it is built on demand (it needs the package index once). The first start
with Docker therefore stays `checking` until that build finished, or reports `docker-image` when it fails.

## Reasons

| Reason | Meaning |
|---|---|
| `docker-missing` | The `docker` command is not installed |
| `docker-unreachable` | No daemon answers (not started, wrong `DOCKER_HOST`, or it answered nonsense) |
| `docker-os-type` | The daemon runs Windows containers; Linux containers are needed |
| `docker-limits` | The daemon cannot enforce memory, process or CPU limits (rootless without cgroup delegation) |
| `docker-seccomp` | The daemon has no seccomp filter, or its default profile is `unconfined` |
| `docker-rootless-cgroup` | A rootless daemon on cgroup v1, which cannot apply any resource limit |
| `docker-image` | The sandbox image cannot be found or built |
| `docker-selftest` | A container of the sandbox's shape showed a restriction the daemon did not apply |
| `docker-checking` | The first check has not finished yet (state `checking`) |

The report also carries `engine` (`docker`, `docker-desktop`, `podman`, `colima`, `orbstack`, `unknown`; for
information only, never a reason to trust a daemon), `rootless`, `cgroup_version`, the individual `checks` (`cli`,
`daemon`, `preflight`, `image`, `self-test`, each with `ok` and `detail`) and `tested_at`.

## Rootful Docker on Linux

A daemon that runs as root (the usual Docker Engine installation on Linux) is reported with a note: membership in the
`docker` group, or any other access to the daemon's socket, is equivalent to root access on that computer, whatever
Nodestro's sandbox does. Give that access only to people who may be root there. Rootless Docker or Podman (with the
`cpu`, `memory` and `pids` cgroup controllers delegated) limit what a container escape could reach to that user.
Docker Desktop, OrbStack and Colima keep the daemon in a virtual machine and get no such note.

## The reaper

Every tool container carries the labels `org.orkexa.sandbox=nodestro` and `org.orkexa.deadline=<epoch seconds>`. A
desktop and Nodestro Local Docker remove all `nodestro` containers at the start (left behind by a crash; no other
owner is touched), then every process removes the expired ones every 60 seconds. A server's processes share the
daemon, so they only remove expired containers. Local MCP servers run with the owner `nodestro-mcp` and are never
reaped. Through the sandbox gateway containers cannot be listed; the gateway removes its own containers.
