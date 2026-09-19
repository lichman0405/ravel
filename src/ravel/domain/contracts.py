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


def _range(span: str) -> tuple[float, float] | None:
    """The two ends of a range, or `None` if it is not written as one."""
    match = _RANGE.match(span)
    if match is None:
        return None
    return float(match.group("low")), float(match.group("high"))


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
    acceptance_contract_ref: str | None = None
    frozen_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_frozen(self) -> bool:
        """Whether this contract has been frozen for execution."""
        return self.frozen_at is not None

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
            match = _SUBSTITUTION.match(entry)
            if match is None:
                continue  # construction refuses these; a bypass may not
            if (
                match.group("given").strip() == given.strip()
                and match.group("instead").strip() == instead.strip()
            ):
                return True
        return False
