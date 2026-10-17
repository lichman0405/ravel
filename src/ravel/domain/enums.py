"""Closed vocabularies.

Every enum here is exhaustive for V0: `schemas/*.yaml` names the members and
the spec forbids inventing more. They are `StrEnum` so a value survives a
round-trip through JSON, a database row, and a model's tool result unchanged.
"""

from __future__ import annotations

from enum import StrEnum


class NodeType(StrEnum):
    """The Scientific DAG's node-type vocabulary. V0 adds no others.

    Six values, five of them plannable. `REVIEW` is the exception: a review is
    a checkpoint on a node that runs rather than a node of its own, so nothing
    may create one — see `PLANNABLE_NODE_TYPES` in `ravel.domain.state_machines`
    for the rule and for why the value is still here.
    """

    RESEARCH = "RESEARCH"
    HYPOTHESIS = "HYPOTHESIS"
    COMPUTATION = "COMPUTATION"
    EXPERIMENT = "EXPERIMENT"
    #: Not a kind of work. Readable so a node written before 2026-09-23 can
    #: still be read; never creatable.
    REVIEW = "REVIEW"
    DECISION = "DECISION"


class NodeStatus(StrEnum):
    """The eleven node states. Transitions are validated in `state_machines`."""

    PLANNED = "PLANNED"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    WAITING_DECISION = "WAITING_DECISION"
    REVIEWING = "REVIEWING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class JoinPolicy(StrEnum):
    """How a fan-in node decides it is unblocked."""

    ALL = "ALL"
    ANY = "ANY"
    THRESHOLD = "THRESHOLD"


class FailurePolicy(StrEnum):
    """What a join does when one of its dependencies fails."""

    BLOCK = "BLOCK"
    CONTINUE = "CONTINUE"
    MASTER_DECIDES = "MASTER_DECIDES"


class ProjectStatus(StrEnum):
    """The project lifecycle.

    The spec fixes the *event* that records a status change but does not
    enumerate the statuses, so this set is an implementation decision. It is
    deliberately coarse: a finer one would duplicate the DAG's own state and
    the two would drift.

    `INCONCLUSIVE` is the fourth ending `acceptance/V0_ACCEPTANCE.md` A20 asks
    for, beside success, failure, and termination, and it is a real one: a
    project whose budget ran out, or whose evidence never became sufficient,
    stopped without succeeding and without failing. Recording that as
    `COMPLETED` would say the question was answered, and recording it as
    `FAILED` would say the answer was no. The `ProjectSuccessContract`'s
    `unresolved_uncertainty_policy` is what the decision is measured against.

    The four endings A20 names map onto these as:
    SUCCESS -> `COMPLETED`, FAILED -> `FAILED`, INCONCLUSIVE -> `INCONCLUSIVE`,
    TERMINATED -> `CANCELLED`.
    """

    CREATED = "CREATED"
    CONTRACT_DEFINED = "CONTRACT_DEFINED"
    EXECUTING = "EXECUTING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    CANCELLED = "CANCELLED"


class ProjectOutcome(StrEnum):
    """How a project ended, in the words `acceptance/V0_ACCEPTANCE.md` A20 uses.

    The same four facts as the terminal `ProjectStatus` values, under the names
    the acceptance criterion is written in — and the mapping is a property
    rather than a convention, because a reader checking A20 against the code
    should find the four words in one place instead of translating between two
    vocabularies.

    It exists as its own enum for the reason every closed vocabulary here does:
    the outcome is a decision Master records, and a decision that named its
    ending as a project status would be recording a database field rather than
    a scientific conclusion. `TERMINATED` and `FAILED` are the pair most worth
    keeping apart — one says the work was stopped, the other says the answer
    was no — and a status enum that spells them `CANCELLED` and `FAILED` makes
    that easy to conflate.
    """

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    TERMINATED = "TERMINATED"

    @property
    def status(self) -> ProjectStatus:
        """The project status this ending is recorded as."""
        return {
            ProjectOutcome.SUCCESS: ProjectStatus.COMPLETED,
            ProjectOutcome.FAILED: ProjectStatus.FAILED,
            ProjectOutcome.INCONCLUSIVE: ProjectStatus.INCONCLUSIVE,
            ProjectOutcome.TERMINATED: ProjectStatus.CANCELLED,
        }[self]

    @property
    def is_termination(self) -> bool:
        """Whether this ending stops work rather than concluding it.

        The one ending a project may reach with nodes still unfinished. The
        others are statements about results, and a result that does not exist
        yet is not one — so `conclude` refuses them while work is in flight and
        cancels the work for this one.
        """
        return self is ProjectOutcome.TERMINATED


