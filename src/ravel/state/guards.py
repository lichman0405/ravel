"""Database-level guards: the rules that hold even when application code is wrong.

Three guarantees live here rather than in Python, because a guarantee that a
bug can bypass is not a guarantee:

1. **Illegal status transitions are rejected by PostgreSQL.** The transition
   tables are *populated from* `ravel.domain.state_machines`, so the Python
   table remains the single source of truth, but the enforcement happens in a
   trigger that no repository, migration, or manual `UPDATE` can skip.

2. **Append-only tables reject UPDATE and DELETE.** A Decision Record, a Review
   Record, an Evidence row, an event — once written, they are history. The rule
   is uniform: no row in RAVEL is ever updated in place to say something else,
   and no row is ever deleted. Test teardown truncates; nothing else removes
   state.

3. **A DAG node's identity columns cannot change.** A node's objective, type,
   dependencies, and author are fixed at creation. Changing what a node is for
   means opening a new node, because results already produced belong to the old
   one. Only the lifecycle columns may move.

`install()` is idempotent (`CREATE OR REPLACE`) and is called both from the
Alembic migration and from `after_create` on the metadata, so a test schema and
a migrated schema enforce exactly the same thing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ravel.domain.state_machines import NODE_TRANSITIONS, PROJECT_TRANSITIONS

#: The tables where `UPDATE` is a legitimate operation. Everything else in
#: RAVEL is append-only.
#:
#: Most of these carry a life cycle — a project's status, a node's status, an
#: agent's last-seen time, an approval's resolution, a contract's freeze, a
#: backend job's state, a handover's — and each is moved by a domain method with
#: a state machine behind it. One is the event counter, which is infrastructure
#: rather than a record: it holds one integer and is advanced by
#: `UPDATE ... RETURNING`.
#:
#: `project_memberships` is here for its two revocation columns, and it is the
#: one entry in the list whose UPDATE is not a transition: a membership does
#: not move through states, it is live and then it is withdrawn. The identity
#: trigger below is what keeps that the only thing that may change — in
#: particular `role`, so that a role change is a new row rather than an edit to
#: the one that says who granted what.
#:
#: The two contract tables are here for one column only. A contract's terms are
#: fixed the moment it is written; `frozen_at` records *when* they became
#: binding, and freezing is a one-way transition. Adding them without the
#: accompanying trigger would open every column to rewriting, which is why
#: `ravel_protect_contract_freeze` is attached alongside.
#:
#: `backend_jobs` is here for its state column, and it is the one table whose
#: identity guard is not merely tidy: `attempt` is what makes starting a job
#: idempotent, so a row whose attempt could be rewritten would let one job
#: stand in for another.
#:
#: `deviation_records` is here for its two resolution columns, and it was
#: missing from this list while `DeviationRepository.resolve` wrote them — so
#: every resolution would have been refused by the append-only trigger, and the
#: mistake was invisible for exactly as long as nothing called it. What a
#: deviation *asked for* is as fixed as any other record's contents; what may
#: change is which decision answered it, and the identity trigger below is what
#: keeps that the only thing.
#:
#: `runtime_services` is the one entry that is not a record at all. It holds a
#: process's account of itself — when it started, when it last spoke — and the
#: whole row is meant to be rewritten on every beat, so it is here because
#: `UPDATE` is its only write. What the identity trigger protects is the row's
#: *name*, so that a process cannot make a dead service look alive by beating
#: on its row.
UPDATABLE_TABLES: frozenset[str] = frozenset(
    {
        "projects",
        "dag_nodes",
        "agent_identities",
        "approval_requests",
        "acceptance_contracts",
        "execution_contracts",
        "backend_jobs",
        "lab_handovers",
        "deviation_records",
        "project_memberships",
        "project_event_counters",
        "runtime_services",
    }
)

#: The tables whose only mutable column is `frozen_at`.
FREEZABLE_TABLES: tuple[str, ...] = ("acceptance_contracts", "execution_contracts")

_IDENTITY_FUNCTION = """
CREATE OR REPLACE FUNCTION ravel_protect_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    column_name text;
    old_row jsonb := to_jsonb(OLD);
    new_row jsonb := to_jsonb(NEW);
