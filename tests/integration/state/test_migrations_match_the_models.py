"""A database built by the migration chain equals one built by the models.

Two ways to get a schema exist in this project and they do not run the same
code. The test suite and `create_all` build from `ravel.state.tables`; a
deployment builds from `alembic upgrade head`. Nothing compared the two, and
the gap was not theoretical — the initial migration declared guards for every
table the metadata knows about, including one a later revision creates, so the
chain failed on a fresh database at the first table it had not created yet.
Every test passed throughout, because every test uses the other path.

So this gate builds both and compares them. What is compared is what the
`_assert_schema_is_current` guard in `tests/integration/conftest.py` compares,
for the same reason: tables, columns, and check constraints by name *and* by
the literals in them. A constraint that kept its name while its vocabulary
changed is exactly what an enum gaining a member produces, and it is the shape
of drift that leaves a deployed database refusing a value the code writes.

Comparing whole `CREATE` statements would be stricter and would also fail on
every difference in how PostgreSQL echoes a definition back — `integer` versus
`INTEGER`, `::character varying` inserted where a cast is implied. The
comparison is by what the schema *does*.

Not covered, and named so that it is not mistaken for covered: index
definitions beyond their existence, foreign key actions, and column defaults.
An autogenerate diff would catch those, and running one here would mean
keeping a second copy of the model in the test.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, create_engine, inspect, text
from sqlalchemy.engine import Engine

from ravel.state.guards import GUARD_TABLES
from ravel.state.tables import Base

pytestmark = pytest.mark.integration

#: The database the migration chain is run against. Named with the same
#: `_test` suffix the rest of the integration suite insists on, because this
#: one is dropped and recreated.
SCRATCH_DATABASE = "ravel_migrations_test"

#: The repository root, which is where `alembic.ini` lives.
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def migrated(integration_settings) -> Iterator[Engine]:
    """A database at `alembic upgrade head`, built from nothing.

    By subprocess rather than in-process, because the chain reads its URL from
    `Settings` and settings are cached per process: an in-process run would
    either need the environment changed under a live cache or would migrate
    whatever database the suite itself is using.
    """
    admin_url = integration_settings.postgres_dsn.rsplit("/", 1)[0] + "/postgres"
    scratch_url = integration_settings.postgres_dsn.rsplit("/", 1)[0] + f"/{SCRATCH_DATABASE}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {SCRATCH_DATABASE}"))
    admin.dispose()

    environment = {**os.environ, "RAVEL_POSTGRES_DB": SCRATCH_DATABASE}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "the migration chain did not apply to an empty database, which is what "
        f"a deployment does:\n{result.stdout}\n{result.stderr}"
    )

    engine = create_engine(scratch_url)
    yield engine
    engine.dispose()
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DATABASE} WITH (FORCE)"))
    admin.dispose()


def test_the_chain_creates_every_table_the_models_declare(migrated: Engine) -> None:
    # Three tables are expected and none of them is an application table:
    # Alembic's own version record, and the two transition tables the guards
    # keep. They are created by raw DDL rather than by the metadata, so their
    # absence from the models is the design and not drift.
    infrastructure = {"alembic_version", *GUARD_TABLES}
    found = set(inspect(migrated).get_table_names()) - infrastructure
    declared = set(Base.metadata.tables)
    assert not declared - found, (
        "the models declare tables no migration creates, so a deployed database "
        "would be missing them"
    )
    assert not found - declared, (
        "the chain creates tables no model declares, so a deployed database "
        "would carry something nothing reads or writes"
    )


def test_every_table_has_the_columns_the_models_declare(migrated: Engine) -> None:
    inspector = inspect(migrated)
    missing = sorted(
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name not in {found["name"] for found in inspector.get_columns(table.name)}
    )
    assert not missing, f"a deployed database would not have: {', '.join(missing)}"


def test_every_check_constraint_the_models_declare_is_in_force(migrated: Engine) -> None:
    present = _check_constraints(migrated)
    absent, drifted = [], []
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            name = getattr(constraint, "name", None)
            if not isinstance(constraint, CheckConstraint) or not isinstance(name, str):
                continue
            definition = present.get((table.name, name))
            if definition is None:
                absent.append(f"{table.name}.{name}")
                continue
            declared = _literals(str(constraint.sqltext))
            if declared != _literals(definition):
                drifted.append(
                    f"{table.name}.{name} (models: {_listed(declared)}; "
                    f"migrations: {_listed(_literals(definition))})"
                )
    assert not absent, (
        "a deployed database would not enforce: "
        + ", ".join(sorted(absent))
        + ". The models have the rule and the chain does not create it."
    )
    assert not drifted, (
        "a deployed database would enforce a different vocabulary than the "
        "models declare: " + "; ".join(sorted(drifted))
    )


def _check_constraints(engine: Engine) -> dict[tuple[str, str], str]:
    """Every check constraint in the schema, as `(table, name) -> definition`."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT relname, conname, pg_get_constraintdef(pg_constraint.oid) "
                "FROM pg_constraint "
                "JOIN pg_class ON pg_class.oid = pg_constraint.conrelid "
                "WHERE contype = 'c'"
            )
        ).all()
    return {(table, name): definition for table, name, definition in rows}


def _literals(definition: str) -> frozenset[str]:
    """Every string literal a constraint's definition mentions."""
    return frozenset(re.findall(r"'([^']*)'", definition))


def _listed(values: frozenset[str]) -> str:
    return ", ".join(sorted(values)) if values else "none"
