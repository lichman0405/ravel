"""Review's two acts: reading what is to be judged, and judging it.

Review is the role that ends a node, and it holds nothing else. It cannot
change the plan, cannot revise a contract, and cannot write the criteria it is
measuring against — so the tools here are exactly two: what is waiting, and the
verdict. Everything else a reviewer might want is in the package the second
tool returns.

Two decisions are worth stating because they are what the handlers add to the
services beneath them:

- **The frozen definition of done is read, not taken from the caller.** Which
  contract a verdict must name is a fact about the node, and a model
  transcribing two opaque identifiers it cannot check would be adding a way to
  get it wrong without adding anything a reader could use. The criterion
  *statements* are copied from the frozen contract for the same reason: Review
  states what it found, and the criteria are what it found it against.
- **Which checkpoint applies is read from the node's status**, through the same
  table the service refuses against, so a reviewer is never told a checkpoint
  is available and then refused for asking.

No handler here writes a Decision Record or touches the DAG's shape, because
nothing Review does may.
"""

from __future__ import annotations

from typing import Any

from ravel.domain.contracts import AcceptanceContract, ExecutionContract
from ravel.domain.dag import DagNode
from ravel.domain.decisions import CriterionResult, ReviewRecord
from ravel.domain.enums import ReviewCheckpoint, ReviewOutcome
from ravel.mcp.context import ToolContext, as_json, require_review
from ravel.review.checkpoints import (
    admissible_checkpoints,
    latest_at,
    pre_run_clearance,
    required_checkpoints,
)
from ravel.review.service import ReviewService
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import RecordRepositories, ReviewRepository
from ravel.state.repositories.research import ArtifactRepository


def _holds_up_the_project(node: DagNode, reviews: list[ReviewRecord]) -> bool:
    """Whether the project is waiting on a verdict about this node.

    A node in REVIEWING has nobody else to end it, and a node waiting to run
    that owes a pre-flight review cannot start without one — both are work the
    project is stuck on. A RUNTIME review is advice that moves nothing, so
    nothing is ever waiting for it.
    """
    checkpoint = _checkpoint_for(node)
    if checkpoint is ReviewCheckpoint.FINAL:
        return True
    if checkpoint is ReviewCheckpoint.PRE_RUN:
        # Asked of the domain's own readable rule rather than of a second
        # reading of "has it passed yet", so a node already cleared to run
        # stops appearing as work.
        return not pre_run_clearance(node, reviews).allowed
    return False


def _checkpoint_for(node: DagNode) -> ReviewCheckpoint | None:
    """The checkpoint a node in this status is to be reviewed at, if any.

    Read from `admissible_checkpoints` rather than decided here: a node's
    status admits one checkpoint, the service refuses a review at any other,
    and both the queue and the refusal have to be readings of one table.
    """
    admissible = admissible_checkpoints(node.status)
    return admissible[0] if admissible else None


def _definition_of_done(
    service: ReviewService, node: DagNode
) -> tuple[dict[str, Any] | None, str | None]:
    """The frozen contract this node is measured against, and what it has none.

    Returns the contract as JSON, or `None` and the reason. A node whose
    criteria were never frozen is not merely unreviewable — it is a fault in
    the plan that Master has to fix, and the reviewer is the one who would
    otherwise discover it by being refused. So the refusal travels with the
    queue entry rather than being swallowed.
    """
    try:
        contract = service.definition_of_done(node)
    except ValueError as refusal:
        return None, str(refusal)
    return as_definition(contract), None


def as_definition(contract: AcceptanceContract | ExecutionContract) -> dict[str, Any]:
    """A contract as JSON, with what kind of contract it is.

    The kind is added because the two are read differently and only one of them
    has criteria: a reviewer that had to work out which it was holding from the
    fields present would be inferring something the record already knows.
    """
    dumped: dict[str, Any] = as_json(contract)
    dumped["kind"] = (
        "acceptance_contract"
        if isinstance(contract, AcceptanceContract)
        else "execution_contract"
    )
    return dumped


def _summary(contract: dict[str, Any]) -> dict[str, Any]:
    """What a queue entry needs to know about a node's definition of done.

    Kept short on purpose: the queue is a list, and the criterion-by-criterion
    detail belongs in the package for the one node being judged.
    """
    return {
        "kind": contract["kind"],
        "contract_id": contract["contract_id"],
        "version": contract["version"],
        "criteria": len(contract.get("criteria") or ()),
    }


