"""Which reviews a node owes, and which of them hold it up.

The spec defines three checkpoints — PRE_RUN, RUNTIME, FINAL — and they are not
three of the same thing. Each answers a different question at a different
moment, and each has a different effect on the node it is about:

- **PRE_RUN** is asked before an expensive or irreversible run: is what this
  node is about to do worth doing, and is it fit to be measured afterwards? Only
  the two node types that freeze acceptance criteria are asked it, because for
  them "fit to be measured" is a question with an answer in the record — the
  criteria exist, and someone can read them. A node that does not run a backend
  has nothing to pre-flight.
- **RUNTIME** is asked while the work is in flight, and it moves nothing. That
  is not an oversight: `docs/02_AGENT_MODEL.md` §3 makes a Review
  recommendation advisory, and the only role that may act on one is Master.
- **FINAL** is asked once, of the result, and it is what ends the node. It is
  required of every node that ran, whatever its type, because a node sitting in
  REVIEWING is a node nobody has judged.

This module reads records and returns values. It does not write anything, and it
does not move a node: `ravel.review.service` is where a verdict is applied.
"""

from __future__ import annotations

from dataclasses import dataclass

from ravel.domain.dag import DagNode
from ravel.domain.decisions import ReviewRecord
from ravel.domain.enums import NodeStatus, NodeType, ReviewCheckpoint, ReviewOutcome
from ravel.domain.state_machines import FROZEN_CRITERIA_NODE_TYPES

__all__ = [
    "PRE_RUN_NODE_TYPES",
    "Clearance",
    "latest_at",
    "pre_run_clearance",
    "required_checkpoints",
]

#: The node types a pre-flight review is asked of. Derived from the frozen
#: criteria rule rather than written out a second time: a node that must have
#: acceptance criteria before it runs is exactly a node whose readiness to run
#: can be reviewed against something a reader can check.
PRE_RUN_NODE_TYPES: frozenset[NodeType] = FROZEN_CRITERIA_NODE_TYPES


@dataclass(frozen=True, slots=True)
class Clearance:
    """Whether a node may be handed to a worker yet."""

    allowed: bool
    reason: str

    def raise_if_denied(self) -> None:
        """Turn a denial into an exception.

        Raises:
            NotClearedError: The node has not been cleared to run.
        """
        if not self.allowed:
            raise NotClearedError(self.reason)


class NotClearedError(RuntimeError):
    """A node was asked to run before its pre-flight review cleared it."""


def required_checkpoints(node_type: NodeType) -> tuple[ReviewCheckpoint, ...]:
    """Every checkpoint a node of this type owes, in the order it owes them."""
    if node_type in PRE_RUN_NODE_TYPES:
        return (ReviewCheckpoint.PRE_RUN, ReviewCheckpoint.FINAL)
    return (ReviewCheckpoint.FINAL,)


def latest_at(
    reviews: tuple[ReviewRecord, ...] | list[ReviewRecord], checkpoint: ReviewCheckpoint
) -> ReviewRecord | None:
    """The most recent review at one checkpoint, if there has been one.

    Latest rather than first, because a PRE_RUN review may legitimately happen
    twice: a Master who revises a contract and sends a node back to be reviewed
    again has produced a second opinion about a changed plan, and the second is
    the one that describes what is about to run.
    """
    matching = [review for review in reviews if review.checkpoint is checkpoint]
    return matching[-1] if matching else None


def pre_run_clearance(
    node: DagNode, reviews: tuple[ReviewRecord, ...] | list[ReviewRecord]
) -> Clearance:
    """Whether a node has been cleared to run by its pre-flight review.

    A node whose type owes no pre-flight review is clear: the answer for a
    RESEARCH or HYPOTHESIS node is not "yes, reviewed" but "this question does
    not apply to it", and saying so is different from passing.

    **This is the readable form of a rule that is enforced elsewhere.** The
    gate itself is in `DagNode.can_enter_running`, which every path into
    RUNNING passes through — this function exists for the callers that hold the
    reviews and want to say *why* a node cannot start yet: the loop choosing
    what to do next, the TUI explaining a wait, a test naming the verdict that
    denied it. A caller that only needs the answer should ask the transition,
    because that is the one that can refuse.
    """
    if node.node_type not in PRE_RUN_NODE_TYPES:
        return Clearance(
            True,
            f"a {node.node_type.value} node owes no pre-flight review",
        )
    if node.status is not NodeStatus.READY:
        return Clearance(
            False,
            f"{node.display_id} is {node.status.value}; only a node waiting to run "
            "can be cleared to run",
        )
    review = latest_at(reviews, ReviewCheckpoint.PRE_RUN)
    if review is None:
        return Clearance(
            False,
            f"{node.display_id} has not been reviewed before running; a "
            f"{node.node_type.value} node freezes its acceptance criteria first, "
            "and the pre-flight review is what reads them",
        )
    if review.outcome is not ReviewOutcome.PASS:
        return Clearance(
            False,
            f"{node.display_id} was reviewed before running and the verdict was "
            f"{review.outcome.value} ({review.display_id}): {review.diagnosis}",
        )
    return Clearance(True, f"{node.display_id} was cleared to run by {review.display_id}")
