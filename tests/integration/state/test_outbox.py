"""The transactional outbox.

The gate item is "a rolled-back transaction emits no event", and it is asserted
the strong way: not merely that the stream is empty after a rollback, but that
the *sequence* did not advance. A counter that burned a number on a rolled-back
write would leave a permanent hole, and a hole is exactly the signal a consumer
uses to decide it missed an event — so a gap that means nothing would make the
signal meaningless.
"""

from __future__ import annotations

import pytest

from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, NodeType
from ravel.domain.events import ActorType, ProjectEventType
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.outbox import emit, events_since, last_event_seq
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry

pytestmark = pytest.mark.integration


class _Deliberate(RuntimeError):
    """A failure raised inside a transaction to force a rollback."""


def test_a_project_creation_emits_exactly_one_event(database: Database, project) -> None:
    with database.read_only() as session:
        events = events_since(session, project.project_id)
    assert [event.event_type for event in events] == [ProjectEventType.PROJECT_CREATED]
    assert events[0].seq == 1


def test_a_rolled_back_write_emits_no_event(database: Database, project) -> None:
    """The gate: state and its event are one atomic fact."""
    with pytest.raises(_Deliberate), database.transaction() as session:
        node = DagNode.create(
            project_id=project.project_id,
            node_type=NodeType.RESEARCH,
            objective="This node never existed.",
            created_by="master",
        )
        DagRepository(session, project.project_id).add_node(
            node, role=AgentRole.MASTER, decision_ref="dec-1"
        )
        raise _Deliberate("the transaction fails after the write was staged")

    with database.read_only() as session:
        assert DagRepository(session, project.project_id).nodes() == []
        assert events_since(session, project.project_id, after_seq=1) == []
        assert last_event_seq(session, project.project_id) == 1


def test_a_rolled_back_transaction_does_not_burn_a_sequence_number(
    database: Database, project
) -> None:
    """A gap in the stream would be indistinguishable from a lost event."""
    for _ in range(3):
        with pytest.raises(_Deliberate), database.transaction() as session:
            emit(
                session,
                project_id=project.project_id,
                event_type=ProjectEventType.DAG_MUTATED,
                actor_type=ActorType.AGENT,
                actor_id="master",
            )
            raise _Deliberate("rolled back")

    with database.transaction() as session:
        committed = emit(
            session,
            project_id=project.project_id,
            event_type=ProjectEventType.DAG_MUTATED,
            actor_type=ActorType.AGENT,
            actor_id="master",
        )

    assert committed.seq == 2, "the three rolled-back writes must not have consumed numbers"
    with database.read_only() as session:
        assert [event.seq for event in events_since(session, project.project_id)] == [1, 2]


def test_sequences_are_contiguous_within_a_project(database: Database, project) -> None:
    """Stated as contiguity rather than as literal numbers, because the point is
    that no number is skipped — not that a particular one was used."""
    with database.read_only() as session:
        baseline = last_event_seq(session, project.project_id)

    with database.transaction() as session:
        for index in range(5):
            emit(
                session,
                project_id=project.project_id,
                event_type=ProjectEventType.DAG_MUTATED,
                actor_type=ActorType.AGENT,
                actor_id="master",
                payload={"index": index},
            )

    with database.read_only() as session:
        seqs = [
            event.seq
            for event in events_since(session, project.project_id, after_seq=baseline)
        ]
    assert seqs == list(range(baseline + 1, baseline + 6))


def test_sequences_are_independent_per_project(database: Database, other_project) -> None:
    """A consumer resumes from a sequence number within one project's stream."""
    with database.transaction() as session:
        registry = ProjectRegistry(session)
        third = registry.create(title="Third", objective="Also unrelated.", created_by="u")

    with database.read_only() as session:
        assert events_since(session, third.project_id)[0].seq == 1
        assert events_since(session, other_project.project_id)[0].seq == 1


def test_resuming_from_a_sequence_returns_only_what_follows(
    database: Database, project
) -> None:
    """The TUI reconnects with the last sequence it rendered."""
    with database.transaction() as session:
        for _ in range(4):
            emit(
                session,
                project_id=project.project_id,
                event_type=ProjectEventType.DAG_MUTATED,
                actor_type=ActorType.AGENT,
                actor_id="master",
            )

    with database.read_only() as session:
        resumed = events_since(session, project.project_id, after_seq=3)
    assert [event.seq for event in resumed] == [4, 5]


def test_a_node_transition_writes_its_event_in_the_same_transaction(
    database: Database, project
) -> None:
    """The event cannot exist without the change, or the change without it."""
    with database.transaction() as session:
        dag = DagRepository(session, project.project_id)
        node = dag.add_node(
            DagNode.create(
                project_id=project.project_id,
                node_type=NodeType.RESEARCH,
                objective="Survey the literature.",
                created_by="master",
            ),
            role=AgentRole.MASTER,
            decision_ref="dec-1",
        )

    with database.transaction() as session:
        DagRepository(session, project.project_id).transition_node(
            node.node_id, NodeStatus.READY, actor_id="scheduler"
        )

    with database.read_only() as session:
        types = [event.event_type for event in events_since(session, project.project_id)]
        stored = DagRepository(session, project.project_id).node(node.node_id)

    assert stored.status is NodeStatus.READY
    assert types == [
        ProjectEventType.PROJECT_CREATED,
        ProjectEventType.DAG_MUTATED,
        ProjectEventType.NODE_CREATED,
        ProjectEventType.NODE_READY,
    ]


def test_an_event_carries_the_actor_that_caused_it(database: Database, project) -> None:
    """Attribution comes from the role the caller proved, not from a claim."""
    with database.transaction() as session:
        DagRepository(session, project.project_id).add_node(
            DagNode.create(
                project_id=project.project_id,
                node_type=NodeType.HYPOTHESIS,
                objective="The dopant raises conductivity.",
                created_by="master",
            ),
            role=AgentRole.MASTER,
            decision_ref="dec-1",
        )

    with database.read_only() as session:
        written = events_since(session, project.project_id)[1:]

    assert [event.event_type for event in written] == [
        ProjectEventType.DAG_MUTATED,
        ProjectEventType.NODE_CREATED,
    ]
    assert {event.actor_id for event in written} == {"master"}
    assert {event.actor_type.value for event in written} == {"AGENT"}
    assert written[0].payload["change"] == "ADD_NODE"
    assert written[0].payload["node_type"] == "HYPOTHESIS"
    assert written[0].payload["decision_ref"] == "dec-1"


def test_a_transaction_that_commits_keeps_everything_it_wrote(
    database: Database, project
) -> None:
    """The complement of the rollback test: nothing is lost on the happy path."""
    with database.transaction() as session:
        emit(
            session,
            project_id=project.project_id,
            event_type=ProjectEventType.MASTER_STARTED,
            actor_type=ActorType.AGENT,
            actor_id="master",
        )

    with database.read_only() as session:
        assert last_event_seq(session, project.project_id) == 2
        assert events_since(session, project.project_id)[-1].event_type is (
            ProjectEventType.MASTER_STARTED
        )
