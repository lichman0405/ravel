# RAVEL sandbox

T19 puts every agent-issued command and every Chromium render inside a Docker
container. The split that makes that a boundary rather than a convention:

| Process | Has `/var/run/docker.sock` | May reach a sandbox |
| --- | --- | --- |
| `ravel-sandbox-supervisor` | yes | starts, stops, and executes into it |
| Gateway / API / DSH / agent containers | no | over the supervisor's HTTP API with its bearer token |

A process without the socket cannot start a container, mount a host path, or
read another project's volume, no matter what it sends to the supervisor: the
supervisor checks the project on the handle and on every request. Giving an
agent this repository and the supervisor's URL and token therefore does not
give it Docker, and a leaked token is still bounded by the handle's project.

## Start it

```sh
# One-time, from the repository root. The sandbox image is not pulled by
# compose: it is built from this tree and its Playwright base is pinned.
docker build -f infra/sandbox/Dockerfile.sandbox -t ravel-sandbox:latest .

# Required secret, at least 16 characters.
export RAVEL_SANDBOX_SUPERVISOR_TOKEN="$(openssl rand -hex 32)"

# Exact browser targets, operator-approved. Empty closes the browser path.
export RAVEL_SANDBOX_PROXY_ALLOWLIST="pubs.acs.org:443,www.nature.com:443"

docker compose \
  -f docker-compose.yml \
  -f infra/sandbox/docker-compose.sandbox.yml \
  up -d sandbox-supervisor
```

The supervisor publishes on `127.0.0.1:8799` only. RAVEL processes configure:

```dotenv
RAVEL_RESEARCH_BROWSER_SANDBOX_PROVIDER=docker
RAVEL_SANDBOX_SUPERVISOR_URL=http://127.0.0.1:8799
RAVEL_SANDBOX_SUPERVISOR_TOKEN=<the same secret>
```

The browser path additionally requires `RAVEL_PROJECT_ID`, `RAVEL_ROLE`
(`research`), and optionally `RAVEL_BINDING_ID` and `RAVEL_LEASE_EPOCH` in the
agent tool server's trusted environment. A process that is not Research does
not get a browser handle.

## What each container gets

One command sandbox and, separately, one browser sandbox per project. Both are
started with:

- a read-only root filesystem, UID/GID 65534, all capabilities dropped,
  `no-new-privileges`, and the default seccomp profile;
- CPU, memory, PID, and tmpfs (`/tmp`) limits, and a per-command wall clock
  (2 CPUs, 4 GiB, 256 PIDs, 10 GiB scratch, 120 seconds by default). A project
  may narrow these; a request wider than the deployment ceiling is refused;
- exactly one mount: the project's named workspace volume at `/workspace`;
- no `--privileged`, no `--pid=host`, no `--network=host`, and no Docker socket;
- a filtered environment. Nothing is inherited from the supervisor. Only
  names in `RAVEL_SANDBOX_ENV_ALLOWLIST` reach a command, and their values are
  redacted from output and from the stored argv.

The command sandbox uses `--network none`. The browser sandbox is attached to a
per-project `--internal` Docker network whose only other peer is a proxy
container. The proxy sits on an egress network as well and enforces an exact
`host:port` allowlist, a public-address policy, and a client-CIDR check. It
refuses RFC1918, loopback, link-local, NAT64-embedded loopback, and the instance
metadata services. Redirects and subresources are guarded again inside
Chromium's request route; the proxy is the authority because it sees the socket
that is actually made.

Web/API research is unaffected: `Fetcher` pins the validated address at the
transport and does not use this service.

## Files, artifacts, and records

Paths are project-relative and rejected lexically before a request is sent;
inside the container, every component is `lstat`ed, the final open uses
`O_NOFOLLOW`, and a symlink or a regular file with more than one hard link is
refused. A sandbox can read and write its own workspace and nothing else.

A run never becomes evidence by assertion. `collect` reads bytes out of the
sandbox, hashes them, and keeps them under the supervisor's state. Registration
through RAVEL's Gateway (or its `ArtifactRepository` + `ArtifactStore`) hashes
the bytes again and refuses a mismatch. The sandbox has no database credential
and no cluster credential, so it cannot write authoritative Project State or an
experiment result.

Handles and runs are JSON records under `RAVEL_SANDBOX_STATE_DIR`:

- `stop` stops the container but keeps the handle, workspace, and run records;
- `reconnect` starts the same container under the same project binding;
- `destroy` removes the container and the workspace volume, but leaves the
  handle and run records queryable;
- a supervisor restart marks an in-flight run `LOST` rather than pretending it
  finished.

## Resource and log ownership

The supervisor owns the container lifecycle, cgroup limits, run records, and
logs. `docker logs` and `journalctl -u ravel-sandbox-supervisor` are operator
surfaces; an agent sees only `GET /handles/{id}/logs`, which returns a redacted
tail. Collected artifact bytes are owned by the supervisor's state volume.
Project workspaces are named `ravel-t19-ws-<digest>` and are destroyed only by
`destroy`; they are not part of a supervisor image or a host bind mount.

## Limits of this V0

- The supervisor token is a deployment capability shared by every project on
  this host. It does not replace project authority, but a token holder that
  also knows another project's id can provision a sandbox for it. Per-project
  tokens or mTLS are the next step; the handle checks are already in place.
- Docker's default seccomp profile is used, not a custom allowlist. It is a
  real restriction (it blocks `clone3`, `mount`, and the namespace syscalls the
  default profile refuses) but it is the distribution's, not RAVEL's.
- The browser runs Chromium with `--no-sandbox` because Chromium's own setuid
  sandbox cannot initialize under a non-root UID, a read-only rootfs, and
  `no-new-privileges`. The Docker container is the boundary.
- One Docker daemon is a single trust domain. A project that can reach the
  supervisor can consume host CPU and disk up to its quota; the supervisor caps
  the number of sandboxes only by one-per-project-purpose, not globally.
