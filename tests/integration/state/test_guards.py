"""`guards.install` and `guards.drop` are mirror images.

`drop` exists for one caller — the migration's `downgrade` — and it has one
failure mode: leaving an object behind. A leftover trigger makes `DROP FUNCTION`
fail on a dependency, so the downgrade stops with an error that names the
function rather than the omission that caused it.

This is asserted against PostgreSQL's own catalogues rather than against the
statement lists, because the question is what actually exists in the database
after each call.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.integration.conftest import Prepared

from ravel.domain.execution import DeviationRecord
from ravel.state import guards
from ravel.state.database import Database
from ravel.state.repositories.records import DeviationRepository

pytestmark = pytest.mark.integration


def _installed_triggers(database: Database) -> set[tuple[str, str]]:
    """`(table, trigger)` pairs.

    The table is part of the identity: `ravel_append_only` is one trigger name
    attached to twenty different tables, so a set of names alone cannot tell one
    attachment from twenty.
    """
    with database.read_only() as session:
        return {
            (table, trigger)
            for table, trigger in session.execute(
                text(
                    "SELECT c.relname, t.tgname FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE NOT t.tgisinternal AND c.relname <> 'alembic_version'"
                )
            )
        }


def _trigger_names(database: Database) -> set[str]:
    return {trigger for _, trigger in _installed_triggers(database)}


def _installed_functions(database: Database) -> set[str]:
    with database.read_only() as session:
        return set(
            session.execute(
                text(
                    "SELECT p.proname FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = current_schema() AND p.proname LIKE 'ravel\\_%'"
                )
            ).scalars()
        )


def test_install_creates_every_guard_the_module_declares(database: Database) -> None:
    with database.engine.begin() as connection:
        guards.install(connection)

    assert set(guards.GUARD_FUNCTIONS) <= _installed_functions(database)
    assert set(guards.GUARD_TRIGGERS) <= _trigger_names(database)


def test_drop_removes_everything_install_created(database: Database) -> None:
    """The round trip a `downgrade` performs, on the live schema.

    The guards are reinstalled at the end and in the `finally`, because the
    `database` fixture is shared across the session: a test that left the schema
    unguarded would make every later test pass for the wrong reason.
    """
    with database.engine.begin() as connection:
        guards.install(connection)
    assert _installed_functions(database), "install must create something to drop"

    try:
        with database.engine.begin() as connection:
            guards.drop(connection)

        assert _installed_functions(database) == set(), "a function survived the drop"
        assert _installed_triggers(database) == set(), "a trigger survived the drop"
    finally:
        with database.engine.begin() as connection:
            guards.install(connection)

    assert set(guards.GUARD_FUNCTIONS) <= _installed_functions(database)


def test_the_transition_tables_go_with_the_guards(database: Database) -> None:
    """They are created outside the metadata, so only `drop` removes them."""
    with database.engine.begin() as connection:
        guards.drop(connection)
        present = connection.execute(
            text("SELECT to_regclass(:name)"), {"name": "ravel_node_transitions"}
        ).scalar()
        assert present is None
    with database.engine.begin() as connection:
        guards.install(connection)

    with database.read_only() as session:
        assert session.execute(text("SELECT count(*) FROM ravel_node_transitions")).scalar()


def test_install_is_idempotent(database: Database) -> None:
    """It runs from the migration, from `after_create`, and from tests."""
    with database.engine.begin() as connection:
        guards.install(connection)
        before = _installed_triggers(database)
        guards.install(connection)

    assert _installed_triggers(database) == before
    assert (
        len([pair for pair in before if pair[1] == "ravel_append_only"])
        == len(guards.append_only_tables())
    ), "exactly one append-only trigger per append-only table, and no duplicates"


def test_a_deviation_may_only_change_where_it_was_answered(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """The identity trigger's whole job, asserted as behaviour.

    `deviation_records` is updatable for exactly two columns, and the trigger is
    what keeps that from being the whole row. Everything describing the
    escalation — which node raised it, what was asked for, why the contract
    refused, when — stays as fixed as any append-only record's contents, because
    a deviation whose `requested_action` could be edited after Master answered it
    is a record that can be made to agree with any decision taken.

    Written as raw SQL rather than through the repository: the repository is the
    path that is *supposed* to be narrow, and what is being asserted is that the
    database refuses the wide write even when nothing narrows it first.
    """
    prepared = prepare()
    with database.transaction() as session:
        deviation = DeviationRepository(session, prepared.project_id).raise_(
            DeviationRecord(
                project_id=prepared.project_id,
                node_id=prepared.node_id,
                execution_contract_ref=prepared.contract.contract_id,
                requested_action="run_extra_rinse",
                description="the contract does not list this action",
                raised_by="backend:mock-compute",
            )
        )

    with (
        pytest.raises(IntegrityError, match="requested_action is immutable"),
        database.engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE deviation_records SET requested_action = :action "
                "WHERE deviation_id = :deviation_id"
            ),
            {"action": "run_measurement", "deviation_id": deviation.deviation_id},
        )

    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE deviation_records SET resolved_by_decision_ref = :decision, "
                "resolved_at = now() WHERE deviation_id = :deviation_id"
            ),
            {"decision": "dec-1", "deviation_id": deviation.deviation_id},
        )

    with database.read_only() as session:
        assert session.execute(
            text(
                "SELECT resolved_by_decision_ref FROM deviation_records "
                "WHERE deviation_id = :deviation_id"
            ),
            {"deviation_id": deviation.deviation_id},
        ).scalar() == "dec-1"


def test_the_freeze_guard_is_attached_only_where_it_belongs(database: Database) -> None:
    with database.read_only() as session:
        tables = set(
            session.execute(
                text(
                    "SELECT c.relname FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE t.tgname = 'ravel_contract_freeze'"
                )
            ).scalars()
        )
    assert tables == set(guards.FREEZABLE_TABLES)
