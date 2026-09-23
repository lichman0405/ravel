"""Contracts: what the user asked for, what a worker may do, what counts as done.

Three contracts exist and they answer three different questions:

- `ResearchContract` — what the user actually wants, and what RAVEL is not
  allowed to do while getting it.
- `ExecutionContract` — what one Worker may do, frozen before it starts. A
  Worker never invents authority; it checks whether an action is explicitly
  permitted and pauses if it is not.
- `AcceptanceContract` — what counts as success, frozen before a
  COMPUTATION/EXPERIMENT runs, with provenance for every criterion.

`AuthorityEnvelope` bounds what Master may decide without asking a human.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.enums import CriterionProvenance, UserRole
from ravel.domain.ids import new_id

#: What a permitted range looks like: two decimal numbers separated by `..`,
#: both ends included. `"8..12"` permits 8 and 12.
#:
#: The format is fixed here because a range that cannot be read mechanically is
#: a range a Worker cannot check against — and the Worker's whole rule is that
#: it *checks* rather than judges. A free-text range would push the decision
#: back onto a model, which is the one thing the contract exists to prevent.
_RANGE = re.compile(r"^\s*(?P<low>-?\d+(?:\.\d+)?)\s*\.\.\s*(?P<high>-?\d+(?:\.\d+)?)\s*$")

#: What a permitted substitution looks like: the thing named, `->`, its
#: replacement. `"reagent-A -> reagent-B"`.
_SUBSTITUTION = re.compile(r"^\s*(?P<given>[^>]+?)\s*->\s*(?P<instead>[^>]+?)\s*$")

#: The kinds of environment a contract may require RAVEL to materialize before
#: a run. A closed list, like `allowed_actions` and for the same reason: a
#: requirement RAVEL does not recognise cannot be materialized, and a contract
#: that named one would otherwise be discovered by a Worker starting a run in a
#: workspace nothing had prepared.
#:
#: The *kind* is fixed here and the *name* is not. `software` names a compute
#: package RAVEL can build inputs for (`raspa`); `lab` names a kind of benchtop
#: package RAVEL assembles for a person to carry out. Which names exist is a
#: deployment fact — it is what the preparation registries hold — so an
#: unsupported name is refused when the contract is materialized rather than
#: when it is written, and the refusal is a record Master answers.
EXECUTION_REQUIREMENT_KINDS: frozenset[str] = frozenset({"software", "lab"})


def _range(span: str) -> tuple[float, float] | None:
    """The two ends of a range, or `None` if it is not written as one."""
    match = _RANGE.match(span)
    if match is None:
        return None
    return float(match.group("low")), float(match.group("high"))


def substitution_parts(entry: str) -> tuple[str, str] | None:
    """A permitted substitution split into what it names and what replaces it.

    Here rather than beside its two callers — the check that reads a
    substitution back and preparation, which writes the reagent list a bench
    works from — because the format is the contract's, and a second reader of
    it would be a second definition of what `"A -> B"` means.

    Returns `None` for an entry that is not written as one; construction
    refuses those, and a caller meeting one here is looking at a row that got
    past the model rather than at a substitution.
    """
    match = _SUBSTITUTION.match(entry)
    if match is None:
        return None
    return match.group("given").strip(), match.group("instead").strip()


def _number(value: float | int | str) -> float | None:
    """A value as a number, or `None` if it is not one.

    `bool` is refused explicitly rather than by accident: it is an `int` in
    Python, and a `True` that compared successfully against `1..1` would let a
    flag pass a check meant for a measurement.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value.strip())
    except (AttributeError, ValueError):
        return None


class BudgetLimits(Record):
    """Time and resource limits. All optional; absence means unbounded."""

    max_wall_clock_hours: float | None = Field(default=None, gt=0)
    max_compute_hours: float | None = Field(default=None, gt=0)
    max_iterations: int | None = Field(default=None, gt=0)
    max_experiment_runs: int | None = Field(default=None, gt=0)
    max_cost_units: float | None = Field(default=None, gt=0)


