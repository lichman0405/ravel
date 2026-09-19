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

**The guards are compared too, and that is a second gap this file closed.** They
are not part of the metadata: `guards.install()` attaches them, and which
triggers it attaches depends on which tables exist *at the moment it is called*.
The initial migration calls it before the tables of later revisions exist, so
each of those revisions has to install again. Nothing checked that they did —
the tables, columns, and constraints all matched while a deployed database could
have been missing every append-only and transition guard on the newest table.
The comparison is against the test database rather than against a recomputation,
because "the deployment enforces what the suite has been asserting against" is
the claim worth making.

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

from ravel.state.database import Database
from ravel.state.guards import GUARD_FUNCTIONS, GUARD_TABLES
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


def test_the_chain_installs_the_guards_the_suite_asserts_against(
    migrated: Engine, database: Database
) -> None:
    """A deployed database carries the same guard triggers as a test one.

    `guards.install()` attaches triggers to the tables PostgreSQL reports at the
    moment it runs, and it is called from the initial migration — where the
    tables added by later revisions do not exist yet. So the chain reaching head
    is not by itself evidence that the newest tables are guarded, and the
    comparison that catches that is against the schema `create_all` plus one
    explicit `install()` produces, which is what every other test in this suite
    has been asserting against all along.

    Both directions are failures. A trigger the deployment lacks is a rule that
    is enforced in the suite and not in production; one it has and the models do
    not is a rule nothing in the code believes in.
    """
    deployed, expected = _guard_triggers(migrated), _guard_triggers(database.engine)
    assert deployed == expected, (
        "a deployed database is guarded differently from the test database. "
        f"Missing: {_listed_pairs(expected - deployed)}. "
        f"Unexpected: {_listed_pairs(deployed - expected)}. "
        "A migration that creates a table must install the guards on it."
    )


def test_the_chain_installs_every_guard_function(migrated: Engine) -> None:
    """The triggers are only half of it; the functions they call are the other.

    A trigger whose function was never created fails at the first write rather
    than at migrate time, which is the worst moment to find out.
    """
    with migrated.connect() as connection:
        present = set(
            connection.execute(
                text(
                    "SELECT p.proname FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = current_schema()"
                )
            ).scalars()
        )
    assert not set(GUARD_FUNCTIONS) - present, (
        "a deployed database is missing the functions its guard triggers call: "
        + ", ".join(sorted(set(GUARD_FUNCTIONS) - present))
    )


def _guard_triggers(engine: Engine) -> set[tuple[str, str]]:
    """Every user-defined trigger in the schema, as `(table, trigger)`.

    Internal triggers are excluded: PostgreSQL creates them for foreign keys and
    constraints, they are not ours, and their names are not stable across
    versions.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT c.relname, t.tgname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE NOT t.tgisinternal AND n.nspname = current_schema()"
            )
        ).all()
    return {(table, trigger) for table, trigger in rows}


def _listed_pairs(pairs: set[tuple[str, str]]) -> str:
    """A set of `(table, trigger)` as readable text for an error message."""
    return ", ".join(f"{table}.{trigger}" for table, trigger in sorted(pairs)) or "none"


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
