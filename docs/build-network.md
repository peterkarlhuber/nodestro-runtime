# A filtered build network for source builds (design analysis, not implemented)

> Status: design analysis for RT-040. Nothing described here exists yet. Until a build network like this exists and
> is proven by the self-test, source builds are refused outside the desktop profile (`deps.source_build_disabled`,
> RT-038).

## The problem

A Python package that ships only a source distribution must run its build script to report its dependencies, so
`uv pip compile` for such a package cannot be split into "download on the host" and "build without network": the next
download is known only after the build ran. Nodestro therefore resolves these packages in one container
(`ContainerImages.compile_in_container`) that has network. Everything else about that container is the proven
sandbox shape (`orkexa.dynamic.docker.run_args`, enforced by `container_images.check_shape` up to an executable
`/tmp` and the proxy variables); the network is the one open door.

Today that network is Docker's default bridge (or `NODESTRO_DOCKER_BUILD_NETWORK`). From there a build script can
reach:

- every host on the LAN and in RFC 1918 / ULA ranges the host routes to;
- the host itself through the bridge gateway (`172.17.0.1` and the like) and every port it publishes or binds on
  `0.0.0.0` (databases, the Docker API over TCP, the Nodestro API of a server);
- the cloud instance metadata service (`169.254.169.254`, `fd00:ec2::254` on AWS, `metadata.google.internal` on
  GCP), which hands out the instance's credentials unless the operator locked it down (IMDSv2 hop limit 1 helps on
  AWS for containers behind a bridge, but cannot be assumed);
- the whole internet, for exfiltration of whatever the build can read (it can read only the requirements, the
  previous lock and a CA certificate; no credentials reach it).

On a desktop the person accepted the build explicitly, and the person's own computer is the blast radius. On a
server many tenants share the network, and the metadata service and internal services are worth more. That is why
the decision for now is: desktop only.

## Goal

A network on which a build container can reach exactly the package sources it needs (the configured index, or a
package proxy) over HTTPS, and nothing else: no LAN, no host, no metadata service, no other internet host, over IPv4
and IPv6, with DNS answered only for the allowed names. The runtime must be able to prove that property on the
daemon it uses before it runs a build there, the way the self-test proves the sandbox shape.

## Options

### A. Internal Docker network plus an egress proxy container (recommended direction)

- A Docker network created with `--internal` has no route out: containers on it reach only other containers on it.
  No gateway, no NAT, no published ports of the host (the bridge gateway address is not assigned to the host for an
  internal network on Docker; this needs to be verified per engine, see below).
- One proxy container sits on both that internal network and an outbound network. It is the only way out. The build
  container gets `HTTPS_PROXY`/`HTTP_PROXY` pointing at it, and nothing else.
- The proxy allows `CONNECT host:443` only for an allowlist of exact host names (for PyPI: `pypi.org`,
  `files.pythonhosted.org`; for a private index: its host). No plain HTTP except, if needed, to the same allowlist.
  Every other `CONNECT` and every request to an IP literal is refused.
