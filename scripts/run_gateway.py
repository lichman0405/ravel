#!/usr/bin/env python3
"""Run the RAVEL Research Gateway.

    .venv/bin/python scripts/run_gateway.py
    .venv/bin/python scripts/run_gateway.py --port 8080

**Why this exists next to `uvicorn ravel.gateway.app:create_app --factory`.**
That command is what a developer wants and what `make gateway` runs: uvicorn's
own reloader, uvicorn's own logging, uvicorn's own argument parsing. A
deployment wants the opposite of all three — no reloader, RAVEL's log format so
that a line from the Gateway looks like a line from the supervisor, and the
address read from the settings the rest of the system is configured by rather
than from a second place a unit file could disagree with.

Both end in the same application. This one is the deployment's entry point and
is what `infra/systemd/ravel-gateway.service` starts.

**The Gateway beats here, not below.** `create_app` installs the lifespan that
reports this process alive and records its shutdown; uvicorn runs it. That
means the two ways of starting the Gateway differ in exactly the ways above and
not in whether the deployment can see it — the lifespan is the application's,
so both get it.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from ravel.config import Settings
from ravel.domain.services import GATEWAY
from ravel.service import configure_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run RAVEL's Research Gateway.")
    parser.add_argument(
        "--host",
        default=None,
        help="the address to bind; overrides RAVEL_GATEWAY_HOST",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="the port to bind; overrides RAVEL_GATEWAY_PORT",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help=(
            "the uvicorn log level; overrides RAVEL_LOG_LEVEL. RAVEL's own "
            "records follow RAVEL_LOG_LEVEL and RAVEL_LOG_FORMAT either way"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    configure_logging(GATEWAY, level=settings.log_level, log_format=settings.log_format)

    host = args.host or settings.gateway_host
    port = args.port or settings.gateway_port
    print(f"\n  gateway on http://{host}:{port}\n\n  Ctrl-C to stop\n")

    uvicorn.run(
        "ravel.gateway.app:create_app",
        factory=True,
        host=host,
        port=port,
        log_level=(args.log_level or settings.log_level).lower(),
        # `None` hands logging to the configuration above instead of replacing
        # it with uvicorn's. Without this the Gateway's own records — the
        # heartbeat's, the harness pool's, a route's — would come out in
        # uvicorn's format while everything else in the process came out in
        # RAVEL's, which is exactly the split `RAVEL_LOG_FORMAT` exists to
        # avoid.
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
