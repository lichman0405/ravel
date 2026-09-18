"""Master's planning tools: the only path by which the DAG changes.

These handlers are thin on purpose. Every rule that makes a change legitimate —
who may make it, whether it falls inside the rolling horizon, whether it is
attributed to a Decision Record — is enforced by `DagMutationService` and the
repository beneath it. A handler that re-implemented any of those would be a
second copy of a rule, and the copies would drift.

What the handlers do add is the shape of the model's side of the boundary:
`project_id` is never a parameter, the Decision Record's *type* is derived from
the operation rather than accepted from the caller (so a cancellation cannot be
filed as a node creation), and a refusal comes back as a message rather than a
stack trace, because the agent has to be able to read why and try something
else.
"""

from __future__ import annotations

from typing import Any

from ravel.domain.dag import DagNode
from ravel.domain.enums import Confidence, DecisionType, JoinPolicy, NodeType
from ravel.mcp.context import ToolContext, as_json, require_master
from ravel.state.services.dag import DagMutationService, DecisionDraft

#: The confidence a decision is recorded with when the model does not say.
#: MEDIUM rather than HIGH: an unstated confidence is not evidence of a strong
#: one, and a record that overstates how sure Master was is worse than a blank.
_DEFAULT_CONFIDENCE = Confidence.MEDIUM


def _resolve_local_refs(
    specs: list[dict[str, Any]], built: list[DagNode]
) -> list[DagNode]:
    """Let a node in a batch depend on a sibling by the name the caller gave it.

    Node ids are assigned by RAVEL when a node is created, so a caller cannot
    name one that does not exist yet. Without this, a stage could only be
    committed as two calls — the measurements first, then the analysis that
    reads them — which is not how a stage is planned, and which would leave the
    stage half-expanded between the calls.

    A `ref` is a name local to one call. It is resolved here, before anything
    is written, and never reaches the database: what is stored is the node id,
    so a later reader sees an ordinary dependency.

    Raises:
        ValueError: Two nodes in the call claim the same `ref`, which would
            make a dependency on it ambiguous.
    """
    refs: dict[str, str] = {}
    for spec, node in zip(specs, built, strict=True):
        ref = spec.get("ref")
        if not ref:
            continue
        name = str(ref)
        if name in refs:
            raise ValueError(
                f"two nodes in this call are named {name!r}; a dependency on that "
                "name would be ambiguous, so give each node a distinct ref"
            )
        refs[name] = node.node_id

    return [
        node
        if not any(dependency in refs for dependency in node.dependencies)
        else node.model_copy(
            update={
                "dependencies": tuple(
                    refs.get(dependency, dependency) for dependency in node.dependencies
                )
            }
        )
        for node in built
    ]


def _draft(
    rationale: str,
    decision_type: DecisionType,
    confidence: str,
    evidence_refs: list[str] | None = None,
    alternatives_considered: list[str] | None = None,
) -> DecisionDraft:
    """Build the decision draft a mutation will be attributed to.

    Raises:
        ValueError: The rationale is empty, or the confidence is not one of the
            three levels. Both are refused here rather than deep inside the
            service so the message names the argument the model got wrong.
    """
    try:
        level = Confidence(confidence)
    except ValueError as exc:
        known = ", ".join(level.value for level in Confidence)
        raise ValueError(f"confidence must be one of {known}; got {confidence!r}") from exc
    return DecisionDraft(
        decision_type=decision_type,
        rationale=rationale,
        confidence=level,
        evidence_refs=tuple(evidence_refs or ()),
        alternatives_considered=tuple(alternatives_considered or ()),
    )


def _node(
    context: ToolContext,
    *,
    node_type: str,
    objective: str,
    roadmap_phase: str | None,
    dependencies: list[str] | None,
    join_policy: str | None,
    join_threshold: int | None,
) -> DagNode:
    """Build a node for this project from the arguments of a tool call.

    Raises:
        ValueError: A node type, join policy, or join configuration the domain
            does not accept.
    """
    try:
        parsed_type = NodeType(node_type)
    except ValueError as exc:
        known = ", ".join(kind.value for kind in NodeType)
        raise ValueError(f"node_type must be one of {known}; got {node_type!r}") from exc
    try:
        policy = JoinPolicy(join_policy) if join_policy else None
    except ValueError as exc:
        known = ", ".join(kind.value for kind in JoinPolicy)
        raise ValueError(f"join_policy must be one of {known}; got {join_policy!r}") from exc

    return DagNode.create(
        project_id=context.project_id,
        node_type=parsed_type,
        objective=objective,
        created_by=context.role.value,
        dependencies=tuple(dependencies or ()),
        join_policy=policy,
        join_threshold=join_threshold,
        roadmap_phase=roadmap_phase,
    )


