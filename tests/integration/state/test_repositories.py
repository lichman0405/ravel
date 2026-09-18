"""Repositories against real PostgreSQL.

Two properties are asserted here that the unit suite cannot reach:

- **Project scope is authority, not a parameter.** A repository is bound to one
  project at construction, and a record aimed elsewhere is refused rather than
  quietly re-homed. The agent-supplied `project_id` is compared against the
  scope; it never becomes the scope.
- **A round trip is lossless.** Every field of every record survives the trip
  through a column and back, including the nested value objects stored as JSONB.
  A field that silently fails to persist is the kind of bug that only shows up
  as a wrong scientific conclusion three phases later.

The third property — only Master mutates the DAG — is the separation of powers,
and it is checked in both directions: the refusal *and* the fact that the
refusal left nothing behind.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ravel.domain.contracts import AcceptanceContract, AcceptanceCriterion, CriterionProvenance
from ravel.domain.dag import DagNode
from ravel.domain.enums import (
    AccessStatus,
    ClaimClass,
    EvidenceSourceTier,
    JoinPolicy,
    NodeStatus,
    NodeType,
)
from ravel.domain.evidence import Evidence, EvidenceSource
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.outbox import events_since, last_event_seq
from ravel.state.repositories.base import NotFound, ProjectScopeError
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ResearchContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.research import EvidenceRepository, EvidenceSourceRepository

pytestmark = pytest.mark.integration


def _a_node(project_id: str, **overrides) -> DagNode:
    """A node built the way production builds one, then adjusted.

    The `display_id` is generated rather than hardcoded: it is unique across the
    installation, not per project, so two projects' nodes cannot share one — an
    identifier that meant different things in different projects would make a
    Decision Record citing it ambiguous.
    """
    node = DagNode.create(
        project_id=project_id,
        node_type=NodeType.COMPUTATION,
        objective="Measure conductivity across the dopant series.",
        created_by="master",
        dependencies=("node-a", "node-b"),
        join_policy=JoinPolicy.ALL,
        roadmap_phase="phase-2",
    )
    return node.model_copy(update=overrides) if overrides else node


def _a_source(project_id: str, url: str, title: str) -> EvidenceSource:
    """A source that was actually read.

    `retrieved_at` is not optional decoration: the model refuses an `OK` source
    without one, because a source RAVEL could read must record when it read it.
    """
    return EvidenceSource(
        project_id=project_id,
        url=url,
        title=title,
        access_status=AccessStatus.OK,
        retrieved_at=datetime(2026, 3, 1, 9, 30, tzinfo=UTC),
        content_hash="sha256:" + "ab" * 32,
    )


# ── Project scope ───────────────────────────────────────────────────────────


def test_a_repository_requires_a_scope(database: Database) -> None:
    with pytest.raises(ValueError, match="project scope"), database.read_only() as session:
        DagRepository(session, "")


def test_a_record_from_another_project_is_refused(
    database: Database, project, other_project
) -> None:
    """The `project_id` an agent supplies is a value to check, not an authority."""
    with pytest.raises(ProjectScopeError, match="scoped to"), (
        database.transaction()
    ) as session:
        DagRepository(session, project.project_id).add_node(
            _a_node(other_project.project_id),
            role=AgentRole.MASTER,
            decision_ref="dec-1",
        )


def test_a_refused_record_is_not_written_anywhere(
    database: Database, project, other_project
) -> None:
    with pytest.raises(ProjectScopeError), database.transaction() as session:
        DagRepository(session, project.project_id).add_node(
            _a_node(other_project.project_id),
            role=AgentRole.MASTER,
            decision_ref="dec-1",
        )

    with database.read_only() as session:
        assert DagRepository(session, project.project_id).nodes() == []
        assert DagRepository(session, other_project.project_id).nodes() == []


def test_another_projects_node_reads_as_absent(database: Database, project, other_project) -> None:
    """A row outside the scope is indistinguishable from one that does not exist.

    That is the correct answer to give a caller with no authority over it: it
    reveals nothing about whether the other project has such a node.
    """
    with database.transaction() as session:
        node = DagRepository(session, project.project_id).add_node(
            _a_node(project.project_id), role=AgentRole.MASTER, decision_ref="dec-1"
        )

    with database.read_only() as session:
        foreign = DagRepository(session, other_project.project_id)
        with pytest.raises(NotFound):
            foreign.node(node.node_id)
        assert foreign.nodes() == []


def test_the_scope_filters_every_kind_of_repository(
    database: Database, project, other_project
) -> None:
    """Scoping is inherited, so it holds for records the base class never saw."""
    with database.transaction() as session:
        source = EvidenceSourceRepository(session, project.project_id).record(
            _a_source(project.project_id, "https://example.org/paper", "A paper")
        )

    with database.read_only() as session:
        foreign = EvidenceSourceRepository(session, other_project.project_id)
        with pytest.raises(NotFound):
            foreign.get(source_id=source.source_id)
        assert foreign.all() == []


def test_a_read_does_not_leak_across_projects(database: Database, project, other_project) -> None:
    with database.transaction() as session:
        for scope, objective in (
            (project.project_id, "The first project's question."),
            (other_project.project_id, "The second project's question."),
        ):
            DagRepository(session, scope).add_node(
                _a_node(scope, objective=objective),
                role=AgentRole.MASTER,
                decision_ref="dec-1",
            )

    with database.read_only() as session:
        first = DagRepository(session, project.project_id).nodes()
        second = DagRepository(session, other_project.project_id).nodes()

    assert [node.objective for node in first] == ["The first project's question."]
    assert [node.objective for node in second] == ["The second project's question."]


# ── Separation of powers ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "role",
    [
        AgentRole.RESEARCH,
        AgentRole.REVIEW,
        AgentRole.COMPUTE_WORKER,
        AgentRole.EXPERIMENTAL_WORKER,
    ],
)
def test_only_master_may_add_a_node(database: Database, project, role: AgentRole) -> None:
    """A worker that could edit the DAG could edit what it is judged against."""
    with pytest.raises(PermissionError, match="may not mutate the DAG"), (
        database.transaction()
    ) as session:
        DagRepository(session, project.project_id).add_node(
            _a_node(project.project_id), role=role, decision_ref="dec-1"
        )


@pytest.mark.parametrize(
    "role",
    [AgentRole.RESEARCH, AgentRole.REVIEW, AgentRole.COMPUTE_WORKER],
)
def test_only_master_may_cancel_a_node(database: Database, project, role: AgentRole) -> None:
    with database.transaction() as session:
        node = DagRepository(session, project.project_id).add_node(
            _a_node(project.project_id), role=AgentRole.MASTER, decision_ref="dec-1"
        )

    with pytest.raises(PermissionError, match="may not mutate the DAG"), (
        database.transaction()
    ) as session:
        DagRepository(session, project.project_id).cancel_node(
            node.node_id, role=role, decision_ref="dec-2"
        )

    with database.read_only() as session:
        stored = DagRepository(session, project.project_id).node(node.node_id)
    assert stored.status is NodeStatus.PLANNED


def test_a_refused_mutation_leaves_no_event(database: Database, project) -> None:
    """A refusal that still announced itself would be a lie in the stream."""
    with database.read_only() as session:
        before = last_event_seq(session, project.project_id)

    with pytest.raises(PermissionError), database.transaction() as session:
        DagRepository(session, project.project_id).add_node(
            _a_node(project.project_id), role=AgentRole.RESEARCH, decision_ref="dec-1"
        )

    with database.read_only() as session:
        assert last_event_seq(session, project.project_id) == before
        assert events_since(session, project.project_id, after_seq=before) == []


def test_a_dag_mutation_must_cite_a_decision(database: Database, project) -> None:
    """An unattributable change to the research plan is not permitted."""
    with pytest.raises(ValueError, match="Decision Record reference"), (
        database.transaction()
    ) as session:
        DagRepository(session, project.project_id).add_node(
            _a_node(project.project_id), role=AgentRole.MASTER, decision_ref=""
        )


# ── Round-trip fidelity ─────────────────────────────────────────────────────


def test_every_field_of_a_node_survives_the_round_trip(database: Database, project) -> None:
    """Including the value objects, which are stored as JSONB and re-validated."""
    original = _a_node(project.project_id)

    with database.transaction() as session:
        DagRepository(session, project.project_id).add_node(
            original, role=AgentRole.MASTER, decision_ref="dec-1"
        )

    with database.read_only() as session:
        stored = DagRepository(session, project.project_id).node(original.node_id)

    assert stored == original
    assert stored.created_at.tzinfo is not None, "timestamps come back timezone-aware"
    assert stored.join_policy is JoinPolicy.ALL
    assert stored.dependencies == ("node-a", "node-b")


def test_a_fully_populated_node_survives_the_round_trip(database: Database, project) -> None:
    """Every optional field set at once, since a null is the easy case."""
    original = _a_node(
        project.project_id,
        status=NodeStatus.WAITING_DECISION,
        started_at=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
        acceptance_contract_ref="acc-1",
        execution_contract_ref="exec-1",
        artifact_refs=("art-1", "art-2"),
        decision_ref="dec-1",
        failure_policy=None,
    )

    with database.transaction() as session:
        DagRepository(session, project.project_id).add_node(
            original, role=AgentRole.MASTER, decision_ref="dec-1"
        )

    with database.read_only() as session:
        stored = DagRepository(session, project.project_id).node(original.node_id)

    assert stored == original
    assert stored.artifact_refs == ("art-1", "art-2")


def test_a_project_survives_the_round_trip(database: Database, project) -> None:
    with database.read_only() as session:
        stored = ProjectRegistry(session).get(project.project_id)
    assert stored == project


def test_an_acceptance_contract_keeps_its_provenance(database: Database, project) -> None:
    """Provenance is what makes a criterion traceable to who demanded it."""
    with database.transaction() as session:
        node = DagRepository(session, project.project_id).add_node(
            _a_node(project.project_id), role=AgentRole.MASTER, decision_ref="dec-1"
        )

    contract = AcceptanceContract(
        project_id=project.project_id,
        node_id=node.node_id,
        criteria=(
            AcceptanceCriterion(
                statement="Conductivity rises by at least 15% over the control.",
                metric="conductivity_s_per_cm",
                threshold=">= 1.15x control",
                provenance=CriterionProvenance.USER_REQUIREMENT,
                provenance_ref="user-message-4",
            ),
            AcceptanceCriterion(
                statement="The effect reproduces across two independent runs.",
                metric="reproduction_count",
                threshold=">= 2",
                provenance=CriterionProvenance.PROVISIONAL,
                provenance_ref="dec-1",
            ),
        ),
        decision_ref="dec-1",
    )

    with database.transaction() as session:
        AcceptanceContractRepository(session, project.project_id).add(contract)

    with database.read_only() as session:
        stored = AcceptanceContractRepository(session, project.project_id).for_node(node.node_id)

    assert stored == contract
    assert [criterion.provenance for criterion in stored.criteria] == [
        CriterionProvenance.USER_REQUIREMENT,
        CriterionProvenance.PROVISIONAL,
    ]
    assert stored.frozen_at is None, "a contract is not frozen until Master freezes it"


def test_freezing_an_acceptance_contract_is_recorded(
    database: Database, project
) -> None:
    with database.transaction() as session:
        node = DagRepository(session, project.project_id).add_node(
            _a_node(project.project_id), role=AgentRole.MASTER, decision_ref="dec-1"
        )
        repository = AcceptanceContractRepository(session, project.project_id)
        contract = repository.add(
            AcceptanceContract(
                project_id=project.project_id,
                node_id=node.node_id,
                criteria=(
                    AcceptanceCriterion(
                        statement="Yield exceeds 80%.",
                        provenance=CriterionProvenance.USER_REQUIREMENT,
                    ),
                ),
            )
        )
        repository.freeze(contract.contract_id)

    with database.read_only() as session:
        frozen = AcceptanceContractRepository(session, project.project_id).frozen_for_node(
            node.node_id
        )
    assert frozen is not None
    assert frozen.frozen_at is not None
    assert frozen.contract_id == contract.contract_id


def test_evidence_keeps_its_source_references(database: Database, project) -> None:
    """An Evidence row with no source is an assertion, not a finding."""
    with database.transaction() as session:
        sources = EvidenceSourceRepository(session, project.project_id)
        first = sources.record(_a_source(project.project_id, "https://example.org/a", "Source A"))
        second = sources.record(_a_source(project.project_id, "https://example.org/b", "Source B"))
        evidence = Evidence(
            project_id=project.project_id,
            statement="The doped sample conducted 1.4x the control at 300 K.",
            claim_class=ClaimClass.FACT,
            source_tier=EvidenceSourceTier.A,
            source_refs=(first.source_id, second.source_id),
            conditions={"temperature": "300 K"},
        )
        EvidenceRepository(session, project.project_id).register(evidence, actor_id="researcher")

    with database.read_only() as session:
        repository = EvidenceRepository(session, project.project_id)
        stored = repository.get(evidence_id=evidence.evidence_id)
        via_first = repository.supported_by(first.source_id)
        via_second = repository.supported_by(second.source_id)

    assert stored == evidence
    assert stored.source_refs == (first.source_id, second.source_id)
    assert stored.conditions == {"temperature": "300 K"}
    # The claim is reachable from either source it rests on — a multi-source
    # claim is not filed under whichever one happened to be listed first.
    assert [claim.evidence_id for claim in via_first] == [evidence.evidence_id]
    assert [claim.evidence_id for claim in via_second] == [evidence.evidence_id]


def test_a_research_contract_round_trips_through_its_nested_budget(
    database: Database, project
) -> None:
    """A nested value object in JSONB must come back as the same object."""
    from ravel.domain.contracts import BudgetLimits, ResearchContract

    contract = ResearchContract(
        project_id=project.project_id,
        original_user_goal="Find a better dopant.",
        scientific_problem="Which dopant raises conductivity without hurting stability?",
        research_hypotheses=("Nitrogen raises conductivity.",),
        target_metrics=("conductivity_s_per_cm",),
        known_constraints=("No rare-earth dopants.",),
        budget_time_limits=BudgetLimits(max_compute_hours=40.0, max_iterations=25),
        uncertainties=("The furnace's upper limit is unverified.",),
    )

    with database.transaction() as session:
        ResearchContractRepository(session, project.project_id).add(contract)

    with database.read_only() as session:
        stored = ResearchContractRepository(session, project.project_id).current()

    assert stored == contract
    assert stored.budget_time_limits.max_compute_hours == 40.0
    assert stored.research_hypotheses == ("Nitrogen raises conductivity.",)


def test_a_project_that_does_not_exist_is_not_found(database: Database, project) -> None:
    with database.read_only() as session:
        with pytest.raises(NotFound):
            ProjectRegistry(session).get("no-such-project")
        assert ProjectRegistry(session).find("no-such-project") is None


def test_two_projects_are_isolated_end_to_end(database: Database, project, other_project) -> None:
    """The whole authorization story, in one test: same schema, disjoint views."""
    with database.transaction() as session:
        DagRepository(session, project.project_id).add_node(
            _a_node(project.project_id), role=AgentRole.MASTER, decision_ref="dec-1"
        )
        DagRepository(session, other_project.project_id).add_node(
            _a_node(other_project.project_id), role=AgentRole.MASTER, decision_ref="dec-2"
        )

    with database.read_only() as session:
        assert len(DagRepository(session, project.project_id).nodes()) == 1
        assert len(DagRepository(session, other_project.project_id).nodes()) == 1
        assert ProjectRegistry(session).list() != []


def test_a_project_row_records_who_created_it(database: Database, project: Project) -> None:
    """Attribution is part of the record, not something recovered from a log."""
    assert project.created_by, "a project without an author is not attributable"
