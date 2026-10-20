"""Docker-first sandbox execution for RAVEL.

The public pieces:

- `models` — the SandboxProvider contract's values and errors.
- `authority` — role and quota checks that only ever narrow a request.
- `provider` — the HTTP client RAVEL processes use; it contains no Docker code.
- `supervisor` — the Docker-capable service, run separately.
- `paths`, `redaction`, `egress` — the shared policy primitives.

Nothing in this package imports `docker` or opens a Unix socket except
`docker_engine`, which only the supervisor imports. A RAVEL API, DSH, or agent
container is given no Docker socket; it reaches sandboxes through `provider`.
"""

__all__: list[str] = []
