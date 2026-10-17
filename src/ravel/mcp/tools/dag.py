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

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ravel.domain.contracts import AcceptanceCriterion, CriterionProvenance
from ravel.domain.dag import DagNode
from ravel.domain.enums import Confidence, DecisionType, JoinPolicy, NodeType
from ravel.domain.project import RoadmapPhase
from ravel.mcp.context import ToolContext, as_json, require_master
from ravel.state.repositories.projects import RoadmapRepository
from ravel.state.services.dag import DagMutationService, DecisionDraft
from ravel.state.services.terms import commit_terms

#: The confidence a decision is recorded with when the model does not say.
#: MEDIUM rather than HIGH: an unstated confidence is not evidence of a strong
#: one, and a record that overstates how sure Master was is worse than a blank.
_DEFAULT_CONFIDENCE = Confidence.MEDIUM


@dataclass(frozen=True, slots=True)
class NodeTermsSpec:
    """What one planned node is measured against, and what it may do.

    The model's side of the two contracts. Parsed from the tool call and handed
    to `commit_terms`, which writes and freezes them with the node — so a plan
    the model states here is one a Worker can start, rather than one that looks
    complete until the scheduler finds it stuck.
    """

    criteria: tuple[AcceptanceCriterion, ...] = ()
    inputs: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ()
    required_outputs: tuple[str, ...] = ()
    parameter_targets: dict[str, str] = field(default_factory=dict)
    allowed_ranges: dict[str, str] = field(default_factory=dict)
    allowed_substitutions: tuple[str, ...] = ()
    allowed_retries: int = 0
    stop_conditions: tuple[str, ...] = ()
    escalation_conditions: tuple[str, ...] = ()
    resource_limits: dict[str, str] = field(default_factory=dict)
    execution_requirements: dict[str, str] = field(default_factory=dict)
    procedure: str = ""

    def commit(self, session: Session, project_id: str, node: DagNode) -> None:
        commit_terms(
            session,
            project_id,
            node,
            criteria=self.criteria,
            inputs=self.inputs,
            allowed_actions=self.allowed_actions,
            required_outputs=self.required_outputs,
            parameter_targets=self.parameter_targets,
            allowed_ranges=self.allowed_ranges,
            allowed_substitutions=self.allowed_substitutions,
            allowed_retries=self.allowed_retries,
            stop_conditions=self.stop_conditions,
            escalation_conditions=self.escalation_conditions,
            resource_limits=self.resource_limits,
            execution_requirements=self.execution_requirements,
            procedure=self.procedure,
        )


def _terms(spec: dict[str, Any]) -> NodeTermsSpec:
    """Read one node's terms out of a tool call.

    Raises:
        ValueError: A criterion has no statement or no provenance, or names a
            provenance RAVEL does not know. Provenance is required rather than
            defaulted because it is what tells a benchmark from a guess: a
            criterion the model inferred and one the user required are the same
            sentence and very different evidence, and a default would make
            every criterion the model wrote look like the stronger of the two.
    """
    raw = spec.get("criteria") or []
    criteria = tuple(_criterion(item) for item in raw)
    retries = spec.get("allowed_retries", 0)
    return NodeTermsSpec(
        criteria=criteria,
        inputs=tuple(str(name) for name in spec.get("inputs") or ()),
        allowed_actions=tuple(str(action) for action in spec.get("allowed_actions") or ()),
        # Stripped because a name is a name: a stray space around one is a
        # typo, not a different deliverable. Whether what is left could be a
        # file name at all is `commit_terms`'s question, and it is asked there
        # so that both contract writers go through it.
        required_outputs=tuple(
            str(name).strip() for name in spec.get("required_outputs") or ()
        ),
        parameter_targets=_string_map(spec.get("parameter_targets")),
        allowed_ranges=_string_map(spec.get("allowed_ranges")),
        allowed_substitutions=tuple(
            str(entry) for entry in spec.get("allowed_substitutions") or ()
        ),
        allowed_retries=int(retries),
        stop_conditions=tuple(str(entry) for entry in spec.get("stop_conditions") or ()),
        escalation_conditions=tuple(
            str(entry) for entry in spec.get("escalation_conditions") or ()
        ),
        resource_limits=_string_map(spec.get("resource_limits")),
        execution_requirements=_string_map(spec.get("execution_requirements")),
        procedure=str(spec.get("procedure") or ""),
    )


