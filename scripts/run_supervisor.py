#!/usr/bin/env python3
"""Run the unattended Project Supervisor.

    .venv/bin/python scripts/run_supervisor.py
    .venv/bin/python scripts/run_supervisor.py --poll-seconds 10

The supervisor discovers projects that are active (not ended, not paused) and
drives each with a `ProjectLoop`. New projects are picked up on the next poll;
ended projects are reaped. It keeps running until interrupted.

This replaces the manual `scripts/run_project.py` step for unattended
deployments. `run_project.py` is still useful for driving a single project
interactively or from a CI job.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from ravel.config import Settings
from ravel.domain.services import SUPERVISOR
from ravel.execution.supervisor import ProjectSupervisor
from ravel.service import configure_logging
from ravel.state.database import Database


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the RAVEL Project Supervisor.")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="how long between discovery polls (default: 5.0)",
    )
    parser.add_argument(
        "--loop-poll-seconds",
        type=float,
        default=0.5,
        help="how long each project loop waits between rounds (default: 0.5)",
    )
    parser.add_argument(
        "--loop-max-rounds",
        type=int,
        default=200,
        help="max rounds each project loop may take (default: 200)",
    )
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace, settings: Settings) -> int:
    database = Database.from_settings(settings)
    supervisor = ProjectSupervisor(
        database=database,
        settings=settings,
        poll_seconds=args.poll_seconds,
        loop_poll_seconds=args.loop_poll_seconds,
        loop_max_rounds=args.loop_max_rounds,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, supervisor.stop)

    try:
        await supervisor.run()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    configure_logging(
        SUPERVISOR, level=settings.log_level, log_format=settings.log_format
    )
    if settings.deepseek_api_key is None:
        print(
            "\n  no DEEPSEEK_API_KEY: Master and Review turns will fail, and the "
            "supervisor will halt projects without deciding anything.\n",
            file=sys.stderr,
        )
    return asyncio.run(main_async(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())