BEGIN
    -- The immutable column list arrives as the trigger's arguments, so one
    -- function serves every table that has identity columns and the list sits
    -- next to the CREATE TRIGGER that uses it rather than inside a function
    -- body. TG_ARGV[1] is the column the message names the row by.
    FOREACH column_name IN ARRAY TG_ARGV LOOP
        IF new_row -> column_name IS DISTINCT FROM old_row -> column_name THEN
            RAISE EXCEPTION
                '%.% is immutable; % % cannot be redefined in place',
                TG_TABLE_NAME, column_name, TG_TABLE_NAME,
                old_row ->> TG_ARGV[1]
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END $$;
"""

#: Columns on `dag_nodes` that a transition may change. Anything else is
#: part of the node's identity and is fixed for the node's lifetime.
DAG_NODE_IDENTITY_COLUMNS: tuple[str, ...] = (
    "node_id",
    "display_id",
    "project_id",
    "node_type",
    "objective",
    "executor_role",
    "dependencies",
    "join_policy",
    "join_threshold",
    "failure_policy",
    "roadmap_phase",
    "created_by",
    "created_at",
)

#: Columns on `backend_jobs` that a state change may not touch. `job_id` is
#: first because it is what the trigger names the row by.
BACKEND_JOB_IDENTITY_COLUMNS: tuple[str, ...] = (
    "job_id",
    "project_id",
    "node_id",
    "attempt",
    "execution_contract_ref",
    "execution_contract_version",
    "backend",
    "submitted_at",
)

#: Columns on `lab_handovers` that a state change may not touch. `handover_id`
#: is first because it is what the trigger names the row by.
#:
#: The mutable columns are `state`, `detail` and `closed_at` — a bench finishes,
#: or RAVEL gives up waiting — and what is absent from this list is the point.
#: `required_outputs` is what a delivery is checked against, so a handover whose
#: owed outputs could be rewritten after the bench delivered would let the
#: check be made to agree with whatever arrived. `execution_contract_version`
#: is what makes the run identifiable; `preparation_id` is the package the
#: bench was actually given, which is what makes a result traceable to a
#: document rather than to whichever preparation happened to be newest.
LAB_HANDOVER_IDENTITY_COLUMNS: tuple[str, ...] = (
    "handover_id",
    "project_id",
    "node_id",
    "attempt",
    "backend",
    "execution_contract_ref",
    "execution_contract_version",
    "preparation_id",
    "workspace_path",
    "protocol",
    "required_outputs",
    "handed_over_at",
)

#: Columns on `deviation_records` that its one permitted UPDATE may not touch.
#: `deviation_id` is first because it is what the trigger names the row by.
#:
#: What is absent from this list is the point: `resolved_by_decision_ref` and
#: `resolved_at` are the two columns `DeviationRepository.resolve` writes, and
#: everything describing the escalation itself — which node raised it, what was
#: asked for, and why the contract refused — is fixed. A deviation whose
#: `requested_action` could be edited after Master answered it would let the
#: record be made to agree with whatever decision was taken.
DEVIATION_IDENTITY_COLUMNS: tuple[str, ...] = (
    "deviation_id",
    "project_id",
    "node_id",
    "execution_contract_ref",
    "requested_action",
    "description",
    "permitted",
    "raised_by",
    "raised_at",
)

#: Columns on `project_memberships` that its one permitted UPDATE may not
#: touch. `membership_id` is first because it is what the trigger names the row
#: by.
#:
#: The mutable columns are `revoked_at` and `revoked_by`, and the absence of
#: `role` from this list is the design. A role change is a revocation and a
#: grant rather than an edit: a row rewritten from LAB_USER to PROJECT_OWNER
#: would let one row stand for two different grants of authority, and the
#: `MEMBER_ADDED` in the stream would no longer say what was actually given.
#: `granted_by` is fixed for the same reason in the other direction — who
#: conferred the authority is part of what happened.
MEMBERSHIP_IDENTITY_COLUMNS: tuple[str, ...] = (
    "membership_id",
    "project_id",
    "user_id",
    "role",
    "granted_at",
    "granted_by",
)

#: The one column on `runtime_services` a heartbeat may not touch.
#:
#: Everything else on the row is the reporting process's own account of itself
#: — which instance it is, when it started, when it last spoke — and all of it
#: is meant to be rewritten on every beat, which is why this list is one name
#: long rather than the usual "everything except the state columns". `service`
#: is the row's identity, so protecting it is what stops a process from taking
#: over *another* service's row and making a dead supervisor look alive by
#: writing `temporal-worker`'s beat onto it.
RUNTIME_SERVICE_IDENTITY_COLUMNS: tuple[str, ...] = ("service",)

_TRANSITION_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS ravel_node_transitions (
    from_status text NOT NULL,
    to_status   text NOT NULL,
    PRIMARY KEY (from_status, to_status)
)
"""

