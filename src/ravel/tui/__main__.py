"""`python -m ravel.tui`, which the Makefile's `tui` target runs.

Three arguments, and each one exists because a person needs it rather than
because a flag was easy to add. `--url` is how the console is pointed at a
Gateway that is not on this machine, which is the normal case once RAVEL is
running on a CVM. `--project` skips the "which one" question for somebody who
works in one project all day. `--username` and `--password` exist for the
headless path — a script, a test, a cron — and are read from the environment
when they are not given, because a password typed on a command line is a
password in somebody's shell history and `ps`.
"""

from __future__ import annotations

import argparse
import os

from ravel.tui.app import RavelTUI

#: The environment variables the credentials are read from when no flag is
#: given. Named the same as the Gateway's own, so one `.env` configures both.
ENV_USERNAME = "RAVEL_TUI_USERNAME"
ENV_PASSWORD = "RAVEL_TUI_PASSWORD"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ravel.tui",
        description="The RAVEL console. Reads and controls; decides nothing.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("RAVEL_GATEWAY_URL", "http://127.0.0.1:8000"),
        help="the Gateway to talk to (default: %(default)s)",
    )
    parser.add_argument("--project", default="", help="open this project instead of asking")
    parser.add_argument(
        "--username", default=os.environ.get(ENV_USERNAME, ""), help="sign in without the form"
    )
    parser.add_argument(
        "--password",
        default=os.environ.get(ENV_PASSWORD, ""),
        help=f"sign in without the form (or set {ENV_PASSWORD})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments and hand off to the console."""
    arguments = build_parser().parse_args(argv)
    RavelTUI(
        base_url=arguments.url,
        username=arguments.username,
        password=arguments.password,
        project_id=arguments.project,
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ENV_PASSWORD", "ENV_USERNAME", "build_parser", "main"]
