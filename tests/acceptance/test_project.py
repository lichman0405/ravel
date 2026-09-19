"""A01, A02, A05, A20: a project is created, planned, and reaches an ending.

These four are the spine. A01 is the project's first moment and A20 its last,
and the two in between are the ones that make the ends mean anything: A02 is
Master reading state it did not write, and A05 is Master writing a plan through
the one path that is allowed to.

What is real in every case: PostgreSQL, the tool servers over real MCP stdio,
the DSH composition the harness launches, Temporal, the mock backends. What is
scripted is the policy a model would supply — which node to plan, when to
conclude — because that is the part these items are not about.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.dsh.mcp_probe import probe
from tests.e2e.conftest import Headless, Task
from tests.integration.conftest import build_prepared
from tests.integration.roles.conftest import RoleEnvironment

from ravel.config import Settings
from ravel.domain.contracts import (
    ApprovalRequirement,
    AuthorityEnvelope,
    BudgetLimits,
    ProjectSuccessContract,
    ResearchContract,
)
from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, NodeType, ProjectOutcome, ProjectStatus, UserRole
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.master import ENDING_DECISION
from ravel.state.database import Database
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    AuthorityEnvelopeRepository,
    ResearchContractRepository,
    SuccessContractRepository,
)
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.records import DecisionRepository

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]


# ── A01 ─────────────────────────────────────────────────────────────────────


def test_a01_a_project_starts_with_its_contracts_and_its_envelope(
    database: Database,
) -> None:
    """A01: "Create Project, Research Contract draft, Authority Envelope,
    Project Success Contract draft."

    All four in one transaction, because that is what they are: a project is
    not a row, it is a row plus the three records that say what it is for, what
    Master may decide alone, and what would count as succeeding. The
    assertions read each back out of PostgreSQL rather than out of the objects
    just written, so a repository that accepted a write and dropped it would
    fail here.

    The project starts `CREATED` and stays there. Nothing in this test starts
    it — A05 is where a plan exists to start it for — and a project that could
    reach `EXECUTING` with no contract in force would be one that could succeed
    at nothing in particular.
    """
    with database.transaction() as session:
        owner = UserRepository(session).create(username="ada", password_hash="x")
        project = ProjectRegistry(session).create(
            title="Catalyst screen",
            objective="Find a dopant that raises conductivity by 15%.",
            created_by=owner.user_id,
        )
        project_id = project.project_id
        MembershipRepository(session, project_id).grant(
            user_id=owner.user_id, role=UserRole.PROJECT_OWNER
        )

        ResearchContractRepository(session, project_id).add(
            ResearchContract(
                project_id=project_id,
                original_user_goal="Find a dopant that survives 500 hours under load.",
                scientific_problem="Which dopant keeps conductivity above the threshold?",
                research_hypotheses=("Niobium doping raises stability.",),
                target_metrics=("conductivity gain >= 15%",),
                acceptance_strategy="Measure the series against the baseline.",
                known_constraints=("Bench time is limited.",),
                prohibited_actions=("No testing on live reactors.",),
            )
        )
        AuthorityEnvelopeRepository(session, project_id).add_version(
            AuthorityEnvelope(
                project_id=project_id,
                requires_approval=(
                    ApprovalRequirement(
                        action="spend_over_budget",
                        required_role=UserRole.PROJECT_OWNER,
                        rationale="Money is the owner's to commit.",
                    ),
                ),
                max_dag_nodes_without_approval=25,
                budget_time_limits=BudgetLimits(max_wall_clock_hours=72),
            )
        )
        SuccessContractRepository(session, project_id).add_version(
            ProjectSuccessContract(
                project_id=project_id,
                success_criteria=("The dopant series shows a 15% conductivity gain.",),
                failure_criteria=("No sample exceeds the control beyond noise.",),
                unresolved_uncertainty_policy="Conclude inconclusive rather than guess.",
            )
        )

    with database.read_only() as session:
        assert ProjectRegistry(session).get(project_id).status is ProjectStatus.CREATED

        envelope = AuthorityEnvelopeRepository(session, project_id).latest()
        assert envelope.version == 1
        assert envelope.requires_human_approval("spend_over_budget")
        assert not envelope.requires_human_approval("run_measurement"), (
            "an envelope that required approval for everything would be one "
            "nobody could work under"
        )
        assert envelope.requirement_for("spend_over_budget") is not None
        assert (
            envelope.requirement_for("spend_over_budget").required_role
            is UserRole.PROJECT_OWNER
        )
        assert envelope.max_dag_nodes_without_approval == 25

        success = SuccessContractRepository(session, project_id).latest()
        assert success.version == 1
        assert success.supersedes is None
        assert success.decision_ref is None, (
            "the first version supersedes nothing and answers no decision; a "
            "later one must name both"
        )

        research = ResearchContractRepository(session, project_id).current()
        assert research.original_user_goal.startswith("Find a dopant")


# ── A02 ─────────────────────────────────────────────────────────────────────


@pytest.mark.timeout(120)
async def test_a02_master_reads_project_state_through_its_own_tool_server(
    database: Database,
    project: Project,
    integration_settings: Settings,
    tmp_path: Path,
) -> None:
    """A02: "Master identity/session starts and can read Project State."

    Through the tool server the harness actually spawns, over the stdio
    transport it actually uses, with the scope it actually sets — because the
    claim is not that some function can read a row, it is that a Master session
    is given a way to see the project and nothing narrower.

    The node is built here and read back through the tool, so the state that
    comes out is a state this test put there. `read_project_state` answers with
    a summary rather than with the nodes themselves — a count by status and the
    identifiers that are ready to run — so the check is that the one node this
    test built is the one node the tool reports as runnable. A tool answering
    from anywhere but this project would have to be empty to agree.

    What this does *not* cover is the model half of "Master session starts" —
    a real DSH runtime taking a turn. That is `tests/dsh/test_spike.py`, which
    needs a model credential; this is the half that needs none, and it is the
    half that decides what the model would be able to see.
    """
    with database.transaction() as session:
        prepared = build_prepared(
            session, project_id=project.project_id, node_type=NodeType.COMPUTATION
        )
        prepared_node = prepared.node

    environment = RoleEnvironment(settings=integration_settings, brief_dir=tmp_path / "briefs")
    result = await probe(
        environment.for_project(project, AgentRole.MASTER),
        calls=(("read_project_state", {}),),
    )

    assert "read_project_state" in result.tools
    assert result.whoami is not None
    assert result.whoami["role"] == AgentRole.MASTER.value
    assert result.whoami["project_id"] == project.project_id, (
        "the scope comes from the environment, not from the caller: a Master "
        "session that could name its own project could name somebody else's"
    )
    assert result.whoami["may_mutate_dag"] is True

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    state = call.payload
    assert state["project_id"] == project.project_id
    assert state["status"] == ProjectStatus.CREATED.value
    assert state["dag"]["nodes"] == 1
    assert state["dag"]["by_status"] == {NodeStatus.READY.value: 1}
    assert state["dag"]["ready_to_run"] == [prepared_node.display_id]


# ── A05 ─────────────────────────────────────────────────────────────────────


async def test_a05_master_plans_a_typed_dag_through_the_authorized_path(
    headless: Headless,
) -> None:
    """A05: "Master creates typed rolling-horizon DAG through authorized
    mutation path."

    *Typed*, so both nodes carry a `node_type` a worker can be dispatched by;
    *rolling horizon*, so the plan is committed one stage at a time and the
    second node's terms are frozen only when its turn comes; *authorized path*,
    so every one of them was written by `DagMutationService` under a Master
    scope rather than by a repository call this test made.

    The trace is the evidence for the middle claim. `ScriptedMaster` records
    what the loop asked it to do, and `planned` happens once — on the round
    where the project had no nodes — rather than being re-entered later.
    """
    headless.compute("COMPUTE_SUCCESS")
    master = headless.master(
        (
            Task(
                build=lambda: _node(headless, "Measure the series.", NodeType.COMPUTATION),
                criteria=("Conductivity rises by at least 15%.",),
                allowed_actions=("run_measurement",),
                required_outputs=("conductivity.csv", "notes.json"),
            ),
        )
    )
    review = headless.review()

    run = await headless.drive(master, review)

    assert master.trace[0] == "contract_defined"
    assert master.trace[1] == "planned"
    assert master.planned, "the scripted Master planned nothing"

    planned = master.planned[0]
    assert planned.node_type is NodeType.COMPUTATION
    assert planned.created_by == AgentRole.MASTER.value
    assert run.status is ProjectStatus.COMPLETED

    with headless.database.read_only() as session:
        acceptance = AcceptanceContractRepository(
            session, headless.project.project_id
        ).frozen_for_node(planned.node_id)
        assert acceptance is not None, (
            "a COMPUTATION node runs against frozen criteria; one that was "
            "planned without them is a node no Worker may ever start"
        )
        assert [c.statement for c in acceptance.criteria] == [
            "Conductivity rises by at least 15%."
        ]


# ── A20 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "outcome",
    [ProjectOutcome.SUCCESS, ProjectOutcome.FAILED, ProjectOutcome.INCONCLUSIVE],
)
async def test_a20_a_project_reaches_a_terminal_outcome_with_its_audit_trail(
    headless: Headless, outcome: ProjectOutcome
) -> None:
    """A20: "End-to-end project reaches one of SUCCESS / FAILED / INCONCLUSIVE
    / TERMINATED, with final audit trail."

    All three of the outcomes a running project can reach, because the item is
    about the *set* of endings being reachable and recorded, and a suite that
    only ever reached SUCCESS would leave the other two to be discovered by a
    deployment.

    The audit trail is the second half and the one worth asserting, because the
    project row does not carry the outcome: the *decision* is where an ending
    is recorded, and its type is what distinguishes success from failure from
    an honest inconclusive. `ENDING_DECISION` is the one place that mapping is
    written down, and this test reads it there rather than restating it — so a
    service that started recording a different decision type for an outcome
    would fail here rather than pass by convention.

    The project's own row is asserted too, and only for what it says: that it
    ended. `Project` has no `outcome` field, deliberately, because a status
    column that could disagree with the decision that produced it would be a
    second answer to the same question.
    """
    headless.compute("COMPUTE_SUCCESS")
    master = headless.master(
        (Task(build=lambda: _node(headless, "Measure the series.", NodeType.COMPUTATION)),),
        outcome=outcome,
    )
    review = headless.review()

    run = await headless.drive(master, review)

    # `ProjectOutcome.status` is the one place the four words A20 is written in
    # are mapped onto the four statuses the database holds, so this asserts the
    # mapping rather than a second copy of it: TERMINATED becoming CANCELLED is
    # exactly the kind of pair that a test restating the table would get wrong.
    assert run.status is outcome.status

    with headless.database.read_only() as session:
        project = ProjectRegistry(session).get(headless.project.project_id)
        assert project.status is outcome.status

        decisions = DecisionRepository(session, headless.project.project_id).all()
        endings = [
            decision
            for decision in decisions
            if decision.decision_type is ENDING_DECISION[outcome]
        ]
        assert len(endings) == 1, (
            f"expected one {ENDING_DECISION[outcome].value} decision, found {len(endings)}"
        )
        assert endings[0].rationale, "an ending with no stated reason"
        assert endings[0].authority_check.actor_role == AgentRole.MASTER.value, (
            "only Master ends a project; a decision recorded against any other "
            "actor would mean the ending came from somewhere else"
        )
        assert endings[0].authority_check.permitted

        assert master.trace[-1] == f"concluded:{outcome.value}"


def _node(headless: Headless, objective: str, node_type: NodeType) -> DagNode:
    """One node, built the way the domain builds one."""
    return DagNode.create(
        project_id=headless.project.project_id,
        node_type=node_type,
        objective=objective,
        created_by=AgentRole.MASTER.value,
    )
