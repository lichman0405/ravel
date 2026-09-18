"""The base every repository is built on.

**Repositories are bound to one project at construction.** That is the whole
authorization story: a repository cannot read or write another project's rows
because every statement it issues is filtered by `project_id` from its scope,
and every record it accepts is checked against that scope before it is staged.
An agent that supplies a `project_id` in a tool call supplies a *value*; the
scope is the *authority*, and the two are compared rather than conflated.

The check is deliberately a hard failure rather than a silent rewrite. A record
aimed at the wrong project is a bug or an attack, and quietly re-homing it
would hide both.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from ravel.domain.base import Record
from ravel.state.mapping import build_row, from_row


class ProjectScopeError(PermissionError):
    """Raised when a record's project does not match the repository's scope."""


class NotFound(LookupError):
    """Raised when a record is absent from the repository's project."""


class ProjectScopedRepository[RecordT: Record]:
    """Reads and writes one kind of record, within one project.

    Subclasses set `row_type` and `record_type`. Everything else — the scoping,
    the conversion, the existence checks — is inherited, so there is one
    implementation of the authorization rule rather than one per repository.
    """

    row_type: ClassVar[type[Any]]
    record_type: ClassVar[type[Any]]

    def __init__(self, session: Session, project_id: str) -> None:
        if not project_id:
            raise ValueError("a repository requires a project scope")
        self.session = session
        self.project_id = project_id

    # ── Authorization ───────────────────────────────────────────────────────

    def _authorize(self, record: Record) -> Record:
        """Refuse a record that belongs to a different project."""
        owner = getattr(record, "project_id", None)
        if owner != self.project_id:
            raise ProjectScopeError(
                f"{type(record).__name__} belongs to project {owner!r}, but this "
                f"repository is scoped to {self.project_id!r}"
            )
        return record

    def _scoped(self, *conditions: Any) -> Any:
        """A `SELECT` restricted to this repository's project."""
        return select(self.row_type).where(
            self.row_type.project_id == self.project_id, *conditions
        )

    # ── Reading ─────────────────────────────────────────────────────────────

    def _one(self, **identity: Any) -> RecordT | None:
        """One record by an identifying column, or `None` if this project has none.

        A record that exists in another project is indistinguishable from one
        that does not exist, which is the correct answer to give a caller
        outside that project.
        """
        conditions = [
            getattr(self.row_type, column) == value for column, value in identity.items()
        ]
        row = self.session.execute(self._scoped(*conditions)).scalars().one_or_none()
        return from_row(self.record_type, row) if row is not None else None

    def get(self, **identity: Any) -> RecordT:
        """One record by identity.

        Raises:
            NotFound: This project has no such record.
        """
        found = self._one(**identity)
        if found is None:
            described = ", ".join(f"{key}={value!r}" for key, value in identity.items())
            raise NotFound(f"no {self.row_type.__name__} with {described} in {self.project_id}")
        return found

    def all(self, **equalities: Any) -> list[RecordT]:
        """Every matching record, oldest first."""
        conditions = [
            getattr(self.row_type, column) == value for column, value in equalities.items()
        ]
        rows = (
            self.session.execute(self._scoped(*conditions).order_by(self._order_by()))
            .scalars()
            .all()
        )
        return [from_row(self.record_type, row) for row in rows]

    def _order_by(self) -> Any:
        """The column list `all()` sorts by. Subclasses override for a better one."""
        return self.row_type.created_at

    # ── Writing ─────────────────────────────────────────────────────────────

    def add(self, record: RecordT) -> RecordT:
        """Stage a record for insertion, after checking its project.

        The record is not in PostgreSQL until the caller's transaction commits.
        """
        self._authorize(record)
        self.session.add(build_row(self.row_type, record))
        return record

    def exists(self, **identity: Any) -> bool:
        """Whether this project has such a record."""
        return self._one(**identity) is not None
