"""Writing a Review Record, and applying what it says.

Two acts, done together because doing them apart is how a node ends up sitting
in REVIEWING with a verdict beside it that nobody acted on.

**Submitting** is Review's act, and what this service adds to the repository is
the checking that makes a review a measurement rather than an opinion. Four
things are refused:

- a review at a checkpoint the node's status does not admit — a final review of
  work still in flight is a review of nothing;
- a review naming a contract the node does not have, or a different version of
  it than was frozen, which is the whole point of recording the version;
- criterion results naming criteria that were never frozen, which can only be a
  review of what someone hoped the criteria said;
- a final review that leaves a frozen criterion unreported. "Criterion by
  criterion" is what the spec asks Review for, and a verdict that skipped one
  would be a PASS over a question nobody answered.

**Applying** is RAVEL's act, and which of the three powers it belongs to
depends on the checkpoint. A FINAL verdict moves the node, because a node in
REVIEWING is waiting for exactly that and Review is the role that ends it. A
RUNTIME verdict moves nothing at all: `docs/02_AGENT_MODEL.md` §3 makes a
recommendation advisory, and acting on one is Master's. A PRE_RUN verdict of
FAIL or PARTIAL stops the node from running and parks it at WAITING_DECISION,
which is also not a plan change — the node keeps its place in the graph and
Master decides what to do about it.

Nothing here writes a Decision Record or touches the DAG's shape, because
nothing here may.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ravel.domain.contracts import AcceptanceContract, ExecutionContract
from ravel.domain.dag import DagNode
from ravel.domain.decisions import ReviewRecord
from ravel.domain.enums import NodeStatus, ReviewCheckpoint, ReviewOutcome
from ravel.domain.roles import AgentRole
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import ReviewRepository

__all__ = ["ReviewError", "ReviewService", "SubmittedReview"]

#: The statuses a review at each checkpoint may be submitted in.
#:
#: PRE_RUN is asked of a node waiting to run. FINAL is asked of a node that has
#: finished and is waiting to be judged. RUNTIME is asked of work in flight,
#: which includes a run that is blocked on something outside RAVEL — the
#: question "is this going the way it should" is answerable there, and often
#: most worth asking there.
_CHECKPOINT_STATUSES: dict[ReviewCheckpoint, frozenset[NodeStatus]] = {
    ReviewCheckpoint.PRE_RUN: frozenset({NodeStatus.READY}),
    ReviewCheckpoint.RUNTIME: frozenset(
        {NodeStatus.RUNNING, NodeStatus.WAITING_EXTERNAL, NodeStatus.WAITING_DECISION}
    ),
    ReviewCheckpoint.FINAL: frozenset({NodeStatus.REVIEWING}),
}

#: The node status a FINAL verdict moves to.
_ENDING: dict[ReviewOutcome, NodeStatus] = {
    ReviewOutcome.PASS: NodeStatus.PASSED,
    ReviewOutcome.FAIL: NodeStatus.FAILED,
    ReviewOutcome.PARTIAL: NodeStatus.PARTIAL,
}


class ReviewError(ValueError):
    """A review does not describe the node it claims to be about."""


@dataclass(frozen=True, slots=True)
class SubmittedReview:
    """A Review Record, and what writing it did to the node."""

    review: ReviewRecord
    node: DagNode
    #: Where the node moved, or `None` when this checkpoint moved nothing.
    moved_to: NodeStatus | None

    @property
    def moved(self) -> bool:
        """Whether applying this review changed the node's status."""
        return self.moved_to is not None


