"""Illegal state transitions are rejected — by PostgreSQL, not only by Python.

The unit suite proves the transition table is right. These tests prove the
table is *enforced*, by issuing raw `UPDATE` statements that bypass every
repository, every domain model, and every line of RAVEL's own code. If the
guard were only in Python, all of these would succeed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from ravel.domain.contracts import (
    AcceptanceContract,
    AcceptanceCriterion,
    CriterionProvenance,
    ExecutionContract,
)
from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, NodeType, ProjectStatus
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import NODE_TRANSITIONS
from ravel.state.database import Database
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry

pytestmark = pytest.mark.integration


def _a_node(session, project_id: str) -> DagNode:
    dag = DagRepository(session, project_id)
    return dag.add_node(
        DagNode.create(
            project_id=project_id,
            node_type=NodeType.RESEARCH,
            objective="Survey known dopants for the target lattice.",
            created_by="master",
        ),
        role=AgentRole.MASTER,
        decision_ref="dec-bootstrap",
    )


def _set_status(database: Database, table: str, key: str, value: str, status: str) -> None:
    """A raw UPDATE that bypasses every repository.

    The timestamps are written alongside the status because the schema requires
    them to agree — a node in RUNNING has a start time, a node that finished
    has a completion time. Reproducing the domain layer's stamping here is what
    lets these tests isolate the *transition* rule: everything else about the
    row is consistent, so the only thing that can reject the write is the
    transition guard.
    """
    assignments = ["status = :status"]
    if table == "dag_nodes":
        if status in {"RUNNING", "WAITING_EXTERNAL", "WAITING_DECISION", "REVIEWING"}:
            assignments.append("started_at = COALESCE(started_at, now())")
        if status in {"PASSED", "FAILED", "PARTIAL"}:
            assignments.append("started_at = COALESCE(started_at, now())")
            assignments.append("completed_at = now()")
    with database.transaction() as session:
        session.execute(
            text(f"UPDATE {table} SET {', '.join(assignments)} WHERE {key} = :key"),
            {"status": status, "key": value},
        )


# ── DAG nodes ───────────────────────────────────────────────────────────────


def test_a_skipped_stage_is_rejected_by_the_database(database: Database, project) -> None:
    """PLANNED straight to RUNNING never passed through the queue."""
    with database.transaction() as session:
        node = _a_node(session, project.project_id)

    with pytest.raises(DatabaseError, match="illegal node transition"):
        _set_status(database, "dag_nodes", "node_id", node.node_id, "RUNNING")


def test_a_result_that_was_never_reviewed_cannot_pass(database: Database, project) -> None:
    with database.transaction() as session:
        node = _a_node(session, project.project_id)

    with pytest.raises(DatabaseError, match="illegal node transition"):
        _set_status(database, "dag_nodes", "node_id", node.node_id, "PASSED")


def test_the_legal_path_is_permitted(database: Database, project) -> None:
    """The guard must not be a wall — the documented path still works."""
    with database.transaction() as session:
        node = _a_node(session, project.project_id)

    for status in ("READY", "RUNNING", "REVIEWING", "PASSED"):
        _set_status(database, "dag_nodes", "node_id", node.node_id, status)

    with database.read_only() as session:
        stored = DagRepository(session, project.project_id).node(node.node_id)
    assert stored.status is NodeStatus.PASSED
    assert stored.completed_at is not None


def test_a_terminal_node_cannot_be_reopened(database: Database, project) -> None:
    """This is what makes the acceptance-criteria freeze meaningful."""
    with database.transaction() as session:
        node = _a_node(session, project.project_id)

    for status in ("READY", "RUNNING", "REVIEWING", "PASSED"):
        _set_status(database, "dag_nodes", "node_id", node.node_id, status)

    with pytest.raises(DatabaseError, match="illegal node transition"):
        _set_status(database, "dag_nodes", "node_id", node.node_id, "RUNNING")


def test_every_illegal_pair_in_the_python_table_is_also_illegal_in_sql(
    database: Database, project
) -> None:
    """The Python table and the SQL table are the same rule, not two rules.

    Walked one pair at a time rather than in bulk, because the point is that
    *this specific move* is refused. A test that only checked the table's
    contents would pass even if no trigger were attached to it.
    """
    for source, targets in NODE_TRANSITIONS.items():
        illegal = [
            target for target in NodeStatus if target not in targets and target is not source
        ]
        if not illegal:
            continue
        # A fresh node per source status: a node already driven to one status
        # cannot then be driven to an unrelated one, because the transitions
        # that would get it there are exactly what this test is checking.
        with database.transaction() as session:
            node = _a_node(session, project.project_id)
        for step in _path_to(source):
            _set_status(database, "dag_nodes", "node_id", node.node_id, step.value)
        for target in illegal:
            with pytest.raises(DatabaseError, match="illegal node transition"):
                _set_status(database, "dag_nodes", "node_id", node.node_id, target.value)
        # A re-assertion is always permitted, so a retried activity is safe.
        _set_status(database, "dag_nodes", "node_id", node.node_id, source.value)


def _path_to(target: NodeStatus) -> list[NodeStatus]:
    """A legal route from PLANNED to a status, for driving a test node there."""
    routes: dict[NodeStatus, list[NodeStatus]] = {
        NodeStatus.PLANNED: [],
        NodeStatus.READY: [NodeStatus.READY],
        NodeStatus.BLOCKED: [NodeStatus.BLOCKED],
        NodeStatus.RUNNING: [NodeStatus.READY, NodeStatus.RUNNING],
        NodeStatus.WAITING_EXTERNAL: [
            NodeStatus.READY,
            NodeStatus.RUNNING,
            NodeStatus.WAITING_EXTERNAL,
        ],
        NodeStatus.WAITING_DECISION: [NodeStatus.READY, NodeStatus.WAITING_DECISION],
        NodeStatus.REVIEWING: [NodeStatus.READY, NodeStatus.RUNNING, NodeStatus.REVIEWING],
        NodeStatus.PASSED: [
            NodeStatus.READY,
            NodeStatus.RUNNING,
            NodeStatus.REVIEWING,
            NodeStatus.PASSED,
        ],
        NodeStatus.FAILED: [
            NodeStatus.READY,
            NodeStatus.RUNNING,
            NodeStatus.REVIEWING,
            NodeStatus.FAILED,
        ],
        NodeStatus.PARTIAL: [
            NodeStatus.READY,
            NodeStatus.RUNNING,
            NodeStatus.REVIEWING,
            NodeStatus.PARTIAL,
        ],
        NodeStatus.CANCELLED: [NodeStatus.CANCELLED],
    }
    return routes[target]


def test_a_nodes_identity_cannot_be_rewritten(database: Database, project) -> None:
    """Changing what a node is for means opening a new node.

    Results already produced belong to the old objective; editing it in place
    would silently re-attribute them.
    """
    with database.transaction() as session:
        node = _a_node(session, project.project_id)

    with pytest.raises(DatabaseError, match="immutable"), database.transaction() as session:
        session.execute(
            text("UPDATE dag_nodes SET objective = :o WHERE node_id = :n"),
            {"o": "Something easier to hit.", "n": node.node_id},
        )

    with pytest.raises(DatabaseError, match="immutable"), database.transaction() as session:
        session.execute(
            text("UPDATE dag_nodes SET node_type = 'DECISION' WHERE node_id = :n"),
            {"n": node.node_id},
        )


def test_a_nodes_lifecycle_columns_may_still_change(database: Database, project) -> None:
    """The guard must be narrow enough not to freeze the node entirely."""
    with database.transaction() as session:
        node = _a_node(session, project.project_id)

    with database.transaction() as session:
        session.execute(
            text(
                "UPDATE dag_nodes SET status = 'READY', artifact_refs = '[\"art-1\"]'::jsonb "
                "WHERE node_id = :n"
            ),
            {"n": node.node_id},
        )

    with database.read_only() as session:
        stored = DagRepository(session, project.project_id).node(node.node_id)
    assert stored.status is NodeStatus.READY
    assert stored.artifact_refs == ("art-1",)


# ── Projects ────────────────────────────────────────────────────────────────


def test_a_project_cannot_execute_without_a_contract(database: Database, project) -> None:
    with pytest.raises(DatabaseError, match="illegal project transition"):
        _set_status(database, "projects", "project_id", project.project_id, "EXECUTING")


def test_a_completed_project_is_final(database: Database, project) -> None:
    for status in ("CONTRACT_DEFINED", "EXECUTING", "COMPLETED"):
        _set_status(database, "projects", "project_id", project.project_id, status)

    with pytest.raises(DatabaseError, match="illegal project transition"):
        _set_status(database, "projects", "project_id", project.project_id, "EXECUTING")


def test_the_project_registry_follows_the_same_rule(database: Database, project) -> None:
    """The repository and the trigger agree; neither is the only guard."""
    with database.transaction() as session:
        registry = ProjectRegistry(session)
        with pytest.raises(Exception, match=r"may not become|illegal"):
            registry.transition(project.project_id, ProjectStatus.EXECUTING, actor_id="u1")


# ── Deletion ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "table",
    ["decision_records", "review_records", "evidence", "artifact_versions", "project_events"],
)
def test_an_append_only_table_refuses_deletion(database: Database, table: str) -> None:
    """Nothing in RAVEL removes state. Test teardown truncates; that is all.

    Asserted against an *empty* table on purpose. A row-level trigger would not
    fire here and this test would pass vacuously while the rule did nothing;
    the guard is statement-level so the refusal holds regardless of contents.
    """
    with pytest.raises(DatabaseError, match="append-only"), database.transaction() as session:
        session.execute(text(f"DELETE FROM {table}"))


@pytest.mark.parametrize("table", ["projects", "dag_nodes", "approval_requests"])
def test_a_life_cycle_table_refuses_deletion_too(database: Database, table: str) -> None:
    """A status may change; the row may not vanish."""
    with pytest.raises(DatabaseError, match="never deleted"), database.transaction() as session:
        session.execute(text(f"DELETE FROM {table}"))


def test_a_project_cannot_be_deleted_out_from_under_its_records(
    database: Database, project
) -> None:
    with pytest.raises(DatabaseError, match="never deleted"), database.transaction() as session:
        session.execute(
            text("DELETE FROM projects WHERE project_id = :p"),
            {"p": project.project_id},
        )


def test_an_event_cannot_be_rewritten(database: Database, project) -> None:
    """An event that could be edited would not be a record of anything."""
    with pytest.raises(DatabaseError, match="append-only"), database.transaction() as session:
        session.execute(text("UPDATE project_events SET actor_id = 'someone-else'"))


# ── Contract freeze ─────────────────────────────────────────────────────────


def _a_contract(session, project_id: str, node_id: str) -> str:
    """A stored, unfrozen acceptance contract. Returns its id."""
    contract = AcceptanceContract(
        project_id=project_id,
        node_id=node_id,
        criteria=(
            AcceptanceCriterion(
                statement="Conductivity rises by at least 15%.",
                provenance=CriterionProvenance.USER_REQUIREMENT,
            ),
        ),
    )
    AcceptanceContractRepository(session, project_id).add(contract)
    return contract.contract_id


def test_freezing_a_contract_is_the_one_write_it_permits(database: Database, project) -> None:
    """`freeze` is a legitimate UPDATE, so the guard must not blanket-refuse it."""
    with database.transaction() as session:
        node = _a_node(session, project.project_id)
        contract_id = _a_contract(session, project.project_id, node.node_id)

    with database.transaction() as session:
        AcceptanceContractRepository(session, project.project_id).freeze(contract_id)

    with database.read_only() as session:
        stored = AcceptanceContractRepository(session, project.project_id).get(
            contract_id=contract_id
        )
    assert stored.frozen_at is not None


def test_a_contracts_terms_cannot_be_edited_after_it_is_written(
    database: Database, project
) -> None:
    """Freezing criteria that could still be edited would freeze nothing."""
    with database.transaction() as session:
        node = _a_node(session, project.project_id)
        contract_id = _a_contract(session, project.project_id, node.node_id)

    for column, value in (
        ("criteria", "'[]'::jsonb"),
        ("node_id", "'somewhere-else'"),
        ("version", "7"),
    ):
        # `pytest.raises` outermost, so the transaction still sees the error and
        # rolls back. Reversed, `raises` would swallow the failure and the
        # transaction would commit instead.
        with pytest.raises(DatabaseError, match="immutable"), (
            database.transaction()
        ) as session:
            session.execute(
                text(f"UPDATE acceptance_contracts SET {column} = {value} "
                "WHERE contract_id = :c"),
                {"c": contract_id},
            )


def test_freezing_is_one_way(database: Database, project) -> None:
    """A contract that could be unfrozen was never binding."""
    with database.transaction() as session:
        node = _a_node(session, project.project_id)
        contract_id = _a_contract(session, project.project_id, node.node_id)
    with database.transaction() as session:
        AcceptanceContractRepository(session, project.project_id).freeze(contract_id)

    with pytest.raises(DatabaseError, match="one-way"), database.transaction() as session:
        session.execute(
            text("UPDATE acceptance_contracts SET frozen_at = NULL WHERE contract_id = :c"),
            {"c": contract_id},
        )

    with pytest.raises(DatabaseError, match="one-way"), database.transaction() as session:
        session.execute(
            text("UPDATE acceptance_contracts SET frozen_at = now() WHERE contract_id = :c"),
            {"c": contract_id},
        )


def test_refreezing_is_idempotent_rather_than_an_error(database: Database, project) -> None:
    """A retried activity must not fail on the second attempt."""
    with database.transaction() as session:
        node = _a_node(session, project.project_id)
        contract_id = _a_contract(session, project.project_id, node.node_id)

    with database.transaction() as session:
        AcceptanceContractRepository(session, project.project_id).freeze(contract_id)
    with database.transaction() as session:
        again = AcceptanceContractRepository(session, project.project_id).freeze(contract_id)

    with database.read_only() as session:
        stored = AcceptanceContractRepository(session, project.project_id).get(
            contract_id=contract_id
        )
    assert again.frozen_at == stored.frozen_at


def test_an_execution_contracts_terms_are_fixed_too(database: Database, project) -> None:
    """Workers act under terms that could not have changed under them."""
    with database.transaction() as session:
        node = _a_node(session, project.project_id)
        contract = ExecutionContract(
            project_id=project.project_id,
            node_id=node.node_id,
            objective="Run the conductivity sweep.",
            allowed_actions=("measure",),
        )
        ExecutionContractRepository(session, project.project_id).add(contract)
        ExecutionContractRepository(session, project.project_id).freeze(contract.contract_id)

    with pytest.raises(DatabaseError, match="immutable"), database.transaction() as session:
        session.execute(
            text("UPDATE execution_contracts SET objective = 'Something easier' "
            "WHERE contract_id = :c"),
            {"c": contract.contract_id},
        )


# ── Drift ───────────────────────────────────────────────────────────────────


def test_the_sql_transition_tables_match_the_python_state_machines(
    database: Database,
) -> None:
    """A change to the state machine that was never migrated fails here.

    Without this, editing `NODE_TRANSITIONS` in Python would leave PostgreSQL
    enforcing the previous rule, and the unit suite would still pass.
    """
    from ravel.state import guards

    with database.read_only() as session:
        for table, expected in guards.expected_transition_rows().items():
            actual = {
                (row[0], row[1])
                for row in session.execute(text(f"SELECT from_status, to_status FROM {table}"))
            }
            assert actual == expected, f"{table} has drifted from the Python table"