class EvidenceSourceTier(StrEnum):
    """Source quality tiers, A (strongest) to D (weakest)."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"


class AccessStatus(StrEnum):
    """How a source was reached. `OK` is the only status that yields content.

    A restricted source is recorded as restricted. Guessing its contents from
    an abstract or from model memory would put a fabricated claim in the
    Evidence Ledger.
    """

    OK = "OK"
    PAYWALLED = "PAYWALLED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ACCESS_LIMITED = "ACCESS_LIMITED"
    POLICY_BLOCKED = "POLICY_BLOCKED"


class ClaimClass(StrEnum):
    """Whether a statement is observed, derived, or proposed."""

    FACT = "FACT"
    INFERENCE = "INFERENCE"
    HYPOTHESIS = "HYPOTHESIS"


class Sufficiency(StrEnum):
    """Whether the evidence gathered can carry a decision."""

    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    INSUFFICIENT = "INSUFFICIENT"


class Confidence(StrEnum):
    """A coarse confidence band. Deliberately not a number."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ReviewCheckpoint(StrEnum):
    """When in a node's life a review happens."""

    PRE_RUN = "PRE_RUN"
    RUNTIME = "RUNTIME"
    FINAL = "FINAL"


class ReviewOutcome(StrEnum):
    """Review's verdict on a node."""

    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"


class CriterionProvenance(StrEnum):
    """Where an acceptance criterion came from.

    A criterion with no provenance cannot be presented as an objective
    benchmark. `PROVISIONAL` is the honest label for "RAVEL made this up to
    have something to measure", and it is recorded rather than hidden.
    """

    USER_REQUIREMENT = "user_requirement"
    LITERATURE_DERIVED = "literature_derived"
    STANDARD = "standard"
    AUTHORITATIVE_DATABASE = "authoritative_database"
    PRIOR_PROJECT_RESULT = "prior_project_result"
    RESEARCH_INFERENCE = "research_inference"
    PROVISIONAL = "provisional"


class DecisionType(StrEnum):
    """The kinds of material change Master records a decision for."""

    CREATE_NODE = "CREATE_NODE"
    CANCEL_NODE = "CANCEL_NODE"
    REPLACE_NODE = "REPLACE_NODE"
    REVISE_EXECUTION_CONTRACT = "REVISE_EXECUTION_CONTRACT"
    REVISE_ACCEPTANCE_CRITERIA = "REVISE_ACCEPTANCE_CRITERIA"
    CHANGE_ROUTE = "CHANGE_ROUTE"
    RESOLVE_DEVIATION = "RESOLVE_DEVIATION"
    ACCEPT_RESULT = "ACCEPT_RESULT"
    REJECT_RESULT = "REJECT_RESULT"
    CONCLUDE_INCONCLUSIVE = "CONCLUDE_INCONCLUSIVE"
    TERMINATE_PROJECT = "TERMINATE_PROJECT"
    RESOLVE_APPROVAL = "RESOLVE_APPROVAL"


class CompletionStatus(StrEnum):
    """Whether a research task satisfied its completion contract."""

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class CompletenessVerdict(StrEnum):
    """Delivery completeness, which is not the same as scientific acceptance."""

    COMPLETE = "COMPLETE"
    INCOMPLETE_DELIVERY = "INCOMPLETE_DELIVERY"


class TerminationStatus(StrEnum):
    """How an execution ended."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    DEVIATION = "DEVIATION"


class JobState(StrEnum):
    """Where a job handed to a backend has got to, in RAVEL's vocabulary.

    Two backend contracts describe the same journey in different words. The
    compute contract says `SUBMITTED, RUNNING, COMPLETED, FAILED, CANCELLED,
    TIMED_OUT`; the experiment contract says `READY, ACTIVE, WAITING,
    RESPONSE_AVAILABLE, COMPLETED, CANCELLED`. They are not the same
    vocabulary, and merging them would lose what the two contracts were written
    to distinguish — a lab task that is `WAITING` is not a compute job that is
    `RUNNING`, and a result that is `RESPONSE_AVAILABLE` is not one that has
    been collected.

    So each backend maps its own states onto these, and the word the backend
    itself used is kept beside it in `BackendJob.backend_state`. What this
    vocabulary buys is a single question the durable layer can ask — "has it
    ended, and how?" — without knowing which backend answered.
    """

    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        """Whether the job has ended and nothing further will be reported."""
        return self in TERMINAL_JOB_STATES


#: The job states from which nothing follows.
TERMINAL_JOB_STATES: frozenset[JobState] = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.TIMED_OUT}
)


class FailureClass(StrEnum):
    """Why a job failed, from the closed list `acceptance/MOCK_SCENARIOS.yaml` fixes.

    This is the distinction that decides whether the work is tried again, and
    it is a fact about the machinery rather than about the science.

    - `INFRA_RETRYABLE` — the machinery failed: a node went down, a queue
      backed up, a scheduler lost the job. The result was never produced, so
      nothing has been learned and re-running is not repeating an experiment.
    - `NON_RETRYABLE` — the work itself failed. Retrying would produce the same
      failure a second time, and the honest next step is a decision.

    There is deliberately no third value for "unknown". A backend that cannot
    classify its failure reports none, and RAVEL treats that as
    `NON_RETRYABLE` — a failure nobody can explain is not one to repeat on the
    chance that it goes away.
    """

    INFRA_RETRYABLE = "INFRA_RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"


class WorkerMessageKind(StrEnum):
    """The only four things a Worker may say to a lab."""

    CONFIRM = "CONFIRM"
    INFORM = "INFORM"
    REQUEST_MISSING_INFORMATION = "REQUEST_MISSING_INFORMATION"
    ESCALATE = "ESCALATE"


class UserRole(StrEnum):
    """The three V0 user roles. No complex RBAC."""

    PROJECT_OWNER = "PROJECT_OWNER"
    LAB_USER = "LAB_USER"
    ADMIN = "ADMIN"


class ApprovalStatus(StrEnum):
    """The state of a request for human authority."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class RetryEpisodeStatus(StrEnum):
    """Where one bounded-retry episode stands.

    This is *automation* state, not scientific state: it says what the runtime
    will do about an unanswered question, and it deliberately does not appear
    on `ProjectStatus`. A project whose runtime has stalled is still the status
    it was — a stall is a fact about the machinery, not an ending, and writing
    it as `FAILED` or `SUCCESS` would be a scientific claim nobody made.

    `ACTIVE` is "within budget, may ask now"; `COOLDOWN` is "within budget, may
    ask after `cooldown_until`"; `AWAITING_OPERATOR` is "budget spent, and only
    a person or a relevant scientific change reopens it"; `RETIRED` is history
    — the question ended, the state moved on, or an operator asked again.
    """

    ACTIVE = "ACTIVE"
    COOLDOWN = "COOLDOWN"
    AWAITING_OPERATOR = "AWAITING_OPERATOR"
    RETIRED = "RETIRED"