def add_dag_node(context: ToolContext) -> Any:
    """Commit one node to the plan."""

    async def add_dag_node(
        node_type: str,
        objective: str,
        rationale: str,
        roadmap_phase: str | None = None,
        dependencies: list[str] | None = None,
        join_policy: str | None = None,
        join_threshold: int | None = None,
        confidence: str = _DEFAULT_CONFIDENCE.value,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add one node to the Scientific DAG, with the reason it is being added.

        `rationale` is not optional and is not a formality: the node is recorded
        against a Decision Record that names it, so a plan can always be read
        back together with why it was made. `dependencies` names node ids that
        must already exist, and a dependency fixes what the node waits for — it
        cannot be changed afterwards, so state it correctly or open a new node.

        `roadmap_phase` must be a stage within the planning horizon; adding work
        to a stage further ahead is refused, and the refusal says which stages
        are reachable.
        """
        require_master(context, "add_dag_node")
        node = _node(
            context,
            node_type=node_type,
            objective=objective,
            roadmap_phase=roadmap_phase,
            dependencies=dependencies,
            join_policy=join_policy,
            join_threshold=join_threshold,
        )
        draft = _draft(
            rationale,
            DecisionType.CREATE_NODE,
            confidence,
            evidence_refs=evidence_refs,
        )
        with context.write() as session:
            written = DagMutationService(session, context.project_id).add_node(
                node, role=context.role, decision=draft
            )
        return as_json(written)

    return add_dag_node


def expand_dag_phase(context: ToolContext) -> Any:
    """Commit a stage's worth of nodes under one decision."""

    async def expand_dag_phase(
        phase: str,
        nodes: list[dict[str, Any]],
        rationale: str,
        confidence: str = _DEFAULT_CONFIDENCE.value,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Commit the concrete work a roadmap stage consists of.

        `nodes` is a list of objects, each with `node_type`, `objective`, and
        optionally `ref`, `dependencies`, `join_policy`, `join_threshold`.

        A node's `dependencies` name nodes that already exist, or siblings in
        this same call — by the `ref` given to them here, since ids are not
        assigned until a node exists. So a stage's analysis can be committed in
        the same call as the measurement it reads, in any order.

        This is the operation the rolling horizon governs: the phase must be the
        current stage or one of the next two, because a detailed plan for a
        stage that far out would be written before its predecessors produced
        anything to plan from.
        """
        require_master(context, "expand_dag_phase")
        if not nodes:
            raise ValueError("expanding a phase commits no nodes; pass the work it consists of")
        built = [
            _node(
                context,
                node_type=str(spec["node_type"]),
                objective=str(spec["objective"]),
                roadmap_phase=None,
                dependencies=spec.get("dependencies"),
                join_policy=spec.get("join_policy"),
                join_threshold=spec.get("join_threshold"),
            )
            for spec in nodes
        ]
        built = _resolve_local_refs(nodes, built)
        draft = _draft(
            rationale,
            DecisionType.CREATE_NODE,
            confidence,
            evidence_refs=evidence_refs,
        )
        with context.write() as session:
            expansion = DagMutationService(session, context.project_id).expand_phase(
                phase, built, role=context.role, decision=draft
            )
        return {
            "phase": expansion.phase,
            "decision": as_json(expansion.decision),
            "nodes": as_json(expansion.nodes),
        }

    return expand_dag_phase


def cancel_dag_node(context: ToolContext) -> Any:
    """Stop a node, with the reason recorded."""

    async def cancel_dag_node(
        node_id: str,
        rationale: str,
        confidence: str = _DEFAULT_CONFIDENCE.value,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Cancel a node, recording the decision that ended it.

        Cancelling is a decision about the plan, not a report about the work, so
        it is refused for a node that has already reached a passing or failed
        end: those are results, and a result is not un-happened by cancelling
        it.
        """
        require_master(context, "cancel_dag_node")
        draft = _draft(
            rationale,
            DecisionType.CANCEL_NODE,
            confidence,
            evidence_refs=evidence_refs,
        )
        with context.write() as session:
            cancelled = DagMutationService(session, context.project_id).cancel_node(
                node_id, role=context.role, decision=draft
            )
        return as_json(cancelled)

    return cancel_dag_node


IMPLEMENTATIONS: dict[str, Any] = {
    "add_dag_node": add_dag_node,
    "expand_dag_phase": expand_dag_phase,
    "cancel_dag_node": cancel_dag_node,
}
