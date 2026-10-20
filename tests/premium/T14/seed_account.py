#!/usr/bin/env python3
"""Seed one human account and one owned project in an isolated test database.

This exists for the Windows Electron launch tests, which need a real Gateway
and a real account but cannot use pytest fixtures from outside the repository.
It writes through the product's repositories; it does not insert rows itself.
The database name must end in ``_test`` and is truncated first, for the same
reason the integration suite refuses anything else.

    RAVEL_TEST_DB=ravel_t14_test .venv/bin/python tests/premium/T14/seed_account.py

It prints one JSON object with the account and project identifiers. It never
prints or stores the password beyond hashing it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import text

from ravel.config import Settings
from ravel.domain.enums import UserRole
from ravel.gateway.auth.passwords import hash_password
from ravel.state import guards
from ravel.state.database import Database, create_db_engine, install_utc_guard
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.tables import Base

DEFAULT_USERNAME = "desktop-owner"
DEFAULT_PASSWORD = "desktop-owner-password"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default=os.environ.get("T14_USERNAME", DEFAULT_USERNAME))
    parser.add_argument("--password", default=os.environ.get("T14_PASSWORD", DEFAULT_PASSWORD))
    parser.add_argument("--title", default="Catalyst screen")
    args = parser.parse_args(argv)

    database_name = os.environ.get("RAVEL_TEST_DB", "ravel_t14_test")
    if not database_name.endswith("_test"):
        print(f"refusing to truncate {database_name!r}: not a _test database", file=sys.stderr)
        return 2

    settings = Settings(
        env="test",
        postgres_db=database_name,
        temporal_task_queue="ravel-v0-test-t14-desktop",
        RAVEL_POSTGRES_DSN=None,
    )
    engine = create_db_engine(settings)
    install_utc_guard(engine)
    database = Database(engine)
    try:
        database.health()
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            guards.install(connection)
            names = ", ".join(sorted(Base.metadata.tables))
            connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))

        with database.transaction() as session:
            user = UserRepository(session).create(
                username=args.username, password_hash=hash_password(args.password)
            )
            project = ProjectRegistry(session).create(
                title=args.title,
                objective="Screen catalyst dopants against a measured objective.",
                created_by=user.user_id,
            )
            MembershipRepository(session, project.project_id).grant(
                user_id=user.user_id, role=UserRole.PROJECT_OWNER
            )
            payload = {
                "username": user.username,
                "user_id": user.user_id,
                "password_set": True,
                "project_id": project.project_id,
                "display_id": project.display_id,
                "title": project.title,
                "database": database_name,
            }
    finally:
        database.dispose()

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