class ResearchContract(Record):
    """The original user goal, translated into a scientific problem.

    Immutable once created. A change to what the user wants is a new contract,
    not an edit to this one — otherwise the record of what RAVEL was asked to
    do would silently follow what it ended up doing.
    """

    contract_id: str = Field(default_factory=new_id)
    project_id: str
    original_user_goal: str = Field(min_length=1)
    business_context: str = ""
    scientific_problem: str = Field(min_length=1)
    research_hypotheses: tuple[str, ...] = ()
    target_metrics: tuple[str, ...] = ()
    acceptance_strategy: str = ""
    known_constraints: tuple[str, ...] = ()
    assumed_constraints: tuple[str, ...] = ()
    available_resources: tuple[str, ...] = ()
    prohibited_actions: tuple[str, ...] = ()
    budget_time_limits: BudgetLimits = Field(default_factory=BudgetLimits)
    uncertainties: tuple[str, ...] = ()
    authority_envelope_ref: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ProjectSuccessContract(Record):
    """What project success and failure mean, frozen before formal execution.

    A later change is versioned and requires a Decision Record. Without this,
    "success" quietly becomes whatever the project achieved.
    """

    contract_id: str = Field(default_factory=new_id)
    project_id: str
    version: int = Field(default=1, ge=1)
    success_criteria: tuple[str, ...] = Field(min_length=1)
    failure_criteria: tuple[str, ...] = ()
    termination_criteria: tuple[str, ...] = ()
    budget_time_limits: BudgetLimits = Field(default_factory=BudgetLimits)
    unresolved_uncertainty_policy: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    supersedes: str | None = None
    decision_ref: str | None = None


class ApprovalRequirement(Record):
    """One class of action that needs a human before Master may take it."""

    action: str = Field(min_length=1)
    required_role: UserRole
    rationale: str = ""


class AuthorityEnvelope(Record):
    """What Master may decide alone, and what needs a human.

    The envelope is data rather than prompt text so that "Master asked for
    permission" is a check against a record, not an agent's recollection of
    being cautious.
    """

    envelope_id: str = Field(default_factory=new_id)
    project_id: str
    version: int = Field(default=1, ge=1)
    requires_approval: tuple[ApprovalRequirement, ...] = ()
    max_dag_nodes_without_approval: int | None = Field(default=None, gt=0)
    max_parallel_branches_without_approval: int | None = Field(default=None, gt=0)
    budget_time_limits: BudgetLimits = Field(default_factory=BudgetLimits)
    created_at: datetime = Field(default_factory=utcnow)

    def requirement_for(self, action: str) -> ApprovalRequirement | None:
        """The approval this action needs, if any."""
        for requirement in self.requires_approval:
            if requirement.action == action:
                return requirement
        return None

    def requires_human_approval(self, action: str) -> bool:
        """Whether an action must be approved by a human before it is taken."""
        return self.requirement_for(action) is not None


class AcceptanceCriterion(Record):
    """One measurable condition, with where it came from.

    `provenance` is required and has no default. A criterion whose origin is
    unrecorded cannot be presented as an objective benchmark, and making the
    field optional would let that happen by omission.
    """

    criterion_id: str = Field(default_factory=new_id)
    statement: str = Field(min_length=1)
    metric: str = ""
    threshold: str = ""
    provenance: CriterionProvenance
    provenance_ref: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _provenance_needs_a_reference(self) -> AcceptanceCriterion:
        """A criterion that cites literature must say which literature.

        `PROVISIONAL` is exempt: it is the honest label for a threshold RAVEL
        chose itself, and demanding a reference for it would push an author
        toward claiming a source they do not have.
        """
        external = {
            CriterionProvenance.LITERATURE_DERIVED,
            CriterionProvenance.STANDARD,
            CriterionProvenance.AUTHORITATIVE_DATABASE,
            CriterionProvenance.PRIOR_PROJECT_RESULT,
        }
        if self.provenance in external and not self.provenance_ref:
            raise ValueError(
                f"provenance {self.provenance.value} requires provenance_ref to name "
                "the source it came from"
            )
        return self


