"""The two contracts a node runs under, written and frozen with the node.

A node nobody may run is not work; it is a line in a graph. Two rules decide
whether a node may run — `can_enter_running` demands frozen acceptance criteria
for the node types that have them, and demands an Execution Contract for every
node a Worker executes — and both are satisfied here, in the same transaction
that creates the node. A plan that could commit the first without the second
would be able to leave work in the DAG that no Worker may start, with nothing
in the project saying why it never does.

**Criteria are written and frozen in the same act, here.** `docs/03` separates
"criteria must be defined before entering RUNNING" from "freeze version on
execution start", and this is the earlier of the two: what is frozen is the
version the work will be measured against, and freezing it before the run is
what makes the *next* version the only way to change it. A criterion that could
be edited after a result arrived would make the review a statement about
whatever the criteria said at the time of writing.

**A node type that has no criteria gets no acceptance contract**, rather than
an empty one: a node measured against nothing is not measured, and an empty
contract would let a Review Record name a definition of done that contains
none. `requires_frozen_criteria` is the domain's answer to which types those
are, and it is asked rather than restated.

Everything here happens inside the caller's transaction, like the rest of this
package: nothing is in PostgreSQL until the caller commits, so a plan that is
refused part way through leaves no half-contracted node behind.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ravel.domain.artifacts import unusable_filename_reason
from ravel.domain.contracts import (
    AcceptanceContract,
    AcceptanceCriterion,
    ExecutionContract,
)
from ravel.domain.dag import DagNode
from ravel.domain.state_machines import requires_frozen_criteria
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.dag import DagRepository

__all__ = ["NodeTerms", "commit_terms"]


@dataclass(frozen=True, slots=True)
class NodeTerms:
    """What a node is measured against, and what it may do — as written.

    Held together because they are committed together, and because a caller
    that recorded one without the other would be reporting a node as ready to
    run when it is not.
    """

    #: The frozen criteria, for the node types that have them.
    acceptance: AcceptanceContract | None
    #: The terms a Worker executes under. Every node a Worker executes has one.
    execution: ExecutionContract


def commit_terms(
    session: Session,
    project_id: str,
    node: DagNode,
    *,
    criteria: tuple[AcceptanceCriterion, ...] = (),
    allowed_actions: tuple[str, ...] = (),
    required_outputs: tuple[str, ...] = (),
    allowed_retries: int = 0,
    procedure: str = "",
) -> NodeTerms:
    """Write both contracts for a node, freeze them, and bind them to it.

    `criteria` is required exactly for the node types the domain says must have
    them. Passing criteria for a node that does not need them is allowed and
    records them: a HYPOTHESIS node may be given something to be judged
    against, and refusing it would be this function inventing a rule.

    `required_outputs` are file names, and each is refused here if it could not
    be one. The check is not about tidiness: the backend names what it writes
    after the requirement, the completeness check compares the two by equality,
    and the storage key is built from the name — so an output that is really a
    sentence about the deliverable becomes an activity failing five retries
    deep, where the seat that wrote it cannot see it and nothing in the project
    says why the node never ended.

    Raises:
        ValueError: A node that must be measured against something was given
            nothing to be measured against, or a required output could not be
            an artifact's name — work that could never start, or could start and
            never deliver. Both are worth refusing at the moment they are
            planned rather than at the moment the scheduler finds them stuck.
    """
    if requires_frozen_criteria(node.node_type) and not criteria:
        raise ValueError(
            f"{node.node_type.value} node {node.display_id!r} was committed with no "
            "acceptance criteria, so no Worker could ever start it: this node type "
            "is measured against criteria frozen before the run, and a run with "
            "nothing to be measured against is not evidence of anything"
        )

    for output in required_outputs:
        problem = unusable_filename_reason(output)
        if problem is not None:
            raise ValueError(
                f"node {node.display_id!r} requires an output named {output!r}, "
                f"which is not a file name: {problem}. A required output is the "
                "name of a file the run delivers — 'conductivity_vs_x.csv' rather "
                "than a description of it — because that name is what the run "
                "writes, what the completeness check compares against, and what "
                "the artifact is stored under"
            )

    dag = DagRepository(session, project_id)
    acceptance = (
        _frozen_acceptance(session, project_id, node, criteria) if criteria else None
    )
    if acceptance is not None:
        dag.bind_acceptance_contract(node.node_id, acceptance.contract_id)

    executions = ExecutionContractRepository(session, project_id)
    executions.add(
        ExecutionContract(
            project_id=project_id,
            node_id=node.node_id,
            objective=node.objective,
            procedure=procedure,
            allowed_actions=allowed_actions,
            allowed_retries=allowed_retries,
            required_outputs=required_outputs,
            acceptance_contract_ref=(
                acceptance.contract_id if acceptance is not None else None
            ),
        )
    )
    execution = executions.freeze(executions.for_node(node.node_id).contract_id)
    dag.bind_execution_contract(node.node_id, execution.contract_id)

    return NodeTerms(acceptance=acceptance, execution=execution)


def _frozen_acceptance(
    session: Session,
    project_id: str,
    node: DagNode,
    criteria: tuple[AcceptanceCriterion, ...],
) -> AcceptanceContract:
    """The node's criteria, written and frozen, ready to be bound to it."""
    repository = AcceptanceContractRepository(session, project_id)
    written = AcceptanceContract(
        project_id=project_id, node_id=node.node_id, criteria=criteria
    )
    repository.add(written)
    return repository.freeze(written.contract_id)
