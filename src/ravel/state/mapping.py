"""Domain record ↔ database row.

This module is small on purpose. The schema was designed so that an aggregate
root gets one column per domain field with the *same name*, which means the
conversion is generic: `model_dump(mode="python")` produces exactly the row's
keyword arguments, and `model_validate` on the row's columns produces exactly
the record. There is no per-entity mapping table to fall out of date, and
adding a field to a domain record without adding its column fails loudly at the
first write rather than silently dropping the value.

Values that are not aggregates — `BudgetLimits`, `AuthorityCheck`,
`CriterionResult`, the list of dependency identifiers — land in JSONB columns
as plain JSON and are re-validated by the domain model on the way back out, so
their shape is still defined in exactly one place.

What this buys, beyond brevity: a round trip through PostgreSQL returns the
*same* validated domain object. The invariant tests in `tests/unit/domain` and
the ones in `tests/integration/state` are therefore testing the same objects,
not two parallel representations of them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import inspect

from ravel.domain.base import Record


def to_row_data(record: BaseModel) -> dict[str, Any]:
    """The column values for a domain record.

    `mode="python"` rather than `mode="json"`: datetimes stay `datetime` for
    the `timestamptz` columns, `StrEnum` members stay str subclasses that
    psycopg adapts as text, and tuples stay tuples that the JSONB serializer
    writes as arrays.
    """
    return record.model_dump(mode="python")


def from_row[RecordT: Record](record_type: type[RecordT], row: Any) -> RecordT:
    """Rebuild a domain record from an ORM row.

    Columns the record does not declare are ignored — they are bookkeeping the
    table needs and the domain does not model. Fields the record declares but
    the row lacks are a genuine error and raise, because a record constructed
    with a missing field would fall back to a default and quietly disagree with
    what was stored.
    """
    columns = {
        attribute.key: getattr(row, attribute.key)
        for attribute in inspect(row).mapper.column_attrs
    }
    known = record_type.model_fields
    return record_type.model_validate(
        {name: value for name, value in columns.items() if name in known}
    )


def build_row(row_type: type[Any], record: BaseModel, **extra: Any) -> Any:
    """Construct an ORM row from a domain record.

    Raises:
        TypeError: The record has a field the table has no column for. This is
            the loud failure the module docstring promises; it means a domain
            field was added without a migration.
    """
    data = to_row_data(record)
    data.update(extra)
    try:
        return row_type(**data)
    except TypeError as error:
        raise TypeError(
            f"{type(record).__name__} does not map onto {row_type.__name__}: {error}. "
            "A domain field was probably added without its column."
        ) from error
