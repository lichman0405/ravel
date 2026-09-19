"""Every record and its table agree about what the record is.

This is a unit test — no database — and it exists because of a bug it would
have caught: `evidence_sources.project_id` is `NOT NULL`, but `EvidenceSource`
did not declare it. Nothing failed at import time, nothing failed in the unit
suite, and the repository looked correct. The failure would have arrived the
first time a Research Agent tried to record a source: a `NOT NULL` violation,
or a `ProjectScopeError` from a record that had no project to compare.

The invariant is equality in both directions, and both directions have teeth:

- A column the record does not declare is either silently dropped on write
  (nullable) or a runtime failure (not nullable).
- A field the record declares with no column fails on write, in `build_row`,
  with a `TypeError` that says a field was added without its column.

Keeping the two in step is cheap to check and expensive to discover.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import DeclarativeBase

from ravel.state import guards
from ravel.state.repositories import ProjectScopedRepository
from ravel.state.tables import Base


def _repository_classes() -> list[type[ProjectScopedRepository]]:
    """Every concrete repository, however deep it sits in the hierarchy."""
    found: set[type[ProjectScopedRepository]] = set()
    pending = list(ProjectScopedRepository.__subclasses__())
    while pending:
        cls = pending.pop()
        if cls in found:
            continue
        found.add(cls)
        pending.extend(cls.__subclasses__())
    return sorted(
        (cls for cls in found if hasattr(cls, "row_type")), key=lambda cls: cls.__name__
    )


def test_the_repository_list_is_not_empty() -> None:
    """A discovery walk that found nothing would pass every test below."""
    classes = _repository_classes()
    assert len(classes) >= 15, [cls.__name__ for cls in classes]


@pytest.mark.parametrize("repository", _repository_classes(), ids=lambda cls: cls.__name__)
def test_a_records_fields_are_exactly_its_tables_columns(
    repository: type[ProjectScopedRepository],
) -> None:
    table = repository.row_type.__table__
    columns = {column.name for column in table.columns}
    fields = set(repository.record_type.model_fields)

    assert fields == columns, (
        f"{repository.record_type.__name__} and {table.name} disagree. "
        f"In the table but not the record: {sorted(columns - fields)}. "
        f"In the record but not the table: {sorted(fields - columns)}."
    )


@pytest.mark.parametrize("repository", _repository_classes(), ids=lambda cls: cls.__name__)
def test_every_scoped_record_carries_the_project_it_belongs_to(
    repository: type[ProjectScopedRepository],
) -> None:
    """The scope check reads `record.project_id`; a record without one cannot be
    authorized at all, and `_authorize` would refuse every instance."""
    assert "project_id" in repository.record_type.model_fields, (
        f"{repository.record_type.__name__} has no project_id, so nothing can "
        f"prove it belongs to the repository's scope"
    )
    assert "project_id" in {column.name for column in repository.row_type.__table__.columns}


@pytest.mark.parametrize("repository", _repository_classes(), ids=lambda cls: cls.__name__)
def test_every_repository_can_order_what_it_returns(
    repository: type[ProjectScopedRepository],
) -> None:
    """`all()` sorts by the column `_order_by` names, and the base names
    `created_at`.

    `DeviationRepository` is why this exists. A deviation records when it was
    *raised*, so the table has `raised_at` and no `created_at` — and the
    repository inherited the base `_order_by` anyway. Nothing failed until
    `ProjectAudit` asked a project for its deviations: `AttributeError`, from
    inside a sort, on a table that was populated correctly.

    The class of mistake is "the default assumed a column this record does not
    have", and it is invisible to every test that does not call `all()`. So the
    check is the expression itself, evaluated on a stand-in that carries only
    what `_order_by` reads.
    """
    stand_in = SimpleNamespace(row_type=repository.row_type)
    column = repository._order_by(stand_in)  # type: ignore[arg-type]

    table = repository.row_type.__table__
    assert column is not None, f"{repository.__name__} orders by nothing"
    assert column.table is table, (
        f"{repository.__name__} orders by {column}, which is not a column of "
        f"{table.name}; `all()` would raise AttributeError rather than sort"
    )


def test_every_table_is_either_append_only_or_explicitly_updatable() -> None:
    """The guard sets partition the schema; an unclassified table would be one
    that `install` silently leaves unguarded."""
    declared = set(Base.metadata.tables)
    updatable = set(guards.UPDATABLE_TABLES)

    assert updatable <= declared, f"unknown tables: {sorted(updatable - declared)}"
    assert guards.append_only_tables() == declared - updatable


def test_the_freezable_tables_are_updatable_and_otherwise_immutable() -> None:
    """A table whose one mutable column is `frozen_at` must be updatable, or the
    blanket append-only guard would refuse the freeze itself — which is exactly
    how the freeze path was broken before this test existed."""
    for table in guards.FREEZABLE_TABLES:
        assert table in guards.UPDATABLE_TABLES, f"{table} must permit its freeze write"
        assert table in Base.metadata.tables, f"{table} is not a declared table"


def test_no_declared_table_is_left_out_of_the_metadata() -> None:
    """`install` iterates the metadata; a table outside it gets no guard."""
    assert isinstance(Base, type) and issubclass(Base, DeclarativeBase)
    assert len(Base.metadata.tables) >= 25
