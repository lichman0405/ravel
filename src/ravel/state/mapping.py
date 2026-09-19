"""Domain record ↔ database row.

This module is small on purpose. The schema was designed so that an aggregate
root gets one column per domain field with the *same* name, which means the
conversion is generic: the record's fields are exactly the row's keyword
arguments, and `model_validate` on the row's columns produces exactly the
record. There is no per-entity mapping table to fall out of date, and adding a
field to a domain record without adding its column fails loudly at the first
write rather than silently dropping the value.

Values that are not aggregates — `BudgetLimits`, `AuthorityCheck`,
`CriterionResult`, the list of dependency identifiers — land in JSONB columns
as plain JSON and are re-validated by the domain model on the way back out, so
their shape is still defined in exactly one place.

**One column type decides one dump mode.** A `timestamptz` column wants a
`datetime`; a JSONB column wants something `json.dumps` accepts, and a nested
`datetime` is not that. Since the two kinds sit side by side in the same row,
the mapping cannot pick a mode for the whole record — it picks per column, by
asking the table which columns are JSON. Reading `attempts` back is what proves
the two agree: `model_validate` parses an ISO string into the same `datetime`,
so both spellings round-trip to the same domain object, and only one of them
can be stored.

What this buys, beyond brevity: a round trip through PostgreSQL returns the
*same* validated domain object. The invariant tests in `tests/unit/domain` and
the ones in `tests/integration/state` are therefore testing the same objects,
not two parallel representations of them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import JSON, inspect

from ravel.domain.base import Record


def json_columns(row_type: type[Any]) -> set[str]:
    """The names of the columns PostgreSQL stores as JSON.

    `JSON` is the dialect-independent parent of `JSONB`, and asking the parent
    means a column declared as plain `JSON` is covered by the same rule rather
    than quietly taking the other dump mode and failing at insert time.
    """
    return {
        attribute.key
        for attribute in inspect(row_type).mapper.column_attrs
        if isinstance(attribute.columns[0].type, JSON)
    }


def to_row_data(record: BaseModel, row_type: type[Any]) -> dict[str, Any]:
    """The column values for a domain record, dumped the way its columns need.

    Python mode for everything, then JSON mode for the JSON columns and only
    those. `mode="json"` for the whole record would turn a `timestamptz` value
    into a string that psycopg would have to be trusted to parse back.
    """
    data = record.model_dump(mode="python")
    stored_as_json = json_columns(row_type) & data.keys()
    if stored_as_json:
        as_json = record.model_dump(mode="json")
        for name in stored_as_json:
            data[name] = as_json[name]
    return data


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
    data = to_row_data(record, row_type)
    data.update(extra)
    try:
        return row_type(**data)
    except TypeError as error:
        raise TypeError(
            f"{type(record).__name__} does not map onto {row_type.__name__}: {error}. "
            "A domain field was probably added without its column."
        ) from error