def _string_map(raw: Any) -> dict[str, str]:
    """A name-to-value map out of a tool call, with every value a string.

    Coerced rather than trusted: tool-call arguments arrive as JSON, so a
    parameter target the model wrote as the number `298` arrives as `298`, and
    a contract whose `parameter_targets["temperature_k"]` is an int where every
    reader expects a string is a difference that surfaces in the preparation
    layer rather than here. The *contract's* validators are what decide whether
    the values mean anything; this only makes them the shape the schema says.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"expected an object of name to value, got {raw!r}; write it as "
            '{"temperature_k": "298"}'
        )
    return {str(key): str(value) for key, value in raw.items()}


def _criterion(item: Any) -> AcceptanceCriterion:
    """One acceptance criterion, as the model stated it.

    Raises:
        ValueError: The criterion is not an object, or is missing something a
            criterion cannot be without.
    """
    if not isinstance(item, dict):
        raise ValueError(
            "each criterion must be an object with a 'statement' and a "
            f"'provenance'; got {item!r}"
        )
    statement = str(item.get("statement") or "").strip()
    if not statement:
        raise ValueError(
            f"a criterion with no statement measures nothing; got {item!r}"
        )
    provenance = item.get("provenance")
    if not provenance:
        raise ValueError(
            f"the criterion {statement!r} states no provenance. Say where it comes "
            "from — user_requirement, literature_derived, standard, "
            "authoritative_database, prior_project_result, research_inference, or "
            "provisional — because a criterion with no provenance cannot be "
            "presented as an objective benchmark"
        )
    try:
        source = CriterionProvenance(str(provenance))
    except ValueError as exc:
        known = ", ".join(level.value for level in CriterionProvenance)
        raise ValueError(
            f"provenance must be one of {known}; got {provenance!r}"
        ) from exc
    return AcceptanceCriterion(
        statement=statement,
        provenance=source,
        metric=str(item.get("metric") or ""),
        threshold=str(item.get("threshold") or ""),
        provenance_ref=(
            str(item["provenance_ref"]) if item.get("provenance_ref") else None
        ),
        notes=str(item.get("notes") or ""),
    )


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
        criteria: list[dict[str, Any]] | None = None,
        inputs: list[str] | None = None,
        allowed_actions: list[str] | None = None,
        required_outputs: list[str] | None = None,
        parameter_targets: dict[str, str] | None = None,
        allowed_ranges: dict[str, str] | None = None,
        allowed_substitutions: list[str] | None = None,
        allowed_retries: int = 0,
        stop_conditions: list[str] | None = None,
        escalation_conditions: list[str] | None = None,
        resource_limits: dict[str, str] | None = None,
        execution_requirements: dict[str, str] | None = None,
        procedure: str = "",
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

        The node's terms travel with it, because a node without them is work
        nobody may start. `criteria` is required for COMPUTATION and EXPERIMENT
        nodes and is a list of objects with a `statement` and a `provenance`
        (`metric`, `threshold`, `provenance_ref` and `notes` are optional);
        `allowed_actions` is what the Worker may do without asking (an action
        that is not listed is refused, not assumed), `required_outputs` is what
        the run must deliver for the result to be complete — each entry the
        *name of a file*, such as `conductivity_vs_x.csv`, since that name is
        what the run writes and the artifact is stored under — and
        `allowed_retries` is how many times an infrastructure failure may be
        retried.

        The rest of the terms are what the run is to do and the room it has to
        move in, and a node whose work is a computation or an experiment is
        under-specified without them:

        - `parameter_targets` — the values this run uses, `{"temperature_k":
          "298"}`. What you write here is what is run; nothing downstream fills
          in a value you left out, and a node whose preparation finds a
          parameter missing is parked for you to decide about rather than
          guessed at.
        - `allowed_ranges` — the window each parameter may be varied inside,
          written `"8..12"`. A Worker checks a value against this; a range that
          is not written as one is refused here rather than halfway through a
          run.
        - `allowed_substitutions` — permitted swaps, written `"A -> B"`.
        - `inputs` — what the run reads, named.
        - `stop_conditions` and `escalation_conditions` — when the Worker stops
          by itself, and when it stops and asks you.
        - `resource_limits` — what the run may consume.
        - `execution_requirements` — whether RAVEL must *build* an environment
          before this node runs: `{"software": "raspa"}` for a computation,
          `{"lab": "bench-chemistry"}` for an experiment a person carries out.
          Omit it for a node that needs no workspace — reading, writing, or
          analysing what is already there — and nothing is prepared for it.
          Naming an environment RAVEL does not have a materializer for is not
          refused here; it becomes a refusal at preparation time that comes
          back to you as a node waiting on your decision.

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
        terms = _terms(
            {
                "criteria": criteria,
                "inputs": inputs,
                "allowed_actions": allowed_actions,
                "required_outputs": required_outputs,
                "parameter_targets": parameter_targets,
                "allowed_ranges": allowed_ranges,
                "allowed_substitutions": allowed_substitutions,
                "allowed_retries": allowed_retries,
                "stop_conditions": stop_conditions,
                "escalation_conditions": escalation_conditions,
                "resource_limits": resource_limits,
                "execution_requirements": execution_requirements,
                "procedure": procedure,
            }
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
            terms.commit(session, context.project_id, written)
        return as_json(written)

    return add_dag_node


def commit_roadmap_phase(context: ToolContext) -> Any:
    """Write one stage of the roadmap: the plan above the DAG."""

    async def commit_roadmap_phase(
        name: str,
        order: int,
        intent: str,
    ) -> dict[str, Any]:
        """Add one stage to this project's roadmap.

        The roadmap is the coarse half of the plan: the stages the project goes
        through, in order, before any of them is detailed. A node is committed
        as work for a stage by name, and `expand_dag_phase` can only expand a
        stage that exists — so on a project with no roadmap this is the first
        planning act, and nothing else can happen until it has been done.

        `order` is the stage's position, counting from 0; it is how the horizon
        is read, and the stage the project is on is the first one that is not
        settled. `intent` says what the stage is for, in the terms the plan is
        argued in — it is what a later reader has instead of the conversation.

        A stage is deliberately coarse: RAVEL commits detailed work only to the
        current stage and the two after it, so the rest of the roadmap is a
        direction rather than a promise. Write the stages you can defend now;
        the ones after the next results can be added later.

        Returns the stage as written, together with the roadmap and the stages
        that may now be expanded.
        """
        require_master(context, "commit_roadmap_phase")
        phase_name = name.strip()
        if not phase_name:
            raise ValueError("a roadmap stage needs a name; a node names its stage by name")
        if order < 0:
            raise ValueError(f"a stage's order counts from 0; got {order}")
        stated_intent = intent.strip()
        if not stated_intent:
            raise ValueError(
                f"stage {phase_name!r} states no intent. Say what the stage is for: "
                "a stage remembered only by its name is a plan nobody can argue with"
            )
        # Read before writing so the refusal names what the project already has.
        # The uniqueness is a constraint on the table as well — this is the
        # message, not the rule.
        with context.read() as session:
            existing = RoadmapRepository(session, context.project_id).phases()
        taken = {phase.name: phase for phase in existing}
        if phase_name in taken:
            duplicate = taken[phase_name]
            raise ValueError(
                f"this project already has a stage named {phase_name!r} (order "
                f"{duplicate.order}); it has {_stage_names(existing)}. Add work to it "
                "with expand_dag_phase, or name a different stage"
            )
        if any(phase.order == order for phase in existing):
            holder = next(phase for phase in existing if phase.order == order)
            raise ValueError(
                f"order {order} is already {holder.name!r}; it has {_stage_names(existing)}"
            )
        phase = RoadmapPhase(
            project_id=context.project_id,
            name=phase_name,
            order=order,
            intent=stated_intent,
        )
        # No Decision Record: a decision records what changed among the *nodes*,
        # and a stage on its own commits none. The decision comes when the stage
        # is expanded, and that is where the plan becomes checkable.
        with context.write() as session:
            written = RoadmapRepository(session, context.project_id).register(
                phase, role=context.role
            )
            horizon = DagMutationService(session, context.project_id).horizon()
        return {
            "phase": as_json(written),
            "roadmap": [as_json(item) for item in horizon.phases],
            "current_phase": horizon.current.name if horizon.current else None,
            "expandable_phases": list(horizon.reachable_names),
        }

    return commit_roadmap_phase


def _stage_names(phases: Any) -> str:
    """The stages a project has, for a refusal that has to be actionable."""
    names = [f"{phase.name!r} (order {phase.order})" for phase in phases]
    return ", ".join(names) if names else "no stages yet"


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
        optionally `ref`, `dependencies`, `join_policy`, `join_threshold`, and
        the terms the node runs under, which are the same ones `add_dag_node`
        takes and mean the same things: `criteria` (required for COMPUTATION
        and EXPERIMENT nodes — a list of objects with a `statement` and a
        `provenance`), `inputs`, `allowed_actions`, `required_outputs` (each
        entry the name of a file the run delivers), `parameter_targets`,
        `allowed_ranges`, `allowed_substitutions`, `allowed_retries`,
        `stop_conditions`, `escalation_conditions`, `resource_limits`,
        `execution_requirements`, and `procedure`. State a node's parameters
        and its execution requirement with the node: a stage expanded without
        them is a stage of work that is planned but not specified, and the
        node that needs an environment prepared will be parked for you later
        rather than run.

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
        terms = [_terms(spec) for spec in nodes]
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
            # Terms after the nodes exist, in the same transaction: an
            # expansion that failed to write them would leave a stage of work
            # no Worker may start, and either all of it is committed or none.
            for spec, node in zip(terms, expansion.nodes, strict=True):
                spec.commit(session, context.project_id, node)
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
    "commit_roadmap_phase": commit_roadmap_phase,
    "add_dag_node": add_dag_node,
    "expand_dag_phase": expand_dag_phase,
    "cancel_dag_node": cancel_dag_node,
}