def read_review_work(context: ToolContext) -> Any:
    """What is waiting to be judged, and what is merely reviewable."""

    async def read_review_work() -> dict[str, Any]:
        """List the nodes a verdict is waiting on now.

        Two lists. `held_up` is work the project cannot get past without a
        verdict: a node that has finished and is waiting for one, or a node
        that cannot start until its pre-flight review clears it. `open_to_review`
        is work in flight, where a RUNTIME review is possible and moves
        nothing — asking for one is a choice, not an obligation.

        Each entry names the checkpoint, the frozen contract the verdict must
        be about, and the last verdict at that checkpoint. Read the package for
        the node you are about to judge before writing anything down.
        """
        require_review(context, "read_review_work")
        with context.read() as session:
            service = ReviewService(session, context.project_id)
            dag = DagRepository(session, context.project_id)
            reviews = ReviewRepository(session, context.project_id)
            entries = [
                _queue_entry(service, reviews, node)
                for node in dag.nodes()
                if admissible_checkpoints(node.status)
            ]

        return {
            "held_up": [entry for entry in entries if entry["holds_up_the_project"]],
            "open_to_review": [
                entry for entry in entries if not entry["holds_up_the_project"]
            ],
        }

    return read_review_work


def _queue_entry(
    service: ReviewService, reviews: ReviewRepository, node: DagNode
) -> dict[str, Any]:
    """One node's line in the queue: what it is, and what is expected of it."""
    checkpoint = _checkpoint_for(node)
    assert checkpoint is not None, "the queue only holds nodes with a checkpoint"
    written = reviews.for_node(node.node_id)
    previous = latest_at(written, checkpoint)
    contract, unreviewable = _definition_of_done(service, node)
    return {
        "node_id": node.node_id,
        "display_id": node.display_id,
        "node_type": node.node_type.value,
        "objective": node.objective,
        "status": node.status.value,
        "checkpoint": checkpoint.value,
        "holds_up_the_project": _holds_up_the_project(node, written),
        "definition_of_done": _summary(contract) if contract is not None else None,
        "unreviewable": unreviewable,
        "last_review_at_this_checkpoint": (
            {
                "review_id": previous.review_id,
                "display_id": previous.display_id,
                "outcome": previous.outcome.value,
                "created_at": previous.created_at.isoformat(),
            }
            if previous is not None
            else None
        ),
    }


def read_review_package(context: ToolContext) -> Any:
    """Everything about one node that a verdict has to be measured against."""

    async def read_review_package(node_id: str) -> dict[str, Any]:
        """Read one node's task, terms, execution, artifacts and prior verdicts.

        `definition_of_done` is what this verdict is measured against: the
        criteria frozen before the run, or — for a node type that has none —
        the Execution Contract the work was performed under. Quote its
        criteria by `criterion_id` in your verdict, and report a final verdict
        on every one of them.

        `execution` is the immutable record of what actually ran, including
        every attempt, what was delivered against what was required, and how
        the run ended. `artifacts` is what the node produced, with the hash and
        size of each version, so a claim about a result can be traced to bytes.

        Read this before submitting anything: a verdict that reports on a
        criterion this node never had is refused, and the refusal is the whole
        point of freezing criteria before the run.
        """
        require_review(context, "read_review_package")
        with context.read() as session:
            service = ReviewService(session, context.project_id)
            dag = DagRepository(session, context.project_id)
            node = dag.node(node_id)
            definition, unreviewable = _definition_of_done(service, node)
            records = RecordRepositories(session, context.project_id)
            execution = records.latest_execution(node_id)
            artifacts = ArtifactRepository(session, context.project_id)
            produced = [
                {
                    "artifact": as_json(artifacts.get(artifact_id=artifact_id)),
                    "versions": as_json(artifacts.versions(artifact_id)),
                }
                for artifact_id in node.artifact_refs
            ]
            reviews = records.reviews.for_node(node_id)
            deviations = [
                deviation
                for deviation in records.deviations.all()
                if deviation.node_id == node_id
            ]

        return {
            "node": as_json(node),
            "checkpoints": [
                checkpoint.value for checkpoint in admissible_checkpoints(node.status)
            ],
            "checkpoints_owed": [
                checkpoint.value for checkpoint in required_checkpoints(node.node_type)
            ],
            "definition_of_done": definition,
            "unreviewable": unreviewable,
            "execution": as_json(execution) if execution is not None else None,
            "artifacts": produced,
            "reviews": as_json(reviews),
            "deviations": as_json(deviations),
        }

    return read_review_package


