"""Artifacts, decisions, reviews, execution, identity, and the event stream."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ravel.domain.artifacts import (
    Artifact,
    ArtifactRegistration,
    ArtifactVersion,
    artifact_key,
)
from ravel.domain.clock import utcnow
from ravel.domain.decisions import (
    AffectedNodes,
    AuthorityCheck,
    CriterionResult,
    DecisionRecord,
    ReviewRecord,
)
from ravel.domain.enums import (
    ApprovalStatus,
    ClaimClass,
    CompletenessVerdict,
    Confidence,
    DecisionType,
    NodeStatus,
    NodeType,
    ProjectStatus,
    ReviewCheckpoint,
    ReviewOutcome,
    TerminationStatus,
    UserRole,
)
from ravel.domain.events import ActorType, ProjectEvent, ProjectEventType
from ravel.domain.execution import (
    CompletenessCheck,
    DeviationRecord,
    ExecutionAttempt,
    ExecutionRecord,
    WorkerMessage,
)
from ravel.domain.identity import (
    AgentIdentity,
    ApprovalRequest,
    MasterCheckpoint,
    ProjectMembership,
    User,
)
from ravel.domain.project import Project, Roadmap, RoadmapPhase
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import TransitionError

# ── Project and roadmap ─────────────────────────────────────────────────────


def _project() -> Project:
    return Project(title="Catalyst screen", objective="Find a better dopant.", created_by="u1")


def test_a_project_starts_created() -> None:
    assert _project().status is ProjectStatus.CREATED


def test_a_project_transition_returns_a_new_project() -> None:
    project = _project()
    defined = project.transition(ProjectStatus.CONTRACT_DEFINED)
    assert defined.status is ProjectStatus.CONTRACT_DEFINED
    assert project.status is ProjectStatus.CREATED
    assert defined.updated_at >= project.updated_at


def test_an_illegal_project_transition_raises() -> None:
    with pytest.raises(TransitionError):
        _project().transition(ProjectStatus.EXECUTING)


def test_pausing_records_a_reason_and_resuming_clears_it() -> None:
    executing = _project().transition(ProjectStatus.CONTRACT_DEFINED).transition(
        ProjectStatus.EXECUTING
    )
    paused = executing.model_copy(
        update={"status": ProjectStatus.PAUSED, "paused_reason": "budget review"}
    )
    assert paused.paused_reason == "budget review"
    assert paused.transition(ProjectStatus.EXECUTING).paused_reason is None


def test_a_project_gets_a_readable_display_id() -> None:
    assert _project().display_id.startswith("P-")


def test_roadmap_phases_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="ordered"):
        Roadmap(
            project_id="proj-a",
            phases=(
                RoadmapPhase(project_id="proj-a", name="Second", order=1),
                RoadmapPhase(project_id="proj-a", name="First", order=0),
            ),
        )


def test_two_roadmap_phases_cannot_share_an_order() -> None:
    with pytest.raises(ValidationError, match="share an `order`"):
        Roadmap(
            project_id="proj-a",
            phases=(
                RoadmapPhase(project_id="proj-a", name="A", order=0),
                RoadmapPhase(project_id="proj-a", name="B", order=0),
            ),
        )


def test_only_expanded_phases_are_committed_to_nodes() -> None:
    roadmap = Roadmap(
        project_id="proj-a",
        phases=(
            RoadmapPhase(project_id="proj-a", name="Near", order=0, expanded=True),
            RoadmapPhase(project_id="proj-a", name="Far", order=1),
        ),
    )
    assert [phase.name for phase in roadmap.expanded_phases()] == ["Near"]


# ── Artifacts ───────────────────────────────────────────────────────────────


def test_a_storage_key_is_derived_from_the_identity_and_version() -> None:
    assert (
        artifact_key("proj-a", "art-1", 2, "results.csv")
        == "projects/proj-a/art-1/2/results.csv"
    )


@pytest.mark.parametrize(
    ("project_id", "artifact_id", "filename"),
    [
        ("../etc", "art-1", "f.csv"),
        ("proj-a", "art-1", "../../f.csv"),
        ("proj-a", "art-1", ""),
        ("", "art-1", "f.csv"),
        ("proj-a", "a/b", "f.csv"),
    ],
)
def test_a_storage_key_refuses_to_escape_its_prefix(
    project_id: str, artifact_id: str, filename: str
) -> None:
    with pytest.raises(ValueError):
        artifact_key(project_id, artifact_id, 1, filename)


def test_versions_start_at_one() -> None:
    with pytest.raises(ValueError, match="starts at 1"):
        artifact_key("proj-a", "art-1", 0, "f.csv")


def _artifact() -> Artifact:
    return Artifact(project_id="proj-a", name="Docking results", created_by="worker-1")


def _version(artifact: Artifact, number: int = 1) -> ArtifactVersion:
    return ArtifactVersion(
        artifact_id=artifact.artifact_id,
        project_id=artifact.project_id,
        version=number,
        storage_key=artifact_key(artifact.project_id, artifact.artifact_id, number, "r.csv"),
        content_hash="sha256:deadbeef",
        size_bytes=128,
        created_by="worker-1",
    )


def test_an_artifact_registration_pairs_an_artifact_with_its_versions() -> None:
    artifact = _artifact()
    registration = ArtifactRegistration(artifact=artifact, versions=(_version(artifact),))
    assert registration.latest.version == 1
    assert registration.artifact.display_id.startswith("ART-")


def test_a_registration_needs_at_least_one_version() -> None:
    with pytest.raises(ValidationError):
        ArtifactRegistration(artifact=_artifact(), versions=())


def test_a_version_must_belong_to_the_artifact_it_is_registered_under() -> None:
    with pytest.raises(ValidationError, match="belongs to artifact"):
        ArtifactRegistration(artifact=_artifact(), versions=(_version(_artifact()),))


def test_versions_in_a_registration_must_be_distinct() -> None:
    artifact = _artifact()
    with pytest.raises(ValidationError, match="share a version number"):
        ArtifactRegistration(
            artifact=artifact, versions=(_version(artifact, 1), _version(artifact, 1))
        )


def test_artifact_versions_are_content_addressed_and_frozen() -> None:
    version = _version(_artifact())
    with pytest.raises(ValidationError):
        version.content_hash = "sha256:other"  # type: ignore[misc]
    assert version.sha256 == "deadbeef"


# ── Decisions and reviews ───────────────────────────────────────────────────


def _authority(**overrides: object) -> AuthorityCheck:
    defaults: dict[str, object] = {"actor_role": "master", "permitted": True}
    defaults.update(overrides)
    return AuthorityCheck(**defaults)  # type: ignore[arg-type]


def test_a_structural_decision_must_name_what_it_changed() -> None:
    with pytest.raises(ValidationError, match="names no affected nodes"):
        DecisionRecord(
            project_id="proj-a",
            decision_type=DecisionType.CREATE_NODE,
            rationale="The first hypothesis needs a control run.",
            confidence=Confidence.MEDIUM,
            authority_check=_authority(),
        )


def test_a_structural_decision_that_names_its_nodes_is_accepted() -> None:
    decision = DecisionRecord(
        project_id="proj-a",
        decision_type=DecisionType.CREATE_NODE,
        rationale="The first hypothesis needs a control run.",
        confidence=Confidence.MEDIUM,
        authority_check=_authority(),
        affected_nodes=AffectedNodes(created=("n1",)),
    )
    assert decision.display_id.startswith("DEC-")
    assert decision.affected_nodes.all_nodes == ("n1",)


def test_a_non_structural_decision_may_touch_nothing() -> None:
    decision = DecisionRecord(
        project_id="proj-a",
        decision_type=DecisionType.ACCEPT_RESULT,
        rationale="The result met every frozen criterion.",
        confidence=Confidence.HIGH,
        authority_check=_authority(),
    )
    assert decision.affected_nodes.is_empty


def test_a_decision_needs_a_rationale() -> None:
    with pytest.raises(ValidationError):
        DecisionRecord(
            project_id="proj-a",
            decision_type=DecisionType.ACCEPT_RESULT,
            rationale="",
            confidence=Confidence.HIGH,
            authority_check=_authority(),
        )


def test_affected_nodes_are_listed_without_duplicates() -> None:
    affected = AffectedNodes(created=("n1",), modified=("n1", "n2"))
    assert affected.all_nodes == ("n1", "n2")


def _results(satisfied: list[bool]) -> tuple[CriterionResult, ...]:
    return tuple(
        CriterionResult(criterion_id=f"c{index}", satisfied=value)
        for index, value in enumerate(satisfied)
    )


def _review(outcome: ReviewOutcome, satisfied: list[bool]) -> ReviewRecord:
    return ReviewRecord(
        project_id="proj-a",
        node_id="n1",
        checkpoint=ReviewCheckpoint.FINAL,
        frozen_acceptance_contract_ref="ctr-1",
        frozen_acceptance_version=1,
        outcome=outcome,
        criterion_results=_results(satisfied),
    )


def test_a_final_review_outcome_must_follow_from_its_criteria() -> None:
    """A FAIL beside three satisfied criteria is a contradiction to guess at."""
    with pytest.raises(ValidationError, match="satisfied all"):
        _review(ReviewOutcome.FAIL, [True, True])
    with pytest.raises(ValidationError, match="satisfied none"):
        _review(ReviewOutcome.PASS, [False, False])
    with pytest.raises(ValidationError, match="satisfied 1 of 2"):
        _review(ReviewOutcome.PASS, [True, False])


def test_a_consistent_final_review_is_accepted() -> None:
    assert _review(ReviewOutcome.PASS, [True, True]).satisfied_count == 2
    assert _review(ReviewOutcome.PARTIAL, [True, False]).satisfied_count == 1
    assert _review(ReviewOutcome.FAIL, [False, False]).satisfied_count == 0


def test_a_review_must_name_the_frozen_version_it_measured() -> None:
    with pytest.raises(ValidationError):
        ReviewRecord(
            project_id="proj-a",
            node_id="n1",
            checkpoint=ReviewCheckpoint.FINAL,
            frozen_acceptance_contract_ref="ctr-1",
            frozen_acceptance_version=0,
            outcome=ReviewOutcome.PASS,
        )


def test_a_pre_run_review_may_reach_a_verdict_without_results() -> None:
    review = ReviewRecord(
        project_id="proj-a",
        node_id="n1",
        checkpoint=ReviewCheckpoint.PRE_RUN,
        frozen_acceptance_contract_ref="ctr-1",
        frozen_acceptance_version=1,
        outcome=ReviewOutcome.PASS,
    )
    assert review.criterion_results == ()


# ── Execution ───────────────────────────────────────────────────────────────


def _completeness(**overrides: object) -> CompletenessCheck:
    defaults: dict[str, object] = {"verdict": CompletenessVerdict.COMPLETE}
    defaults.update(overrides)
    return CompletenessCheck(**defaults)  # type: ignore[arg-type]


def test_incomplete_delivery_must_name_what_is_missing() -> None:
    """Otherwise nobody can act on the report."""
    with pytest.raises(ValidationError, match="must name what is missing"):
        _completeness(verdict=CompletenessVerdict.INCOMPLETE_DELIVERY)


def test_a_complete_verdict_cannot_list_missing_outputs() -> None:
    with pytest.raises(ValidationError, match="cannot list missing outputs"):
        _completeness(missing_outputs=("metrics.json",))


def test_incomplete_delivery_is_not_a_scientific_failure() -> None:
    check = _completeness(
        verdict=CompletenessVerdict.INCOMPLETE_DELIVERY,
        required_outputs=("metrics.json", "logs.txt"),
        delivered_outputs=("logs.txt",),
        missing_outputs=("metrics.json",),
    )
    assert check.verdict is CompletenessVerdict.INCOMPLETE_DELIVERY


def _execution(**overrides: object) -> ExecutionRecord:
    defaults: dict[str, object] = {
        "project_id": "proj-a",
        "node_id": "n1",
        "execution_contract_ref": "ctr-1",
        "execution_contract_version": 1,
        "backend": "mock-compute",
        "completeness": _completeness(),
        "termination_status": TerminationStatus.COMPLETED,
    }
    defaults.update(overrides)
    return ExecutionRecord(**defaults)  # type: ignore[arg-type]


def test_attempts_are_numbered_consecutively_from_one() -> None:
    with pytest.raises(ValidationError, match="consecutively"):
        _execution(attempts=(ExecutionAttempt(attempt=2),))


def test_attempts_are_counted() -> None:
    record = _execution(attempts=(ExecutionAttempt(attempt=1), ExecutionAttempt(attempt=2)))
    assert record.attempt_count == 2


def test_delivery_completeness_is_a_separate_question_from_acceptance() -> None:
    assert _execution().delivery_is_complete


def test_a_deviation_stays_open_until_a_decision_resolves_it() -> None:
    deviation = DeviationRecord(
        project_id="proj-a",
        node_id="n1",
        execution_contract_ref="ctr-1",
        requested_action="change_temperature",
        description="The lab asked to run at 40C.",
        raised_by="experimental-worker",
    )
    assert deviation.is_open
    assert not deviation.permitted, "a deviation is by definition not permitted"


def test_worker_messages_are_limited_to_the_four_permitted_kinds() -> None:
    from ravel.domain.enums import WorkerMessageKind

    message = WorkerMessage(node_id="n1", kind=WorkerMessageKind.ESCALATE, body="Out of range.")
    assert message.kind is WorkerMessageKind.ESCALATE
    with pytest.raises(ValidationError):
        WorkerMessage(node_id="n1", kind="NEGOTIATE", body="Give me more scope.")  # type: ignore[arg-type]


# ── Identity ────────────────────────────────────────────────────────────────


def test_an_admin_may_not_direct_the_research() -> None:
    """Holding both operational and scientific authority collapses the split."""
    admin = ProjectMembership(project_id="proj-a", user_id="u1", role=UserRole.ADMIN)
    owner = ProjectMembership(project_id="proj-a", user_id="u2", role=UserRole.PROJECT_OWNER)
    lab = ProjectMembership(project_id="proj-a", user_id="u3", role=UserRole.LAB_USER)
    assert admin.is_admin and not admin.may_direct_project
    assert owner.may_direct_project and not owner.is_admin
    assert not lab.may_direct_project


def test_a_user_password_hash_is_not_required_at_creation() -> None:
    assert User(username="shibo").password_hash is None


def test_an_agent_identity_defaults_to_its_role_name() -> None:
    identity = AgentIdentity(project_id="proj-a", role=AgentRole.COMPUTE_WORKER)
    assert identity.display_name == "Compute Worker"


def test_touching_an_identity_returns_a_new_one() -> None:
    identity = AgentIdentity(project_id="proj-a", role=AgentRole.MASTER)
    touched = identity.touched()
    assert touched.last_seen_at is not None
    assert identity.last_seen_at is None


def test_an_approval_must_be_resolved_by_a_human_and_only_once() -> None:
    approval = ApprovalRequest(project_id="proj-a", requested_by="master", action="EXCEED_BUDGET")
    assert approval.is_pending
    resolved = approval.resolve(ApprovalStatus.APPROVED, resolved_by="u1", note="Approved.")
    assert resolved.status is ApprovalStatus.APPROVED
    assert resolved.resolved_by == "u1"
    assert not resolved.is_pending
    with pytest.raises(ValueError, match="already APPROVED"):
        resolved.resolve(ApprovalStatus.REJECTED, resolved_by="u2")


def test_an_approval_cannot_be_resolved_to_pending() -> None:
    approval = ApprovalRequest(project_id="proj-a", requested_by="master", action="EXCEED_BUDGET")
    with pytest.raises(ValueError, match="would not resolve it"):
        approval.resolve(ApprovalStatus.PENDING, resolved_by="u1")


def test_a_checkpoint_anchors_recovery_to_an_event_sequence() -> None:
    checkpoint = MasterCheckpoint(
        project_id="proj-a",
        master_identity_id="ident-1",
        current_focus="Choosing the second hypothesis.",
        last_event_seq=42,
    )
    assert checkpoint.last_event_seq == 42


def test_a_checkpoint_cannot_claim_a_negative_sequence() -> None:
    with pytest.raises(ValidationError):
        MasterCheckpoint(project_id="proj-a", master_identity_id="i", last_event_seq=-1)


# ── Events ──────────────────────────────────────────────────────────────────


def test_an_event_has_no_sequence_until_the_database_assigns_one() -> None:
    """The sequence must come from the single writer that can keep it gap-free."""
    event = ProjectEvent(
        project_id="proj-a",
        event_type=ProjectEventType.NODE_CREATED,
        actor_type=ActorType.AGENT,
        actor_id="master",
        payload={"node_id": "n1"},
    )
    assert event.seq is None


def test_an_event_serialises_for_the_wire() -> None:
    import json

    event = ProjectEvent(
        project_id="proj-a",
        seq=7,
        event_type=ProjectEventType.DAG_MUTATED,
        actor_type=ActorType.AGENT,
        actor_id="master",
    )
    wire = event.as_wire()
    json.dumps(wire)  # must not raise
    assert wire["event_type"] == "DAG_MUTATED"
    assert wire["seq"] == 7


def test_the_event_vocabulary_is_closed() -> None:
    """A new event type is a product decision, not an implementation detail."""
    assert len(ProjectEventType) == 23
    assert ProjectEventType.PROJECT_CREATED in set(ProjectEventType)
    with pytest.raises(ValueError):
        ProjectEventType("SOMETHING_ELSE")


def test_a_timestamp_must_carry_a_timezone() -> None:
    from ravel.domain.clock import ensure_utc

    with pytest.raises(ValueError, match="naive"):
        ensure_utc(utcnow().replace(tzinfo=None))
    assert ensure_utc(utcnow()).tzinfo is not None


def test_claim_classes_match_the_schema() -> None:
    assert {c.value for c in ClaimClass} == {"FACT", "INFERENCE", "HYPOTHESIS"}


def test_node_and_project_statuses_match_the_schema() -> None:
    assert len(NodeStatus) == 11
    assert len(NodeType) == 6