class RetryQuestion(StrEnum):
    """The four questions a `ProjectLoop` puts to Master and Review.

    The values are the prefixes of the loop's question keys, so the persisted
    row says which power was being asked without the retry table having to
    import the loop. Execution-seat turns are deliberately absent: a seat is
    given one turn per `(node, status)` episode, and a long HPC or human-lab
    wait is not a model question that can go unanswered.
    """

    DECISION = "decision"
    PRE_FLIGHT = "pre-flight"
    VERDICT = "verdict"
    ENDING = "ending"


class ActionKind(StrEnum):
    """The only four side-effecting operations an Action may stand for.

    Every external effect RAVEL can cause is one of these, whether it is a
    Slurm submission, a mail send, a human-lab handover, or a sandbox command.
    Keeping the vocabulary closed is what lets approval, idempotency and
    budget policy be one implementation rather than four that resemble each
    other.
    """

    COMPUTE_SUBMIT = "COMPUTE_SUBMIT"
    MAIL_SEND = "MAIL_SEND"
    LAB_HANDOVER = "LAB_HANDOVER"
    SANDBOX_EXEC = "SANDBOX_EXEC"


class ActionStatus(StrEnum):
    """Where an Action stands.

    `ACCEPTED` means only that the external system accepted the work. It is
    not scientific success and, for mail, not delivery to a human. `UNKNOWN`
    is a real state rather than a zero or an empty success: the external
    outcome is not known and must be reconciled. Mail projects the same fact
    as `UNKNOWN_DELIVERY` on the wire, which is a projection and not a second
    internal state.
    """

    DRAFT = "DRAFT"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    READY = "READY"
    CLAIMED = "CLAIMED"
    ACCEPTED = "ACCEPTED"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class BudgetReservationStatus(StrEnum):
    """The life of one budget reservation.

    The names are the ones `schemas/premium/budget_reservation.schema.json`
    publishes. `RESERVED` is money or resource-hours promised to an approved
    Action and not yet spent. `UNKNOWN` is the same promise when the external
    system did not tell us the actual cost: the reservation stays held until a
    person or a reliable provider reconciles it, which is why it is *not* a
    synonym for zero. `SETTLED` has a known actual amount; `RELEASED` is a
    known cancellation or confirmed non-execution.
    """

    RESERVED = "RESERVED"
    UNKNOWN = "UNKNOWN"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class ActionAttemptOutcome(StrEnum):
    """How one claim of an Action ended, in the Action schema's vocabulary."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    UNKNOWN = "UNKNOWN"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"


class ActionEvent(StrEnum):
    """Internal audit labels for an Action's transitions.

    These are not `ProjectEventType` values: the public event vocabulary is
    closed and versioned, and adding Action values to it would change a
    contract this task does not own. Action history is nonetheless
    reconstructible from the immutable Action row, its attempts and its budget
    reservation, which is the state the task's acceptance tests read.
    """

    CREATED = "CREATED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_RESOLVED = "APPROVAL_RESOLVED"
    CLAIMED = "CLAIMED"
    ACCEPTED = "ACCEPTED"
    UNKNOWN = "UNKNOWN"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    COMPLETED = "COMPLETED"
    RELEASED = "RELEASED"
    OVERRUN = "OVERRUN"

