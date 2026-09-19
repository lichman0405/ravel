#!/usr/bin/env python3
"""Create a RAVEL user, and optionally the project they will direct.

    .venv/bin/python scripts/create_account.py --username ada
    .venv/bin/python scripts/create_account.py --username ada --new-project \\
        --title "Catalyst screen" --objective "Find a dopant that raises conductivity by 15%."
    .venv/bin/python scripts/create_account.py --username bench --project <project_id> \\
        --role LAB_USER --granted-by ada

RAVEL's Gateway can authenticate a person but cannot create one: every route
under `/auth` reads an account, and the only writes it performs are on login
chains. Membership is the one thing in this system that manufactures authority,
so it is deliberately not reachable over HTTP by whoever happens to be logged
in. That leaves provisioning to an operator, at a terminal, on the host — which
is what this script is.

It writes through RAVEL's own repositories, so an account made here is subject
to exactly the constraints a test-created one is: usernames are unique, a
membership beyond the project's first names who granted it and cannot confer
more authority than the granter holds, and the append-only guards apply. None
of that is re-implemented here, and none of it can be bypassed here.

Everything happens in one transaction. A half-provisioned operator — an account
with no project, or a project whose owner was never granted — is a state nobody
can log in to fix, so it is not a state this can leave behind.

The password is hashed with RAVEL's own Argon2id parameters and never printed,
stored, or passed to the repository, which takes a hash and has no argument for
anything else. When `--password` is omitted it is read from the terminal, which
keeps it out of the shell's history and out of `ps`.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from ravel.config import Settings
from ravel.domain.enums import UserRole
from ravel.gateway.auth.passwords import hash_password
from ravel.state.database import Database
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.projects import ProjectRegistry


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a RAVEL user, and optionally a project and a membership.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--username", required=True, help="the login name; must be unique")
    parser.add_argument("--email", default=None, help="optional contact address")
    parser.add_argument(
        "--password",
        default=None,
        help="read from the terminal when omitted; passing it here puts it in "
        "your shell history and in `ps`, so prefer the prompt",
    )
    parser.add_argument(
        "--new-project",
        action="store_true",
        help="also open a project owned by this account (needs --title and --objective)",
    )
    parser.add_argument("--title", default=None, help="the new project's title")
    parser.add_argument("--objective", default=None, help="the new project's objective")
    parser.add_argument(
        "--project",
        default=None,
        help="an existing project to grant a membership in, instead of --new-project",
    )
    parser.add_argument(
        "--role",
        default=UserRole.PROJECT_OWNER.value,
        choices=[role.value for role in UserRole],
        help="the membership to grant (default: PROJECT_OWNER)",
    )
    parser.add_argument(
        "--granted-by",
        default=None,
        help="username of the member granting this role; required for every "
        "membership after a project's first",
    )
    parser.add_argument(
        "--no-membership",
        action="store_true",
        help="create the account only, with no project and no membership",
    )
    return parser.parse_args(argv)


def validate(args: argparse.Namespace) -> None:
    """Refuse an impossible request before anything is opened.

    Raises:
        ValueError: The arguments do not describe a request that can be met.
    """
    if args.new_project and args.project:
        raise ValueError("--new-project and --project are alternatives; give one")
    if args.new_project and not (args.title and args.objective):
        raise ValueError("--new-project needs both --title and --objective")


def read_password(args: argparse.Namespace) -> str:
    """The password to hash, from the flag or from the terminal."""
    if args.password is not None:
        return args.password
    first = getpass.getpass("password for the new account: ")
    if not first:
        raise ValueError("no password given; an account without one could never log in")
    if first != getpass.getpass("repeat it: "):
        raise ValueError("the two did not match; nothing was written")
    return first


def provision(args: argparse.Namespace, password: str) -> dict[str, str]:
    """Create the account, and whatever was asked for alongside it.

    Nothing in here exits the process. Every refusal is raised, because an
    exit taken inside the transaction would leave the context manager to
    discover the rollback in its `finally` rather than through the `except`
    that says it rolls back — and a refusal that has already written a user is
    the half-provisioned state this script exists not to create.

    Raises:
        ValueError: The request cannot be met.
        PermissionError: The granter does not hold the authority being granted.
    """
    database = Database.from_settings(Settings())
    role = UserRole(args.role)

    with database.transaction() as session:
        users = UserRepository(session)
        if users.by_username(args.username) is not None:
            raise ValueError(f"{args.username!r} already exists; usernames are not reused")

        account = users.create(
            username=args.username,
            password_hash=hash_password(password),
            email=args.email,
        )
        made = {"username": account.username, "user_id": account.user_id}

        if args.no_membership:
            return made

        if args.new_project:
            project_id = ProjectRegistry(session).create(
                title=args.title or "",
                objective=args.objective or "",
                created_by=account.user_id,
            ).project_id
        elif args.project:
            project_id = args.project
        else:
            return made

        granted_by = None
        if args.granted_by is not None:
            granter = users.by_username(args.granted_by)
            if granter is None:
                raise ValueError(f"no account named {args.granted_by!r} to grant on behalf of")
            granted_by = granter.user_id

        MembershipRepository(session, project_id).grant(
            user_id=account.user_id,
            role=role,
            granted_by=granted_by,
        )
        made["project_id"] = project_id
        made["role"] = role.value
        return made


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate(args)
        made = provision(args, read_password(args))
    except (ValueError, PermissionError, LookupError) as refusal:
        # `ValueError` is this script's own validation and the domain's
        # transition refusals, `PermissionError` is membership authority, and
        # `LookupError` is a repository's `NotFound`. All three already carry a
        # sentence written for a person, so it is printed rather than a
        # traceback that buries it.
        print(f"\n  refused: {refusal}\n", file=sys.stderr)
        return 1

    print(f"\n  created {made['username']!r}  ({made['user_id']})")
    if "project_id" in made:
        print(f"  project   {made['project_id']}")
        print(f"  role      {made['role']}")
        print(f"\n  the TUI opens on it:  make tui   (log in as {made['username']})")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