_PROJECT_TRANSITION_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS ravel_project_transitions (
    from_status text NOT NULL,
    to_status   text NOT NULL,
    PRIMARY KEY (from_status, to_status)
)
"""

_NODE_TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION ravel_enforce_node_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status
       AND NOT EXISTS (
           SELECT 1 FROM ravel_node_transitions t
           WHERE t.from_status = OLD.status AND t.to_status = NEW.status
       )
    THEN
        RAISE EXCEPTION
            'illegal node transition % -> % on node %',
            OLD.status, NEW.status, OLD.node_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
"""

_PROJECT_TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION ravel_enforce_project_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status
       AND NOT EXISTS (
           SELECT 1 FROM ravel_project_transitions t
           WHERE t.from_status = OLD.status AND t.to_status = NEW.status
       )
    THEN
        RAISE EXCEPTION
            'illegal project transition % -> % on project %',
            OLD.status, NEW.status, OLD.project_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
"""

_REJECT_WRITE_FUNCTION = """
CREATE OR REPLACE FUNCTION ravel_reject_write() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '% is append-only; % is not permitted on this table',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'check_violation';
END $$;
"""

_REJECT_DELETE_FUNCTION = """
CREATE OR REPLACE FUNCTION ravel_reject_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '% rows are never deleted; % is not permitted',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'check_violation';
END $$;
"""

#: A contract's terms are fixed when it is written. The only thing that may
#: ever change is `frozen_at`, and only once — so this is expressed as "every
#: column except `frozen_at` is immutable" rather than as a list of protected
#: columns that a new column would silently escape.
#:
#: `to_jsonb(NEW) - 'frozen_at'` removes the one key and compares the rest, so
#: a column added to either table later is immutable by default. The mistake
#: that is easy to make — adding a mutable field and forgetting the guard —
#: fails closed rather than open.
_CONTRACT_FREEZE_FUNCTION = """
CREATE OR REPLACE FUNCTION ravel_protect_contract_freeze() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - 'frozen_at') IS DISTINCT FROM (to_jsonb(OLD) - 'frozen_at') THEN
        RAISE EXCEPTION
            '% is immutable; only frozen_at may change on contract %',
            TG_TABLE_NAME, OLD.contract_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.frozen_at IS NOT NULL AND NEW.frozen_at IS DISTINCT FROM OLD.frozen_at THEN
        RAISE EXCEPTION
            'contract % was frozen at %; freezing is one-way and cannot be redone',
            OLD.contract_id, OLD.frozen_at
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
"""

#: Trigger names, so `install` can drop before creating and stay idempotent.
_NODE_TRANSITION_TRIGGER = "ravel_dag_nodes_transition"
_PROJECT_TRANSITION_TRIGGER = "ravel_projects_transition"
_DAG_NODE_IDENTITY_TRIGGER = "ravel_dag_nodes_identity"
_BACKEND_JOB_IDENTITY_TRIGGER = "ravel_backend_jobs_identity"
_LAB_HANDOVER_IDENTITY_TRIGGER = "ravel_lab_handovers_identity"
_DEVIATION_IDENTITY_TRIGGER = "ravel_deviation_records_identity"
_MEMBERSHIP_IDENTITY_TRIGGER = "ravel_project_memberships_identity"
_RUNTIME_SERVICE_IDENTITY_TRIGGER = "ravel_runtime_services_identity"
_APPEND_ONLY_TRIGGER = "ravel_append_only"
_NO_DELETE_TRIGGER = "ravel_no_delete"
_CONTRACT_FREEZE_TRIGGER = "ravel_contract_freeze"

#: Every object `install` can create, in one place so `drop` cannot miss one.
#: A hand-written drop list drifts from the install list, and the failure it
#: produces — `DROP FUNCTION` refused because a forgotten trigger still depends
#: on it — points at the symptom rather than at the omission.
GUARD_TRIGGERS: tuple[str, ...] = (
    _NODE_TRANSITION_TRIGGER,
    _PROJECT_TRANSITION_TRIGGER,
    _DAG_NODE_IDENTITY_TRIGGER,
    _BACKEND_JOB_IDENTITY_TRIGGER,
    _LAB_HANDOVER_IDENTITY_TRIGGER,
    _DEVIATION_IDENTITY_TRIGGER,
    _MEMBERSHIP_IDENTITY_TRIGGER,
    _RUNTIME_SERVICE_IDENTITY_TRIGGER,
    _APPEND_ONLY_TRIGGER,
    _NO_DELETE_TRIGGER,
    _CONTRACT_FREEZE_TRIGGER,
)

GUARD_FUNCTIONS: tuple[str, ...] = (
    "ravel_enforce_node_transition",
    "ravel_enforce_project_transition",
    "ravel_protect_identity",
    "ravel_protect_contract_freeze",
    "ravel_reject_write",
    "ravel_reject_delete",
)

#: Guard functions that earlier revisions created under another name. One
#: function now protects the identity columns of every table that has them, so
#: the node-specific one it replaced is dropped rather than left behind: a
#: database that has been migrated would otherwise keep a function that looks
#: like an active guard and is called by nothing.
_SUPERSEDED_FUNCTIONS: tuple[str, ...] = ("ravel_protect_dag_node_identity",)

#: Lookup tables holding the transition rules, created outside the metadata.
GUARD_TABLES: tuple[str, ...] = ("ravel_node_transitions", "ravel_project_transitions")


def _trigger_ddl(
    *,
    name: str,
    table: str,
    function: str,
    events: str,
    when: str = "BEFORE",
    for_each: str = "ROW",
    arguments: tuple[str, ...] = (),
) -> str:
    """The DDL for one trigger.

    `arguments` are passed to the trigger function as `TG_ARGV`. Literal
    quoting is safe here: every caller supplies column names from a module
    constant, never from input.
    """
    passed = "".join(f", '{argument}'" for argument in arguments)
    return (
        f"DROP TRIGGER IF EXISTS {name} ON {table};\n"
        f"CREATE TRIGGER {name} {when} {events} ON {table} "
        f"FOR EACH {for_each} EXECUTE FUNCTION {function}({passed.lstrip(', ')});"
    )


def _transition_rows(
    transitions: dict[Any, frozenset[Any]],
) -> list[tuple[str, str]]:
    """Flatten a Python transition table into `(from, to)` string pairs."""
    return [
        (source.value, target.value)
        for source, targets in transitions.items()
        for target in sorted(targets)
    ]


def install(bind: Any, table_names: frozenset[str] | None = None) -> None:
    """Create the guard functions and attach them to the tables that exist.

    Idempotent: every object is created with `OR REPLACE` or dropped first, so
    this can run on an empty database, on an existing one, and in a test
    schema that already has the tables.

    **Only tables that are actually present are touched.** `table_names` is the
    set worth guarding — every table the metadata declares, unless a caller
    knows better — and it is intersected with what PostgreSQL reports. The
    intersection is not a refinement: `CREATE TRIGGER ... ON <table>` fails
    outright when the table is absent, and the metadata describes the schema at
    the end of the migration chain while an individual migration runs in the
    middle of it. A fresh `alembic upgrade head` calls this from the initial
    migration, where the tables added by later revisions are declared but not
    yet created, and without the intersection the whole chain fails on the
    first table that a later migration introduces.

    Args:
        bind: A SQLAlchemy `Connection` or `Engine`.
        table_names: The tables worth guarding. Defaults to every table the
            metadata declares; the ones that do not exist yet are skipped.
    """
    if table_names is None:
        table_names = _declared_table_names()

    statements: list[str] = [
        _TRANSITION_TABLE_DDL,
        _PROJECT_TRANSITION_TABLE_DDL,
        _NODE_TRIGGER_FUNCTION,
        _PROJECT_TRIGGER_FUNCTION,
        _IDENTITY_FUNCTION,
        _REJECT_WRITE_FUNCTION,
        _REJECT_DELETE_FUNCTION,
        _CONTRACT_FREEZE_FUNCTION,
        *(
            f"DROP FUNCTION IF EXISTS {name}() CASCADE;"
            for name in _SUPERSEDED_FUNCTIONS
        ),
    ]

    declared = _declared_table_names()
    existing = {name for name in table_names if name in declared} & _existing_tables(bind)

    if "dag_nodes" in existing:
        statements.append(
            _trigger_ddl(
                name=_NODE_TRANSITION_TRIGGER,
                table="dag_nodes",
                function="ravel_enforce_node_transition",
                events="UPDATE",
            )
        )
        statements.append(
            _trigger_ddl(
                name=_DAG_NODE_IDENTITY_TRIGGER,
                table="dag_nodes",
                function="ravel_protect_identity",
                events="UPDATE",
                arguments=DAG_NODE_IDENTITY_COLUMNS,
            )
        )

    if "backend_jobs" in existing:
        statements.append(
            _trigger_ddl(
                name=_BACKEND_JOB_IDENTITY_TRIGGER,
                table="backend_jobs",
                function="ravel_protect_identity",
                events="UPDATE",
                arguments=BACKEND_JOB_IDENTITY_COLUMNS,
            )
        )

    if "lab_handovers" in existing:
        statements.append(
            _trigger_ddl(
                name=_LAB_HANDOVER_IDENTITY_TRIGGER,
                table="lab_handovers",
                function="ravel_protect_identity",
                events="UPDATE",
                arguments=LAB_HANDOVER_IDENTITY_COLUMNS,
            )
        )

    if "deviation_records" in existing:
        statements.append(
            _trigger_ddl(
                name=_DEVIATION_IDENTITY_TRIGGER,
                table="deviation_records",
                function="ravel_protect_identity",
                events="UPDATE",
                arguments=DEVIATION_IDENTITY_COLUMNS,
            )
        )

    if "project_memberships" in existing:
        statements.append(
            _trigger_ddl(
                name=_MEMBERSHIP_IDENTITY_TRIGGER,
                table="project_memberships",
                function="ravel_protect_identity",
                events="UPDATE",
                arguments=MEMBERSHIP_IDENTITY_COLUMNS,
            )
        )

    if "runtime_services" in existing:
        statements.append(
            _trigger_ddl(
                name=_RUNTIME_SERVICE_IDENTITY_TRIGGER,
                table="runtime_services",
                function="ravel_protect_identity",
                events="UPDATE",
                arguments=RUNTIME_SERVICE_IDENTITY_COLUMNS,
            )
        )

    if "projects" in existing:
        statements.append(
            _trigger_ddl(
                name=_PROJECT_TRANSITION_TRIGGER,
                table="projects",
                function="ravel_enforce_project_transition",
                events="UPDATE",
            )
        )

    for table in sorted(existing):
        # Every guard name is dropped on every table before the applicable one
        # is created. Without this, a table that moved between `UPDATABLE_TABLES`
        # and its complement — or into `FREEZABLE_TABLES` — would keep the
        # trigger it no longer warrants: `CREATE TRIGGER` does not replace, and
        # only the new name would be dropped.
        statements.append(
            f"DROP TRIGGER IF EXISTS {_APPEND_ONLY_TRIGGER} ON {table};\n"
            f"DROP TRIGGER IF EXISTS {_NO_DELETE_TRIGGER} ON {table};\n"
            f"DROP TRIGGER IF EXISTS {_CONTRACT_FREEZE_TRIGGER} ON {table};"
        )
        # `FOR EACH STATEMENT`, not `FOR EACH ROW`. A row-level trigger fires
        # once per affected row, so it does not fire at all on an empty table —
        # `DELETE FROM decision_records` against a fresh schema would succeed
        # while the rule claimed to forbid it. A statement-level trigger fires
        # whether or not any row matches, which is what "this table refuses
        # deletes" has to mean.
        if table in UPDATABLE_TABLES:
            statements.append(
                _trigger_ddl(
                    name=_NO_DELETE_TRIGGER,
                    table=table,
                    function="ravel_reject_delete",
                    events="DELETE",
                    for_each="STATEMENT",
                )
            )
        else:
            statements.append(
                _trigger_ddl(
                    name=_APPEND_ONLY_TRIGGER,
                    table=table,
                    function="ravel_reject_write",
                    events="UPDATE OR DELETE",
                    for_each="STATEMENT",
                )
            )
        if table in FREEZABLE_TABLES:
            statements.append(
                _trigger_ddl(
                    name=_CONTRACT_FREEZE_TRIGGER,
                    table=table,
                    function="ravel_protect_contract_freeze",
                    events="UPDATE",
                )
            )

    for statement in statements:
        bind.execute(text(statement))  # type: ignore[attr-defined]

    _sync_transition_tables(bind)


def _sync_transition_tables(bind: Any) -> None:
    """Rewrite the SQL transition tables to match the Python state machines.

    A full rewrite rather than an incremental sync: this is called on every
    install, the tables have a handful of rows, and "delete everything and
    reinsert" cannot leave a stale row behind that a removal in Python would
    otherwise leave enforceable.
    """
    for table, transitions in (
        ("ravel_node_transitions", NODE_TRANSITIONS),
        ("ravel_project_transitions", PROJECT_TRANSITIONS),
    ):
        bind.execute(text(f"DELETE FROM {table}"))  # type: ignore[attr-defined]
        rows = _transition_rows(transitions)
        if not rows:
            continue
        bind.execute(  # type: ignore[attr-defined]
            text(f"INSERT INTO {table} (from_status, to_status) VALUES (:f, :t)"),
            [{"f": source, "t": target} for source, target in rows],
        )


def _declared_table_names() -> frozenset[str]:
    """Every table this process knows about, from the shared metadata."""
    from ravel.state.tables import Base

    return frozenset(Base.metadata.tables)


def _existing_tables(bind: Any) -> set[str]:
    """Every table present in the current schema, asked of PostgreSQL.

    Asked rather than assumed: `DROP TRIGGER ... ON <table>` fails outright if
    the table is absent, and after a partial migration the metadata and the
    database disagree about what exists.
    """
    from sqlalchemy import text as _text

    rows = bind.execute(  # type: ignore[attr-defined]
        _text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    ).scalars()
    return set(rows)


def drop(bind: Any, table_names: frozenset[str] | None = None) -> None:
    """Remove every guard object `install` may have created.

    The exact mirror of `install`, driven by the same name tuples. Used by the
    migration's `downgrade`, which must remove the functions before the tables
    they are attached to — `DROP FUNCTION` refuses while a trigger depends on
    it, so leaving one behind turns a clean downgrade into a dependency error.
    """
    from sqlalchemy import text as _text

    if table_names is None:
        table_names = _declared_table_names()
    present = _existing_tables(bind) | set(table_names)

    for table in sorted(present):
        for trigger in GUARD_TRIGGERS:
            bind.execute(  # type: ignore[attr-defined]
                _text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
            )
    for function in (*GUARD_FUNCTIONS, *_SUPERSEDED_FUNCTIONS):
        bind.execute(_text(f"DROP FUNCTION IF EXISTS {function}()"))  # type: ignore[attr-defined]
    for table in GUARD_TABLES:
        bind.execute(_text(f"DROP TABLE IF EXISTS {table}"))  # type: ignore[attr-defined]


def append_only_tables() -> frozenset[str]:
    """The tables that reject UPDATE and DELETE outright."""
    return frozenset(_declared_table_names() - UPDATABLE_TABLES)


def expected_transition_rows() -> dict[str, set[tuple[str, str]]]:
    """The transition rows the database should hold, for a drift test.

    Read back from the database and compared against this, so a state-machine
    change that was never migrated fails the suite rather than silently
    leaving the old rule enforced in PostgreSQL.
    """
    return {
        "ravel_node_transitions": set(_transition_rows(NODE_TRANSITIONS)),
        "ravel_project_transitions": set(_transition_rows(PROJECT_TRANSITIONS)),
    }
