"""Alembic environment.

The URL comes from `Settings`, and the metadata comes from `ravel.state.tables`,
so `--autogenerate` diffs the live database against the same definitions the
application uses. A DSN in `alembic.ini` would be a second place it could be
wrong, and a checked-in one could leak a password.

`include_object` deliberately keeps the guard objects out of autogenerate's
view. The transition tables and the trigger functions are installed by
`ravel.state.guards`, not by a migration body, and a diff that tried to drop
and recreate them would either break enforcement or produce churn on every
revision.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from ravel.config import get_settings
from ravel.state.tables import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: Objects `ravel.state.guards` owns. Autogenerate must not see them.
_GUARD_TABLES = {"ravel_node_transitions", "ravel_project_transitions"}


def _database_url() -> str:
    """The DSN to migrate, from process settings."""
    return get_settings().postgres_dsn


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Whether autogenerate should consider an object."""
    return not (type_ == "table" and name in _GUARD_TABLES)


def run_migrations_offline() -> None:
    """Emit the migration SQL to stdout without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run the migration against a live database."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