class ReviewService:
    """Submit reviews of one project's nodes, and apply what they say."""

    def __init__(self, session: Session, project_id: str) -> None:
        self.session = session
        self.project_id = project_id
        self.dag = DagRepository(session, project_id)
        self.reviews = ReviewRepository(session, project_id)
        self.acceptance = AcceptanceContractRepository(session, project_id)
        self.executions = ExecutionContractRepository(session, project_id)

    # ── Submitting ──────────────────────────────────────────────────────────

    def submit(self, review: ReviewRecord, *, actor_id: str | None = None) -> SubmittedReview:
        """Write a Review Record and apply it.

        Raises:
            NotFound: This project has no such node.
            ReviewError: The review is not one this node's state can carry.
        """
        node = self.dag.node(review.node_id)
        self._require_admissible(node, review)
        frozen = self._require_frozen_criteria(node, review)
        self._require_criteria_are_the_frozen_ones(frozen, review)

        written = self.reviews.submit(
            review, actor_id=actor_id or review.review_session_ref or AgentRole.REVIEW.value
        )
        return SubmittedReview(review=written, node=node, moved_to=self._apply(node, written))

    # ── Applying ────────────────────────────────────────────────────────────

    def _apply(self, node: DagNode, review: ReviewRecord) -> NodeStatus | None:
        """Move the node if this checkpoint's verdict moves it.

        Returns the status the node moved to, or `None` if it did not move.
        """
        match review.checkpoint:
            case ReviewCheckpoint.RUNTIME:
                # Advisory. The record is the whole of what Review produces
                # here; acting on it is Master's, through a Decision Record.
                return None
            case ReviewCheckpoint.PRE_RUN:
                if review.outcome is ReviewOutcome.PASS:
                    # The clearance gate now lets the node run. Nothing to move.
                    return None
                target = NodeStatus.WAITING_DECISION
            case _:
                target = _ENDING[review.outcome]

        if node.status is target:
            return None
        moved = self.dag.transition_node(
            node.node_id, target, actor_id=AgentRole.REVIEW.value
        )
        return moved.status

    # ── Checks ──────────────────────────────────────────────────────────────

    def _require_admissible(self, node: DagNode, review: ReviewRecord) -> None:
        """Refuse a review the node's status cannot carry."""
        allowed = _CHECKPOINT_STATUSES[review.checkpoint]
        if node.status in allowed:
            return
        raise ReviewError(
            f"a {review.checkpoint.value} review is of a node that is "
            f"{', '.join(sorted(status.value for status in allowed))}; "
            f"{node.display_id} is {node.status.value}"
        )

    def _require_frozen_criteria(
        self, node: DagNode, review: ReviewRecord
    ) -> AcceptanceContract | ExecutionContract:
        """The frozen definition of done this review must have measured against.

        A node that froze acceptance criteria is measured against them. One
        that did not — every node type but COMPUTATION and EXPERIMENT — is
        measured against its Execution Contract, which is frozen before the run
        and names what the node owed. Either way the review has to name the
        same contract and the same version the node actually had, so a verdict
        cannot be re-pointed at a criterion written after the fact.

        A node that has acceptance criteria and never froze them is refused
        rather than measured against its Execution Contract. It is the one case
        where falling back would be wrong rather than merely different: the
        criteria exist, so a reader would take the verdict to be about them,
        and they can still be rewritten.
        """
        frozen = self.acceptance.frozen_for_node(node.node_id)
        contract: AcceptanceContract | ExecutionContract
        if frozen is not None:
            contract = frozen
        else:
            self._refuse_unfrozen_criteria(node)
            try:
                contract = self.executions.for_node(node.node_id)
            except NotFound as error:
                raise ReviewError(
                    f"{node.display_id} has neither frozen acceptance criteria nor an "
                    "Execution Contract, so there is nothing this review could have "
                    "measured against"
                ) from error

        if review.frozen_criteria_ref != contract.contract_id:
            raise ReviewError(
                f"{review.display_id} measured against {review.frozen_criteria_ref!r}, "
                f"and {node.display_id} was {_kind(contract)} {contract.contract_id!r}"
            )
        if review.frozen_criteria_version != contract.version:
            raise ReviewError(
                f"{review.display_id} measured against version "
                f"{review.frozen_criteria_version} of {contract.contract_id!r}, which is "
                f"version {contract.version}"
            )
        return contract

    def _refuse_unfrozen_criteria(self, node: DagNode) -> None:
        """Refuse a node that wrote acceptance criteria and never froze them.

        Reached only when no frozen contract was found, so a contract here is
        by definition an unfrozen one.
        """
        try:
            unfrozen = self.acceptance.for_node(node.node_id)
        except NotFound:
            return  # the node has no acceptance criteria at all, which is fine
        raise ReviewError(
            f"{node.display_id} has acceptance criteria ({unfrozen.contract_id}) that "
            "were never frozen, so there is no definition of done this review could "
            "have been measured against; a verdict over criteria that can still be "
            "rewritten measures nothing"
        )

    def _require_criteria_are_the_frozen_ones(
        self, contract: AcceptanceContract | ExecutionContract, review: ReviewRecord
    ) -> None:
        """Refuse criterion results that name something nobody froze.

        Only an Acceptance Contract has criteria to be by. A review of a node
        measured against its Execution Contract reports a verdict without
        them, and this refuses results that name criteria anyway — they could
        only have come from a contract this node never had.
        """
        named = [result.criterion_id for result in review.criterion_results]
        if not isinstance(contract, AcceptanceContract):
            if named:
                raise ReviewError(
                    f"{review.display_id} reports on {len(named)} criterion/criteria, "
                    f"and {contract.contract_id!r} is an Execution Contract, which has "
                    "none to report on"
                )
            return

        known = contract.criteria_by_id()
        unknown = [criterion_id for criterion_id in named if criterion_id not in known]
        if unknown:
            raise ReviewError(
                f"{review.display_id} reports on {', '.join(sorted(unknown))}, which "
                f"{contract.contract_id!r} does not contain; a criterion that was not "
                "frozen cannot be one this result was measured against"
            )
        if len(set(named)) != len(named):
            raise ReviewError(
                f"{review.display_id} reports on the same criterion more than once"
            )

        if review.checkpoint is not ReviewCheckpoint.FINAL:
            return
        unreported = sorted(set(known) - set(named))
        if unreported:
            raise ReviewError(
                f"{review.display_id} is a final verdict and leaves "
                f"{len(unreported)} of {len(known)} frozen criteria unreported "
                f"({', '.join(unreported)}); a PASS over a criterion nobody answered "
                "is not a verdict this node's result can be given"
            )


def _kind(contract: AcceptanceContract | ExecutionContract) -> str:
    """What kind of contract this is, for an error message."""
    if isinstance(contract, AcceptanceContract):
        return "frozen against acceptance contract"
    return "measured against execution contract"