def submit_review(context: ToolContext) -> Any:
    """Record a verdict, and let the one part that is Review's be applied."""

    async def submit_review(
        node_id: str,
        checkpoint: str,
        outcome: str,
        criterion_results: list[dict[str, Any]] | None = None,
        diagnosis: str = "",
        recommendations: list[str] | None = None,
        evidence_refs: list[str] | None = None,
        artifact_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Submit a verdict on one node at one checkpoint.

        `checkpoint` is PRE_RUN, RUNTIME or FINAL, and only the one the node's
        status admits is accepted. A FINAL verdict ends the node: PASS, FAIL
        and PARTIAL become its result, and it stays in the record as that. A
        RUNTIME verdict is advice and moves nothing — Master decides what to do
        about it. A PRE_RUN verdict that is not a PASS parks the node at
        WAITING_DECISION rather than letting it run.

        `criterion_results` is a list of objects, each with a `criterion_id`
        from the package and `satisfied` (true or false), and optionally
        `observed`, `note`, `evidence_refs` and `artifact_refs`. A final
        verdict must answer every frozen criterion one by one; a verdict that
        skips one is refused, because a criterion nobody answered is a
        criterion nobody checked.

        `diagnosis` says what you found, and `recommendations` says what you
        would do about it. Both reach Master, and neither decides anything:
        acting on a recommendation is Master's, through a Decision Record.
        """
        require_review(context, "submit_review")
        parsed_checkpoint = _checkpoint(checkpoint)
        parsed_outcome = _outcome(outcome)
        with context.write() as session:
            service = ReviewService(session, context.project_id)
            node = DagRepository(session, context.project_id).node(node_id)
            contract = service.definition_of_done(node)
            review = ReviewRecord(
                project_id=context.project_id,
                node_id=node_id,
                checkpoint=parsed_checkpoint,
                # Read from the node rather than accepted from the caller: the
                # contract a verdict was measured against is a fact about the
                # project. The service still refuses a review that names some
                # other contract, which is what keeps this a reading and not an
                # exemption.
                frozen_criteria_ref=contract.contract_id,
                frozen_criteria_version=contract.version,
                outcome=parsed_outcome,
                criterion_results=_results(contract, criterion_results),
                diagnosis=diagnosis,
                recommendations=tuple(recommendations or ()),
                evidence_refs=tuple(evidence_refs or ()),
                artifact_refs=tuple(artifact_refs or ()),
            )
            submitted = service.submit(review, role=context.role)

        return {
            "review": as_json(submitted.review),
            "status_before": submitted.node.status.value,
            "moved_to": submitted.moved_to.value if submitted.moved_to else None,
        }

    return submit_review


def _checkpoint(value: str) -> ReviewCheckpoint:
    """The checkpoint the model named.

    Raises:
        ValueError: It is not one of the three. Named here rather than deep
            inside the service so the message lists the ones that exist.
    """
    try:
        return ReviewCheckpoint(value)
    except ValueError as exc:
        known = ", ".join(level.value for level in ReviewCheckpoint)
        raise ValueError(f"checkpoint must be one of {known}; got {value!r}") from exc


def _outcome(value: str) -> ReviewOutcome:
    """The verdict the model reached.

    Raises:
        ValueError: It is not one of the three outcomes.
    """
    try:
        return ReviewOutcome(value)
    except ValueError as exc:
        known = ", ".join(level.value for level in ReviewOutcome)
        raise ValueError(f"outcome must be one of {known}; got {value!r}") from exc


def _results(
    contract: AcceptanceContract | ExecutionContract,
    results: list[dict[str, Any]] | None,
) -> tuple[CriterionResult, ...]:
    """The model's verdict on each criterion, as a record.

    The statement is copied from the frozen contract when the criterion is one
    of its own, so what the record says was measured is what was frozen rather
    than what the reviewer typed from memory. A criterion this contract does
    not contain is left for `ReviewService` to refuse — the rule lives there,
    and a second copy of it here would be a second thing to keep right.
    """
    known = (
        contract.criteria_by_id() if isinstance(contract, AcceptanceContract) else {}
    )
    built: list[CriterionResult] = []
    for item in results or ():
        if not isinstance(item, dict):
            raise ValueError(
                f"each criterion result must be an object with a 'criterion_id' "
                f"and 'satisfied'; got {item!r}"
            )
        criterion_id = str(item.get("criterion_id") or "").strip()
        if not criterion_id:
            raise ValueError(
                f"a criterion result names no criterion, so there is nothing it "
                f"could be about; got {item!r}"
            )
        if "satisfied" not in item:
            raise ValueError(
                f"the result for {criterion_id!r} does not say whether the "
                "criterion was satisfied; a verdict that leaves it out answers "
                "nothing"
            )
        # `isinstance` rather than truthiness, because `bool("false")` is
        # `True`: a verdict that arrived as the string `"false"` — which is
        # what a model writes when it is being careful in the wrong way — would
        # be recorded as this criterion having been met. That is a scientific
        # judgement silently inverted, and it is the one error in a review that
        # nobody downstream can detect. The type is demanded rather than
        # coerced; a caller that means false has a way to say so.
        satisfied = item["satisfied"]
        if not isinstance(satisfied, bool):
            raise ValueError(
                f"the result for {criterion_id!r} says satisfied={satisfied!r}, "
                "which is not true or false; a verdict on a criterion is one or "
                "the other, and a value that could be read either way is not a "
                "verdict"
            )
        frozen = known.get(criterion_id)
        built.append(
            CriterionResult(
                criterion_id=criterion_id,
                statement=frozen.statement if frozen is not None else "",
                satisfied=satisfied,
                observed=str(item.get("observed") or ""),
                note=str(item.get("note") or ""),
                evidence_refs=tuple(str(ref) for ref in item.get("evidence_refs") or ()),
                artifact_refs=tuple(str(ref) for ref in item.get("artifact_refs") or ()),
            )
        )
    return tuple(built)


IMPLEMENTATIONS: dict[str, Any] = {
    "read_review_work": read_review_work,
    "read_review_package": read_review_package,
    "submit_review": submit_review,
}