- The proxy resolves the allowed names itself and connects only if every resolved address is a public unicast
  address: it refuses loopback, RFC 1918 (`10/8`, `172.16/12`, `192.168/16`), CGNAT (`100.64/10`), link-local
  (`169.254/16`, which includes `169.254.169.254`), `0/8`, multicast, and for IPv6 `::1`, `fc00::/7` (which includes
  `fd00::/8` and AWS's `fd00:ec2::254`), `fe80::/10`, IPv4-mapped (`::ffff:0:0/96`) and NAT64 (`64:ff9b::/96`)
  forms of the IPv4 ranges. It connects to the address it checked (no second lookup), which defeats DNS rebinding
  between check and connect.
- Candidates: a small purpose-built proxy (a few hundred lines; the allowlist and address checks are the whole
  point, so they should be our code and tested), or Squid/Envoy/tinyproxy with a strict configuration. A stock proxy
  usually resolves again after its ACL check and allows IP-literal `CONNECT` unless told otherwise; both must be
  closed and tested, not assumed.
- The proxy image is pinned by digest like `UV_IMAGE`, runs with the sandbox's hardening (read-only, no
  capabilities, `nobody`), and lives only as long as the build (or as a long-lived service on a server, owned by the
  operator's deployment, not by tenant actions).

### B. Package proxy instead of the internet

The allowlist can name one host: a package proxy (devpi, a PyPI mirror, Artifactory, a pull-through cache) that the
operator already runs. Then the egress proxy allows only that host, and the build reaches the internet only through
the package proxy's own policy. This is simpler to reason about and is the natural choice for servers. A package
proxy that needs credentials cannot be used as is: credentials never go into the build container (build code could
read them). The egress proxy would have to add them on the way out (it terminates nothing for `CONNECT`, so this
works only for a plain-HTTP hop to the package proxy, or by letting the egress proxy terminate TLS with its own CA,
which the build container then trusts; both are larger changes and need their own review).

### C. Host firewall rules (iptables/nftables on `DOCKER-USER`)

Blocking RFC 1918, link-local and the metadata addresses for the build network on the host is effective on a Linux
host the operator controls, but Nodestro does not control the host firewall (rootless Docker, Docker Desktop, Podman,
remote daemons, the sandbox gateway), cannot verify the rules from inside a container reliably, and a wrong rule
fails open. It can be documented as defense in depth for operators, not as the mechanism Nodestro relies on.

### D. No network at all: iterative resolving

Download the hashed sdists on the host side (no code runs), build their metadata in a `--network none` container,
read the new requirements, download those, and repeat until the set is closed. This removes the network from the
container entirely, at the cost of reimplementing part of the resolver loop (or driving uv step by step) and of
`setup_requires`/PEP 517 build backends that fetch more during the build (they would fail instead of fetching, which
is the safe outcome). It is the stronger end state and remains a candidate (RT-038 already names it).

## Details that decide whether it is secure

- **DNS.** On an internal network the build container should not need DNS at all: with `HTTPS_PROXY` set, uv and pip
  send the host name in `CONNECT`, and the proxy resolves it. Give the container no resolver it can use for
  arbitrary names (Docker's embedded DNS at `127.0.0.11` still answers container names on that network and may
  forward other names; whether it forwards on an `--internal` network differs between engines and must be tested).
  A DNS channel is an exfiltration channel even when TCP is blocked.
- **IPv4 and IPv6.** The internal network must have IPv6 disabled or be filtered the same way; the proxy's address
  checks must cover both families and the mapped/translated forms. A test must try both.
- **Redirects.** A package index may redirect to another host (PyPI's file host, a CDN). With `CONNECT`, redirects
  are followed by the client inside the container, so each new host goes through the proxy's allowlist again: a
  redirect to a host outside the allowlist fails, which is correct. The allowlist must therefore name the real file
  hosts, not only the index.
- **CONNECT allowlist.** Match exact host names (case-folded, no trailing dot tricks, no `user@host`), port 443 only,
  no IP literals, no IDN confusion (compare the A-label form).
- **DNS rebinding.** Resolve once in the proxy, check every address, connect to that checked address. Never let the
  client's choice of address through.
- **Bypass via the proxy itself.** The proxy's own management or status endpoints must be off; it must not forward
  to itself, to other containers on its networks or to the host.
- **Other containers on the internal network.** Only the build container and the proxy may be attached; a shared
  internal network on a server would let one tenant's build talk to another's. One network per build, or per-build
  proxy credentials, are needed on servers.
- **Proof, not configuration.** `NODESTRO_DOCKER_BUILD_NETWORK` naming a network is not proof that the network is
  filtered. The runtime must check it.

## How the self-test would prove it

Before the first build on a daemon (and after a daemon change), a probe container in the build shape on the build
network, using only the standard library, must show:

1. `CONNECT` through the proxy to an allowed host succeeds (TLS handshake completes against the expected name).
2. Refused: `CONNECT` to a non-allowlisted name; to an IP literal; to `169.254.169.254:80`; to `[fd00:ec2::254]:80`;
   to the bridge gateway and the host's addresses (both families); to an RFC 1918 address; to an allowed name on a
   port other than 443.
3. Direct connections without the proxy fail: TCP to a public address, to the gateway, to `169.254.169.254`, over
   IPv4 and IPv6; UDP 53 to a public resolver; a lookup of an arbitrary external name through the container's
   resolver returns nothing.
4. The interfaces of the container are only `lo` and the internal network; there is no default route.

A test name that the proxy resolves to a private address (a controlled rebinding name, or a test-only allowlist
entry pointing at `127.0.0.1`) must be refused. Only when every check passes may the runtime record the network as
proven (like `docker_status.record_proof`) and allow source builds on that profile. A failed or missing proof keeps
`deps.source_build_disabled`; there is no operator switch that skips the proof.

## Open questions

- Who owns the proxy on a server: the runtime (started per build) or the deployment (`nodestro-server` compose)?
- How the sandbox gateway (Nodestro Local Docker) would create the internal network and the proxy without giving the
  client network control in general.
- Whether iterative resolving (option D) is cheaper to build and review than a proven proxy.
