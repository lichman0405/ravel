"""Closed vocabularies.

Every enum here is exhaustive for V0: `schemas/*.yaml` names the members and
the spec forbids inventing more. They are `StrEnum` so a value survives a
round-trip through JSON, a database row, and a model's tool result unchanged.
"""

from __future__ import annotations

from enum import StrEnum


class NodeType(StrEnum):
    """The six Scientific DAG node types. V0 adds no others."""

    RESEARCH = "RESEARCH"
    HYPOTHESIS = "HYPOTHESIS"
    COMPUTATION = "COMPUTATION"
    EXPERIMENT = "EXPERIMENT"
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
    """

    CREATED = "CREATED"
    CONTRACT_DEFINED = "CONTRACT_DEFINED"
    EXECUTING = "EXECUTING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


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
