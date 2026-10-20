"""Entry point for the sandbox supervisor container.

The supervisor is the only RAVEL service that gets `/var/run/docker.sock`, and
this module is how that service starts. Configuration is environment-only so the
container can be launched by compose without an argument list that duplicates
the deployment's settings.

Set `RAVEL_SANDBOX_SUPERVISOR_BIND=0.0.0.0` when the RAVEL app runs in another
container (compose does). The default is loopback: a supervisor started on a
host for a local test should not be reachable from the host's network by
default, because its bearer token is the only thing between a caller and
Docker.
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from ravel.sandbox.supervisor import SupervisorConfig, create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAVEL sandbox supervisor")
    parser.add_argument(
        "--bind",
        default=os.environ.get("RAVEL_SANDBOX_SUPERVISOR_BIND", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("RAVEL_SANDBOX_SUPERVISOR_PORT", "8799")),
    )
    args = parser.parse_args(argv)

    try:
        config = SupervisorConfig.from_env()
        config.validate()
        app = create_app(config)
    except (ValueError, OSError) as exc:
        print(f"sandbox supervisor configuration refused: {exc}", file=sys.stderr)
        return 2

    uvicorn.run(
        app,
        host=args.bind,
        port=args.port,
        log_level=os.environ.get("RAVEL_SANDBOX_SUPERVISOR_LOG_LEVEL", "info").lower(),
        access_log=False,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