class AcceptanceContract(Record):
    """The frozen success definition for one COMPUTATION/EXPERIMENT node.

    Freezing is a one-way door: once `frozen_at` is set, no further version may
    be produced for this node. A wrong benchmark is handled by leaving the node
    at FAIL/PARTIAL, recording a Decision Record, and opening a new node — never
    by editing the criteria the result was measured against.
    """

    contract_id: str = Field(default_factory=new_id)
    project_id: str
    node_id: str
    version: int = Field(default=1, ge=1)
    criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1)
    frozen_at: datetime | None = None
    supersedes: str | None = None
    decision_ref: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_frozen(self) -> bool:
        """Whether this contract has been frozen for execution."""
        return self.frozen_at is not None

    def freeze(self, at: datetime | None = None) -> AcceptanceContract:
        """Return a frozen copy.

        Idempotent: freezing an already-frozen contract returns it unchanged,
        so a retried activity does not fault.
        """
        if self.is_frozen:
            return self
        return self.model_copy(update={"frozen_at": at or utcnow()})

    def criteria_by_id(self) -> dict[str, AcceptanceCriterion]:
        """The criteria keyed by identifier."""
        return {criterion.criterion_id: criterion for criterion in self.criteria}


class ExecutionContract(Record):
    """What one Worker is permitted to do for one node.

    Frozen before the node enters RUNNING. The Worker's rule is mechanical:
    is the requested action explicitly permitted? If yes, execute; if no,
    pause and escalate. The Worker is never asked to judge whether an action is
    scientifically material — only whether the contract names it.
    """

    contract_id: str = Field(default_factory=new_id)
    project_id: str
    node_id: str
    version: int = Field(default=1, ge=1)
    objective: str = Field(min_length=1)
    procedure: str = ""
    inputs: tuple[str, ...] = ()
    #: The closed list of actions a Worker may take. An action absent from
    #: this list is not "probably fine" — it is forbidden, and the Worker
    #: escalates. An empty list means the node cannot act at all, which is a
    #: valid way to author a contract that only waits.
    allowed_actions: tuple[str, ...] = ()
    parameter_targets: dict[str, str] = Field(default_factory=dict)
    allowed_ranges: dict[str, str] = Field(default_factory=dict)
    allowed_retries: int = Field(default=0, ge=0)
    allowed_substitutions: tuple[str, ...] = ()
    required_outputs: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...] = ()
    escalation_conditions: tuple[str, ...] = ()
    resource_limits: dict[str, str] = Field(default_factory=dict)
    #: The environment the run needs prepared, keyed by kind: `{"software":
    #: "raspa"}` or `{"lab": "bench-chemistry"}`. Empty means the run needs
    #: nothing built for it, which is the case for every node whose work is
    #: reading, writing, or being judged — and for every contract written
    #: before this field existed.
    #:
    #: The requirement is stated by the contract rather than inferred from the
    #: node type, because whether a run needs a workspace is a property of the
    #: work: a COMPUTATION node that runs a calculation needs one, a
    #: COMPUTATION node that analyses numbers already on disk does not, and
    #: Master is the role that knows which it is planning.
    execution_requirements: dict[str, str] = Field(default_factory=dict)
    acceptance_contract_ref: str | None = None
    frozen_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_frozen(self) -> bool:
        """Whether this contract has been frozen for execution."""
        return self.frozen_at is not None

    @property
    def requires_preparation(self) -> bool:
        """Whether RAVEL must materialize an environment before this run starts.

        The one question the execution loop asks before it prepares anything.
        A contract that names no environment is not prepared — it is executed
        by a Worker under the terms it already has — which is what keeps the
        nodes that need nothing built for them running exactly as they did
        before preparation existed.
        """
        return bool(self.execution_requirements)

    def freeze(self, at: datetime | None = None) -> ExecutionContract:
        """Return a frozen copy. Idempotent."""
        if self.is_frozen:
            return self
        return self.model_copy(update={"frozen_at": at or utcnow()})

    @field_validator("allowed_ranges")
    @classmethod
    def _ranges_are_readable(cls, ranges: dict[str, str]) -> dict[str, str]:
        """Refuse a range that is not written as one.

        Checked when the contract is built rather than when a Worker consults
        it. The alternative is a contract that reads as permissive and cannot be
        checked — and since the Worker's rule is to treat what it cannot check
        as forbidden, a typo would surface as an unexplained escalation halfway
        through an experiment instead of as an error where it was made.
        """
        for parameter, span in ranges.items():
            if _range(span) is None:
                raise ValueError(
                    f"allowed_ranges[{parameter!r}] is {span!r}, which is not a "
                    "range; write it as two numbers separated by '..', for "
                    "example '8..12'"
                )
        return ranges

    @field_validator("allowed_substitutions")
    @classmethod
    def _substitutions_are_readable(
        cls, substitutions: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Refuse a substitution that does not say what replaces what."""
        for entry in substitutions:
            if _SUBSTITUTION.match(entry) is None:
                raise ValueError(
                    f"allowed_substitutions contains {entry!r}, which does not "
                    "name a replacement; write it as '<given> -> <instead>'"
                )
        return substitutions

    @field_validator("execution_requirements")
    @classmethod
    def _requirements_name_a_known_environment(
        cls, requirements: dict[str, str]
    ) -> dict[str, str]:
        """Refuse a requirement RAVEL would not know what to do with.

        Two ways a requirement can be unmaterializable at the moment it is
        written, and both are refused here rather than at the run: a kind that
        is not in the closed list, and a requirement that names nothing. The
        second is not pedantry — `{"software": ""}` reads as "this run has a
        software requirement" to every reader and to any check that asks
        whether the dict is non-empty, while naming nothing to prepare.

        A recognised kind with an unsupported name is *not* refused here.
        Which software RAVEL can build inputs for is a deployment fact rather
        than a domain one, so the answer belongs to the preparation layer, and
        its answer is a refusal Master decides on.
        """
        for kind, name in requirements.items():
            if kind not in EXECUTION_REQUIREMENT_KINDS:
                known = ", ".join(sorted(EXECUTION_REQUIREMENT_KINDS))
                raise ValueError(
                    f"execution_requirements names {kind!r}, which is not a kind "
                    f"of environment RAVEL prepares; the kinds are {known}. Write "
                    'it as {"software": "<package>"} or {"lab": "<kind of package>"}'
                )
            if not name.strip():
                raise ValueError(
                    f"execution_requirements[{kind!r}] names nothing to prepare, "
                    "so the contract reads as one that requires an environment "
                    "while saying nothing about which"
                )
        return requirements

    def permits(self, action: str) -> bool:
        """Whether the contract explicitly names an action as permitted."""
        return action in self.allowed_actions

    def retries_permitted(self) -> bool:
        """Whether a deterministic retry is allowed at all."""
        return self.allowed_retries > 0

    def permitted_range(self, parameter: str) -> tuple[float, float] | None:
        """The window this contract permits for a parameter, if it names one."""
        span = self.allowed_ranges.get(parameter)
        return None if span is None else _range(span)

    def permits_value(self, parameter: str, value: float | int | str) -> bool:
        """Whether the contract explicitly permits this value for a parameter.

        Closed list, like `permits`, and for the same reason: a parameter the
        contract does not mention is not permitted, and neither is a value that
        cannot be read as a number. The Worker is never asked whether a value is
        close enough or scientifically equivalent — only whether it falls inside
        what was written down before the experiment started.
        """
        window = self.permitted_range(parameter)
        if window is None:
            return False
        number = _number(value)
        if number is None:
            return False
        low, high = window
        return low <= number <= high

    def permits_substitution(self, given: str, instead: str) -> bool:
        """Whether replacing `given` with `instead` is explicitly permitted.

        Exact after trimming, and case-sensitive. Chemical, biological, and
        gene identifiers are case-significant often enough that folding case
        would permit a substitution nobody wrote down. Being strict here fails
        in the direction the contract is for: an unrecognised substitution
        escalates to Master, which is recoverable, where a wrongly permitted one
        is an experiment that ran without authority.
        """
        for entry in self.allowed_substitutions:
            parts = substitution_parts(entry)
            if parts is None:
                continue  # construction refuses these; a bypass may not
            if parts == (given.strip(), instead.strip()):
                return True
        return False
