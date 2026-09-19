"""Only Master changes the plan, and every change carries a Decision Record.

The gate for this phase, asserted three ways because there are three ways to
get it wrong:

- a non-Master caller is refused, for every mutating operation;
- the refusal leaves nothing behind — no node, no decision, no event, because a
  change that was refused but recorded would be worse than one that succeeded;
- a change that *is* accepted records the decision that authorized it, and the
  record names the nodes the change actually touched.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from tests.integration.dag.conftest import STAGES, register_roadmap

from ravel.domain.dag import DagNode
from ravel.domain.decisions import AffectedNodes, AuthorityCheck, DecisionRecord
from ravel.domain.enums import Confidence, DecisionType, JoinPolicy, NodeStatus, NodeType
from ravel.domain.planning import HorizonError
from ravel.domain.project import Project, RoadmapPhase
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import TransitionError
from ravel.state.database import Database
from ravel.state.outbox import events_since, last_event_seq
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import RoadmapRepository
from ravel.state.services.dag import DagMutationService, DecisionDraft

pytestmark = pytest.mark.integration

#: Every role that is not Master. A refusal that held for four of these and not
#: the fifth would be a refusal that does not mean anything.
NON_MASTER = [
    AgentRole.RESEARCH,
    AgentRole.REVIEW,
    AgentRole.COMPUTE_WORKER,
    AgentRole.EXPERIMENTAL_WORKER,
]

NodeFactory = Callable[..., DagNode]


def _draft(decision_type: DecisionType = DecisionType.CREATE_NODE) -> DecisionDraft:
    return DecisionDraft(
        decision_type=decision_type,
        rationale="The literature review left the mechanism open.",
        confidence=Confidence.MEDIUM,
    )


def _counts(service: DagMutationService, project: Project) -> tuple[int, int, int]:
    """`(nodes, decisions, last event seq)` — what a trace would consist of."""
    return (
        len(service.dag.nodes()),
        len(service.decisions.all()),
        last_event_seq(service.session, project.project_id),
    )


# ── The gate: a non-Master caller cannot mutate ─────────────────────────────


@pytest.mark.parametrize("role", NON_MASTER, ids=lambda role: role.value)
def test_a_non_master_caller_cannot_add_a_node(
    service: DagMutationService, a_node: NodeFactory, role: AgentRole
) -> None:
    with pytest.raises(PermissionError, match="may not mutate the DAG"):
        service.add_node(a_node(), role=role, decision=_draft())


@pytest.mark.parametrize("role", NON_MASTER, ids=lambda role: role.value)
def test_a_non_master_caller_cannot_expand_a_stage(
    service: DagMutationService, a_node: NodeFactory, role: AgentRole
) -> None:
    with pytest.raises(PermissionError, match="may not mutate the DAG"):
        service.expand_phase(STAGES[0], [a_node()], role=role, decision=_draft())


@pytest.mark.parametrize("role", NON_MASTER, ids=lambda role: role.value)
def test_a_non_master_caller_cannot_cancel_a_node(
    service: DagMutationService, a_node: NodeFactory, role: AgentRole
) -> None:
    node = a_node()
    service.expand_phase(STAGES[0], [node], role=AgentRole.MASTER, decision=_draft())

    with pytest.raises(PermissionError, match="may not mutate the DAG"):
        service.cancel_node(node.node_id, role=role, decision=_draft(DecisionType.CANCEL_NODE))


@pytest.mark.parametrize("role", NON_MASTER, ids=lambda role: role.value)
def test_a_non_master_caller_cannot_register_a_stage(
    database: Database, project: Project, role: AgentRole
) -> None:
    with (
        database.transaction() as session,
        pytest.raises(PermissionError, match="may not mutate the DAG"),
    ):
        RoadmapRepository(session, project.project_id).register(
            RoadmapPhase(project_id=project.project_id, name="Smuggled", order=9),
            role=role,
        )


def test_a_refused_mutation_leaves_no_trace(
    service: DagMutationService, project: Project, a_node: NodeFactory
) -> None:
    """The strong form: not "it raised", but "nothing was written anyway".

    The exception is caught *inside* the transaction on purpose, so whatever the
    refused call staged would be committed and counted. A service that wrote the
    decision before checking authority would fail here and nowhere else.
    """
    before = _counts(service, project)

    with pytest.raises(PermissionError):
        service.add_node(a_node(), role=AgentRole.RESEARCH, decision=_draft())

    assert _counts(service, project) == before


def test_a_refused_expansion_leaves_no_trace(
    service: DagMutationService, project: Project, a_node: NodeFactory
) -> None:
    before = _counts(service, project)

    with pytest.raises(HorizonError):
        service.expand_phase(
            STAGES[3], [a_node()], role=AgentRole.MASTER, decision=_draft()
        )

    assert _counts(service, project) == before


def test_a_draft_with_no_rationale_is_refused_before_anything_is_staged(
    service: DagMutationService,
) -> None:
    with pytest.raises(ValueError, match="needs a rationale"):
        DecisionDraft(decision_type=DecisionType.CREATE_NODE, rationale="   ")

    assert service.dag.nodes() == []


# ── The gate: a material change carries its decision ────────────────────────


def test_adding_a_node_records_the_decision_that_authorized_it(
    service: DagMutationService, project: Project, a_node: NodeFactory
) -> None:
    node = service.add_node(a_node(), role=AgentRole.MASTER, decision=_draft())

    decisions = service.decisions.all()
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.project_id == project.project_id
    assert decision.decision_type is DecisionType.CREATE_NODE
    assert decision.affected_nodes.created == (node.node_id,)
    assert decision.authority_check.actor_role == AgentRole.MASTER.value
    assert decision.authority_check.permitted
    # The node carries its own provenance, not only the event stream.
    assert node.decision_ref == decision.decision_id


def test_expanding_a_stage_writes_one_decision_naming_every_node(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    nodes = [a_node() for _ in range(3)]
    expansion = service.expand_phase(STAGES[0], nodes, role=AgentRole.MASTER, decision=_draft())

    assert len(service.decisions.all()) == 1
    assert set(expansion.decision.affected_nodes.created) == {
        node.node_id for node in expansion.nodes
    }
    assert all(node.roadmap_phase == STAGES[0] for node in expansion.nodes)
    assert all(node.decision_ref == expansion.decision.decision_id for node in expansion.nodes)


def test_cancelling_a_node_records_what_it_cancelled(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    node = a_node()
    service.expand_phase(STAGES[0], [node], role=AgentRole.MASTER, decision=_draft())

    cancelled = service.cancel_node(
        node.node_id, role=AgentRole.MASTER, decision=_draft(DecisionType.CANCEL_NODE)
    )

    assert cancelled.status is NodeStatus.CANCELLED
    decision = service.decisions.all()[-1]
    assert decision.decision_type is DecisionType.CANCEL_NODE
    assert decision.affected_nodes.cancelled == (node.node_id,)


def test_a_declared_dependency_is_written_as_an_edge(
    service: DagMutationService, a_node: NodeFactory, a_join_node: Callable[..., DagNode]
) -> None:
    """The edge is the declaration, materialised in the same transaction.

    The two are written together because they are one fact, and a graph that
    could disagree with the nodes it was built from would be worse than no
    graph at all — every later reading of "what depends on this" would need to
    ask which of the two views was current.
    """
    measurement = a_node(NodeType.COMPUTATION)
    service.expand_phase(STAGES[0], [measurement], role=AgentRole.MASTER, decision=_draft())
    analysis = a_join_node(dependencies=(measurement.node_id,))

    service.expand_phase(STAGES[0], [analysis], role=AgentRole.MASTER, decision=_draft())

    edges = service.dag.edges()
    assert [(edge.from_node, edge.to_node) for edge in edges] == [
        (measurement.node_id, analysis.node_id)
    ]
    assert service.dag.node(analysis.node_id).dependencies == (measurement.node_id,)


def test_a_node_may_depend_on_another_node_added_beside_it(
    service: DagMutationService, a_join_node: Callable[..., DagNode], a_node: NodeFactory
) -> None:
    """A stage's analysis reads that stage's measurement, so they arrive together.

    The analysis is listed *first* on purpose: the batch is written as a batch,
    and a plan whose meaning depended on the order the caller happened to list
    its nodes in would be a plan held together by luck.
    """
    measurement = a_node(NodeType.COMPUTATION)
    analysis = a_join_node(dependencies=(measurement.node_id,))

    service.expand_phase(
        STAGES[0], [analysis, measurement], role=AgentRole.MASTER, decision=_draft()
    )

    assert service.dag.nodes_in_status(NodeStatus.PLANNED)
    assert service.dag.join_state(service.dag.node(analysis.node_id)) == "waiting"
    assert [(edge.from_node, edge.to_node) for edge in service.dag.edges()] == [
        (measurement.node_id, analysis.node_id)
    ]


def test_a_dependency_on_a_node_that_does_not_exist_is_refused(
    service: DagMutationService, a_join_node: Callable[..., DagNode]
) -> None:
    """`dependencies` has no foreign key behind it, so the check is here.

    Left to the database, the first thing to notice would be the scheduler
    following the identifier and raising NotFound out of a readiness pass —
    which reports a broken plan as a broken scheduler.
    """
    orphan = a_join_node(dependencies=("node-that-was-never-created",))

    with pytest.raises(NotFound, match="does not have"):
        service.expand_phase(STAGES[0], [orphan], role=AgentRole.MASTER, decision=_draft())


def test_nodes_that_would_wait_on_each_other_are_refused(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    """A cycle does not crash; it stalls, which is harder to notice.

    Two nodes waiting on each other stay PLANNED forever and nothing in the
    stream says why. Neither node can be refused on its own — each is a valid
    node — so this is the one dependency fault that only the graph can see, and
    it is refused at the moment it is written.
    """
    first, second = a_node(NodeType.COMPUTATION), a_node(NodeType.EXPERIMENT)
    depending_on_each_other = [
        first.model_copy(
            update={"dependencies": (second.node_id,), "join_policy": JoinPolicy.ALL}
        ),
        second.model_copy(
            update={"dependencies": (first.node_id,), "join_policy": JoinPolicy.ALL}
        ),
    ]

    with pytest.raises(ValueError, match="wait on each other forever"):
        service.expand_phase(
            STAGES[0],
            depending_on_each_other,
            role=AgentRole.MASTER,
            decision=_draft(),
        )

    assert service.dag.nodes() == [], "the refused plan left nothing behind"
    assert service.dag.edges() == []


def test_the_edge_rows_and_the_declarations_agree(
    service: DagMutationService, a_join_node: Callable[..., DagNode], a_node: NodeFactory
) -> None:
    """Both directions: every edge is declared, and every declaration is an edge.

    One direction alone would pass if edges were simply never written, which is
    exactly the state this project was in before the edge was understood as a
    materialisation of the declaration rather than a mutation of its own.
    """
    first, second = a_node(NodeType.COMPUTATION), a_node(NodeType.EXPERIMENT)
    joined = a_join_node(dependencies=(first.node_id, second.node_id))
    service.expand_phase(
        STAGES[0], [first, second, joined], role=AgentRole.MASTER, decision=_draft()
    )

    declared = {
        (dependency, node.node_id)
        for node in service.dag.nodes()
        for dependency in node.dependencies
    }
    written = {(edge.from_node, edge.to_node) for edge in service.dag.edges()}
    assert written == declared
    assert len(written) == 2


def test_the_event_that_announces_a_node_carries_its_fan_in(
    service: DagMutationService,
    project: Project,
    a_join_node: Callable[..., DagNode],
    a_node: NodeFactory,
) -> None:
    """The stream is what a reader watches to know how the plan took shape.

    A node's dependencies were decided when it was created, not amended later,
    so the event that announces it is the only place they can appear — and a
    reader that had to go back to the table for every fan-in would be reading
    current state and calling it history.
    """
    measurement = a_node(NodeType.COMPUTATION)
    service.expand_phase(STAGES[0], [measurement], role=AgentRole.MASTER, decision=_draft())
    mark = last_event_seq(service.session, project.project_id)
    analysis = a_join_node(dependencies=(measurement.node_id,))

    service.expand_phase(STAGES[0], [analysis], role=AgentRole.MASTER, decision=_draft())

    mutation = next(
        event
        for event in events_since(service.session, project.project_id, after_seq=mark)
        if event.payload.get("change") == "ADD_NODE"
    )
    assert mutation.payload["node_id"] == analysis.node_id
    assert mutation.payload["dependencies"] == [measurement.node_id]
    assert mutation.payload["join_policy"] == JoinPolicy.ALL.value


def test_a_structural_decision_that_names_no_node_is_refused() -> None:
    """The record's own validator, not the service's, is the last word."""
    with pytest.raises(ValueError, match="names no affected nodes"):
        DecisionRecord(
            project_id="proj-a",
            decision_type=DecisionType.CREATE_NODE,
            rationale="Create something, name nothing.",
            affected_nodes=AffectedNodes(),
            confidence=Confidence.MEDIUM,
            authority_check=AuthorityCheck(actor_role="MASTER", permitted=True),
        )


# ── The horizon, through the service ────────────────────────────────────────


def test_a_stage_beyond_the_horizon_cannot_be_expanded(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    service.expand_phase(STAGES[0], [a_node()], role=AgentRole.MASTER, decision=_draft())

    with pytest.raises(HorizonError, match="beyond the planning horizon"):
        service.expand_phase(STAGES[3], [a_node()], role=AgentRole.MASTER, decision=_draft())


def test_a_single_node_aimed_past_the_horizon_is_refused(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    with pytest.raises(HorizonError, match="beyond the planning horizon"):
        service.add_node(
            a_node(roadmap_phase=STAGES[3]), role=AgentRole.MASTER, decision=_draft()
        )


def test_a_node_may_claim_no_stage_at_all(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    """A decision node is not work for a stage, and nothing pretends it is."""
    node = service.add_node(
        a_node(NodeType.DECISION, objective="Record the route change."),
        role=AgentRole.MASTER,
        decision=_draft(),
    )
    assert node.roadmap_phase is None


def test_a_node_claimed_for_a_different_stage_is_refused(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    """Two answers to "which stage is this for" is one answer too many."""
    node = a_node(roadmap_phase=STAGES[1])

    with pytest.raises(ValueError, match="belongs to one stage"):
        service.expand_phase(STAGES[0], [node], role=AgentRole.MASTER, decision=_draft())


def test_expanding_a_stage_that_is_not_on_the_roadmap_is_refused(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    with pytest.raises(HorizonError, match="no roadmap phase named"):
        service.expand_phase(
            "Stage 99", [a_node()], role=AgentRole.MASTER, decision=_draft()
        )


def test_expanding_a_stage_before_the_roadmap_exists_is_refused(
    database: Database, project: Project, a_node: NodeFactory
) -> None:
    """A project with no roadmap has no stage to expand, and says so."""
    with database.transaction() as session:
        service = DagMutationService(session, project.project_id)
        assert service.horizon().current is None
        with pytest.raises(HorizonError, match="no roadmap phase"):
            service.expand_phase(
                STAGES[0], [a_node()], role=AgentRole.MASTER, decision=_draft()
            )


def test_an_empty_expansion_is_refused(service: DagMutationService) -> None:
    with pytest.raises(ValueError, match="commits no nodes"):
        service.expand_phase(STAGES[0], [], role=AgentRole.MASTER, decision=_draft())


def test_expansion_is_atomic_across_the_nodes_it_commits(
    service: DagMutationService, project: Project, a_node: NodeFactory
) -> None:
    """One node in the batch is invalid, so none of the batch is written."""
    before = _counts(service, project)

    with pytest.raises(ValueError, match="belongs to one stage"):
        service.expand_phase(
            STAGES[0],
            [a_node(), a_node(roadmap_phase=STAGES[2])],
            role=AgentRole.MASTER,
            decision=_draft(),
        )

    assert _counts(service, project) == before


def test_the_horizon_advances_when_a_stage_finishes(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    """Nothing advances it: the last node of the stage reaching an end does."""
    node = a_node()
    service.expand_phase(STAGES[0], [node], role=AgentRole.MASTER, decision=_draft())
    current = service.horizon().current
    assert current is not None and current.name == STAGES[0]

    service.cancel_node(
        node.node_id, role=AgentRole.MASTER, decision=_draft(DecisionType.CANCEL_NODE)
    )

    moved = service.horizon().current
    assert moved is not None and moved.name == STAGES[1]
    assert service.phase_work()[STAGES[0]].is_settled


def test_work_counts_only_the_nodes_a_stage_actually_has(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    service.expand_phase(
        STAGES[0], [a_node(), a_node()], role=AgentRole.MASTER, decision=_draft()
    )
    service.add_node(a_node(NodeType.DECISION, objective="Record."), role=AgentRole.MASTER,
                     decision=_draft())

    work = service.phase_work()
    assert work[STAGES[0]].nodes == 2
    assert work[STAGES[0]].unfinished == 2
    assert STAGES[1] not in work


# ── Doors that were open beside the ones that were locked ───────────────────


def test_cancelling_through_a_status_report_is_refused(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    """`transition_node` is a status report, and cancellation is not one.

    It takes no role and no decision, because a worker reporting RUNNING has
    neither. Cancelling through it would reach the same CANCELLED status as
    `cancel_node` while skipping both of that method's requirements.
    """
    node = a_node()
    service.expand_phase(STAGES[0], [node], role=AgentRole.MASTER, decision=_draft())

    with pytest.raises(PermissionError, match="cancel_node"):
        service.dag.transition_node(node.node_id, NodeStatus.CANCELLED, actor_id="research")

    assert service.dag.node(node.node_id).status is NodeStatus.PLANNED


def test_a_node_cannot_run_without_frozen_criteria(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    node = a_node(NodeType.COMPUTATION, objective="Measure conductivity.")
    service.expand_phase(STAGES[0], [node], role=AgentRole.MASTER, decision=_draft())
    service.dag.transition_node(node.node_id, NodeStatus.READY, actor_id="scheduler")

    with pytest.raises(TransitionError, match="acceptance criteria are frozen"):
        service.dag.transition_node(node.node_id, NodeStatus.RUNNING, actor_id="compute-worker")


def test_a_node_cannot_run_without_an_execution_contract(
    service: DagMutationService,
    a_node: NodeFactory,
    freeze_criteria: Callable[[str], str],
) -> None:
    node = a_node(NodeType.COMPUTATION, objective="Measure conductivity.")
    service.expand_phase(STAGES[0], [node], role=AgentRole.MASTER, decision=_draft())
    freeze_criteria(node.node_id)
    service.dag.transition_node(node.node_id, NodeStatus.READY, actor_id="scheduler")

    with pytest.raises(TransitionError, match="no Execution Contract"):
        service.dag.transition_node(node.node_id, NodeStatus.RUNNING, actor_id="compute-worker")


def test_a_prepared_and_cleared_node_runs(
    service: DagMutationService,
    a_node: NodeFactory,
    freeze_criteria: Callable[[str], str],
    execution_contract: Callable[[str], str],
    clear_to_run: Callable[[str], None],
) -> None:
    """Everything a COMPUTATION node owes before it may be handed to a Worker.

    Three things, and the third is the one this file is not the right place to
    argue for: the pre-flight review is asserted in
    `tests/integration/review/test_pre_run_gate.py`, and what is checked here is
    only that the three together are *sufficient* — the refusals above each stop
    the node, and this shows the node runs once none of them applies.
    """
    node = a_node(NodeType.COMPUTATION, objective="Measure conductivity.")
    service.expand_phase(STAGES[0], [node], role=AgentRole.MASTER, decision=_draft())
    service.dag.bind_acceptance_contract(node.node_id, freeze_criteria(node.node_id))
    service.dag.bind_execution_contract(node.node_id, execution_contract(node.node_id))
    service.dag.transition_node(node.node_id, NodeStatus.READY, actor_id="scheduler")
    clear_to_run(node.node_id)

    running = service.dag.transition_node(
        node.node_id, NodeStatus.RUNNING, actor_id="compute-worker"
    )

    assert running.status is NodeStatus.RUNNING
    assert running.started_at is not None


def test_a_nodes_criteria_cannot_be_rebound_after_they_are_bound(
    service: DagMutationService,
    a_node: NodeFactory,
    freeze_criteria: Callable[[str], str],
) -> None:
    """Rebinding would measure a result against criteria chosen after seeing it."""
    node = a_node(NodeType.COMPUTATION, objective="Measure conductivity.")
    other = a_node(NodeType.COMPUTATION, objective="Measure something else.")
    service.expand_phase(STAGES[0], [node, other], role=AgentRole.MASTER, decision=_draft())
    service.dag.bind_acceptance_contract(node.node_id, freeze_criteria(node.node_id))

    with pytest.raises(ValueError, match="already references"):
        service.dag.bind_acceptance_contract(node.node_id, freeze_criteria(other.node_id))


def test_a_node_cannot_be_bound_to_another_projects_contract(
    service: DagMutationService,
    a_node: NodeFactory,
    other_project: Project,
    database: Database,
    acceptance_contract: Callable[[object, str, str], str],
) -> None:
    """A reference its own project cannot resolve is a reference nobody can check."""
    node = a_node(NodeType.COMPUTATION, objective="Measure conductivity.")
    service.expand_phase(STAGES[0], [node], role=AgentRole.MASTER, decision=_draft())

    with database.transaction() as session:
        elsewhere = DagRepository(session, other_project.project_id).add_node(
            DagNode.create(
                project_id=other_project.project_id,
                node_type=NodeType.COMPUTATION,
                objective="Someone else's measurement.",
                created_by="master",
            ),
            role=AgentRole.MASTER,
            decision_ref="a-decision-in-another-project",
        )
        foreign_id = acceptance_contract(session, other_project.project_id, elsewhere.node_id)

    with pytest.raises(NotFound, match="no AcceptanceContractRow"):
        service.dag.bind_acceptance_contract(node.node_id, foreign_id)


def test_a_node_cannot_reference_another_projects_artifact(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    node = a_node()
    service.expand_phase(STAGES[0], [node], role=AgentRole.MASTER, decision=_draft())

    with pytest.raises(NotFound, match="no artifact"):
        service.dag.record_artifact(node.node_id, "artifact-from-elsewhere")


# ── The same rules, reached by the other door ───────────────────────────────


def test_the_repository_refuses_what_the_service_refuses(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    """Authority is at the repository, so a caller skipping the service hits it."""
    repository = DagRepository(service.session, service.project_id)

    with pytest.raises(PermissionError, match="may not mutate the DAG"):
        repository.add_node(a_node(), role=AgentRole.RESEARCH, decision_ref="any")


def test_a_mutation_without_a_decision_is_refused_at_the_repository(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    with pytest.raises(ValueError, match="requires a Decision Record reference"):
        service.dag.add_node(a_node(), role=AgentRole.MASTER, decision_ref="")


def test_registering_a_stage_after_the_horizon_moved_is_allowed(
    database: Database, project: Project
) -> None:
    """The roadmap is coarse and may run ahead; only the DAG may not.

    A stage registered beyond the horizon is not expanded, which is the whole
    distinction: the roadmap says where the project is going, and registering
    the destination is not the same as planning the route there.
    """
    phases = register_roadmap(database, project, *STAGES, "Stage 5")
    assert [phase.name for phase in phases][-1] == "Stage 5"
    with database.transaction() as session:
        horizon = DagMutationService(session, project.project_id).horizon()
    assert "Stage 5" in horizon.names
    assert "Stage 5" not in horizon.reachable_names
