"""Actions, human approvals bound to them, and resource budgets.

An Action is the only record through which RAVEL may cause an effect outside
its own database. That is why this module is deliberately narrow: it models
the *proposal* to act, the exact content a human approved, and the budget
promise made before the effect is attempted. It does not model a scheduler, a
mail client, or a lab protocol; each of those lives behind an Action kind and
is reached only after the checks here have passed.

Three rules are represented in types rather than left to callers:

- **An approval is bound to content.** `hash_action_content` hashes the
  payload, the target, the contract version, the resource request, the cost
  estimate, the envelope version, the actor and the expiry together. Changing
  any of them changes the hash and therefore invalidates the old approval.
- **Money is a string, not a float.** CNY is a fixed-point decimal string in
  every record, and the parser rejects non-finite values and excess scale
  rather than rounding them. A float never enters a budget comparison.
- **UNKNOWN is not zero.** A reservation whose cost is not known says so:
  `cost_known` is false, the estimated amount may be absent, and the
  reservation keeps consuming the resource-hour budget until it is reconciled.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import Field, field_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.enums import (
    ActionAttemptOutcome,
    ActionKind,
    ActionStatus,
    BudgetReservationStatus,
)
from ravel.domain.ids import new_id

#: The default life of an approval, and of the Action that carries it.
APPROVAL_TTL = timedelta(hours=24)

#: CNY values are fixed point at six decimal places. Six is enough to price a
#: fraction of a CPU-second or a single API call, and it is a *scale* rather
#: than a rounding rule: a value with a seventh decimal is refused instead of
#: silently losing a digit of the amount somebody approved.
CNY_SCALE = 6
CNY_QUANTUM = Decimal(1).scaleb(-CNY_SCALE)
_CNY_MAX_DIGITS = 18

_DECIMAL_TEXT = re.compile(r"^[+]?(?:\d+(?:\.\d*)?|\.\d+)$")

#: The resource ceilings the task fixes for V0 test work. They are defaults,
#: and `effective_limits` intersects them with a stricter envelope: a default
#: may narrow what an envelope permits, never widen it.
DEFAULT_MAX_CPU_HOURS_PER_JOB = Decimal("1")
DEFAULT_MAX_GPU_HOURS_PER_JOB = Decimal("0")
DEFAULT_MAX_WALL_CLOCK_HOURS_PER_JOB = Decimal("24")
DEFAULT_COMPUTE_JOBS_PER_PROJECT = 2
DEFAULT_COMPUTE_JOBS_DEPLOYMENT_WIDE = 8


class ActionError(ValueError):
    """The base class for a refusal about an Action."""

    code = "ACTION_REFUSED"


class ApprovalRequired(ActionError):
    """The Action needs a human approval that has not been given, or is stale."""

    code = "APPROVAL_REQUIRED"


class StaleApproval(ApprovalRequired):
    """The approved content, envelope or expiry no longer matches the Action."""

    code = "STALE_VERSION"


class BudgetExceeded(ActionError):
    """The Action cannot be promised budget, or its account is paused."""

    code = "BUDGET_EXCEEDED"


class ActionConflict(ActionError):
    """The requested state change lost a race or is illegal for this state."""

    code = "STALE_VERSION"


class UnknownExternalResult(ActionError):
    """Execution stopped without a known external outcome; reconcile first."""

    code = "UNKNOWN_EXTERNAL_RESULT"


class UnsupportedAction(ActionError):
    """The requested operation or resource is outside the V0 contract."""

    code = "UNSUPPORTED"


def parse_cny(value: str | Decimal | int | None, *, field: str = "CNY amount") -> Decimal | None:
    """A fixed-point decimal amount, or `None` for an absent one.

    `float` is refused on purpose: a binary float cannot represent 0.10 exactly,
    and accepting one would put a number that is almost the approved amount into
    a budget ledger. Every caller that has a float converts it to a string at
    the boundary where it was read, so the refusal is also a reminder that a
    float was the wrong type to begin with.

    Raises:
        BudgetExceeded: The value is not a finite, non-negative decimal with at
            most six fractional digits.
    """
    if value is None:
        return None
    if isinstance(value, float):
        raise BudgetExceeded(
            f"{field} must be a fixed-point decimal string, not a float; a float "
            "cannot represent a monetary amount exactly"
        )
    text = str(value).strip()
    if not _DECIMAL_TEXT.match(text):
        raise BudgetExceeded(
            f"{field} {value!r} is not a fixed-point non-negative decimal string"
        )
    try:
        amount = Decimal(text)
    except InvalidOperation as error:  # pragma: no cover - regex rejects these
        raise BudgetExceeded(f"{field} {value!r} is not a decimal") from error
    if not amount.is_finite():
        raise BudgetExceeded(f"{field} must be finite")
    if amount < 0:
        raise BudgetExceeded(f"{field} must not be negative")
    if amount.adjusted() >= _CNY_MAX_DIGITS:
        raise BudgetExceeded(f"{field} has too many significant digits")
    if amount != amount.quantize(CNY_QUANTUM):
        raise BudgetExceeded(
            f"{field} {text!r} has more than {CNY_SCALE} decimal places; a "
            "budget amount is never silently rounded"
        )
    return amount


def format_cny(value: Decimal | str | int | None) -> str | None:
    """The canonical spelling of an amount: fixed point, no exponent, no zeros.

    `Decimal("10.00")` renders as `"10"` and `Decimal("0.125")` as `"0.125"`,
    so two callers that computed the same amount produce the same string and a
    ledger comparison is a string comparison after parsing rather than a test
    that depends on how the number was typed.
    """
    amount = parse_cny(value)
    if amount is None:
        return None
    if amount == 0:
        return "0"
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def cny(value: str | Decimal | int | None, *, field: str = "CNY amount") -> str | None:
    """Validate and canonicalise a CNY amount in one step."""
    return format_cny(parse_cny(value, field=field))


def _decimal(value: Decimal | str | int, *, field: str) -> Decimal:
    """A non-negative resource quantity with the same fixed-point discipline."""
    amount = parse_cny(value, field=field)
    assert amount is not None
    return amount


class ResourceRequest(Record):
    """The physical resources one Action asks a cluster to spend.

    The three quantities are upper bounds, not forecasts: a job may finish
    earlier, but a scheduler is asked for at most this much. Wall clock is
    kept separate from CPU because waiting for a lab or a queue is time a
    person experiences, not time a processor burns.
    """

    cpu_hours: Decimal = Field(default=Decimal("0"))
    gpu_hours: Decimal = Field(default=Decimal("0"))
    wall_clock_hours: Decimal = Field(default=Decimal("0"))

    @field_validator("cpu_hours", "gpu_hours", "wall_clock_hours", mode="before")
    @classmethod
    def _quantity(cls, value: object, info: Any) -> Decimal:
        if value is None:
            return Decimal("0")
        return _decimal(value, field=info.field_name or "resource quantity")  # type: ignore[arg-type]

    def within(self, limits: ResourceLimits) -> bool:
        """Whether every quantity is within an effective limit."""
        return (
            self.cpu_hours <= limits.cpu_hours
            and self.gpu_hours <= limits.gpu_hours
            and self.wall_clock_hours <= limits.wall_clock_hours
        )

    def describe_violation(self, limits: ResourceLimits) -> str | None:
        """A sentence naming the first limit exceeded, or `None` if none is."""
        checks = (
            ("cpu_hours", self.cpu_hours, limits.cpu_hours),
            ("gpu_hours", self.gpu_hours, limits.gpu_hours),
            ("wall_clock_hours", self.wall_clock_hours, limits.wall_clock_hours),
        )
        for name, value, limit in checks:
            if value > limit:
                return f"{name}={value} exceeds the effective limit {limit}"
        return None


class ResourceLimits(Record):
    """The strictest limits that apply to one Action.

    Built by `effective_resource_limits`, never by taking the defaults alone:
    a configuration default is a ceiling for the deployment, and an
    AuthorityEnvelope may be stricter for a project. The narrower of the two
    is what an Action is checked against, which is the rule that lets an owner
    tighten a limit without editing every call site.
    """

    cpu_hours: Decimal = DEFAULT_MAX_CPU_HOURS_PER_JOB
    gpu_hours: Decimal = DEFAULT_MAX_GPU_HOURS_PER_JOB
    wall_clock_hours: Decimal = DEFAULT_MAX_WALL_CLOCK_HOURS_PER_JOB

    @field_validator("cpu_hours", "gpu_hours", "wall_clock_hours", mode="before")
    @classmethod
    def _quantity(cls, value: object, info: Any) -> Decimal:
        if value is None:
            return Decimal("0")
        return _decimal(value, field=info.field_name or "resource limit")  # type: ignore[arg-type]


def effective_resource_limits(
    envelope_limits: Any | None = None,
    *,
    cpu_hours: Decimal | str | int | None = None,
    gpu_hours: Decimal | str | int | None = None,
    wall_clock_hours: Decimal | str | int | None = None,
) -> ResourceLimits:
    """The tightest of the configured defaults and an envelope's limits.

    `envelope_limits` is the `BudgetLimits` carried by the project's current
    AuthorityEnvelope. Only the two fields that have the same meaning there
    are read: `max_compute_hours` bounds CPU hours, `max_wall_clock_hours`
    bounds elapsed time. A missing field means the envelope says nothing, so
    the deployment default applies.
    """
    limits = ResourceLimits(
        cpu_hours=(
            _decimal(cpu_hours, field="cpu_hours")
            if cpu_hours is not None
            else DEFAULT_MAX_CPU_HOURS_PER_JOB
        ),
        gpu_hours=(
            _decimal(gpu_hours, field="gpu_hours")
            if gpu_hours is not None
            else DEFAULT_MAX_GPU_HOURS_PER_JOB
        ),
        wall_clock_hours=(
            _decimal(wall_clock_hours, field="wall_clock_hours")
            if wall_clock_hours is not None
            else DEFAULT_MAX_WALL_CLOCK_HOURS_PER_JOB
        ),
    )
    if envelope_limits is None:
        return limits
    updates: dict[str, Decimal] = {}
    if envelope_limits.max_compute_hours is not None:
        updates["cpu_hours"] = min(
            limits.cpu_hours, Decimal(str(envelope_limits.max_compute_hours))
        )
    if envelope_limits.max_wall_clock_hours is not None:
        updates["wall_clock_hours"] = min(
            limits.wall_clock_hours, Decimal(str(envelope_limits.max_wall_clock_hours))
        )
    return limits.model_copy(update=updates)


class ActionAttempt(Record):
    """One claim of an Action and what came of it.

    `PENDING` is the only non-final value. A row that stays `PENDING` after the
    claiming process died is precisely the uncertainty the `UNKNOWN` Action
    status exists for; it is never read as "no attempt happened".
    """

    attempt_id: str = Field(default_factory=new_id)
    claimed_at: datetime = Field(default_factory=utcnow)
    outcome: ActionAttemptOutcome = ActionAttemptOutcome.PENDING
    external_ref: str | None = None
    note: str | None = None


class Action(Record):
    """A proposed externally-visible side effect, and what became of it.

    The fields named in `CONTRACTS.md` section 3 are all present under the same
    names. The additional fields are operational: `cluster` and the resource
    request are the budget's keys, `node_id` and `contract_version` are what
    make an HPC submission key reproducible, and `idempotency_key` is the
    unique key that turns a retried creation into the original Action.
    """

    action_id: str = Field(default_factory=new_id)
    project_id: str
    kind: ActionKind
    payload_hash: str
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_artifact_ref: str | None = None
    contract_id: str | None = None
    contract_version: int | None = Field(default=None, ge=1)
    node_id: str | None = None
    actor: str = Field(min_length=1)
    requesting_binding: str = Field(min_length=1)
    target_ref: str | None = None
    required_approval_ref: str | None = None
    envelope_version: int = Field(ge=1)
    budget_reservation_ref: str | None = None
    expires_at: datetime
    status: ActionStatus = ActionStatus.DRAFT
    created_at: datetime = Field(default_factory=utcnow)
    attempts: tuple[ActionAttempt, ...] = ()
    cluster: str = Field(default="default", min_length=1)
    prior_action_id: str | None = None
    resource_request: ResourceRequest = Field(default_factory=ResourceRequest)
    estimated_cny: str | None = None
    cost_known: bool = True
    idempotency_key: str = Field(min_length=1)
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    accepted_at: datetime | None = None
    completed_at: datetime | None = None
    external_ref: str | None = None
    external_state: str | None = None
    failure_reason: str | None = None
    uncertainty: str | None = None
    overrun_event_ref: str | None = None

    @field_validator("payload_hash")
    @classmethod
    def _hash_shape(cls, value: str) -> str:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("payload_hash must be 'sha256:' followed by 64 lowercase hex")
        return value

    @field_validator("estimated_cny", mode="before")
    @classmethod
    def _money(cls, value: object) -> str | None:
        return cny(value)  # type: ignore[arg-type]

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition is possible."""
        return self.status in _TERMINAL

    @property
    def projected_status(self) -> str:
        """The status the API shows.

        The internal state for an uncertain mail send is `UNKNOWN`; the
        contract projects that as `UNKNOWN_DELIVERY` so a client cannot read
        it as "sent" or as a generic failure. Every other kind keeps the
        internal spelling.
        """
        if self.kind is ActionKind.MAIL_SEND and self.status is ActionStatus.UNKNOWN:
            return "UNKNOWN_DELIVERY"
        return self.status.value

    def is_expired(self, at: datetime | None = None) -> bool:
        """Whether the Action's approval window has closed."""
        moment = at or utcnow()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return moment >= expires

    def content_hash(self) -> str:
        """Recompute the hash of exactly the content a human approved."""
        return hash_action_content(
            kind=self.kind,
            actor=self.actor,
            payload=self.payload,
            payload_artifact_ref=self.payload_artifact_ref,
            target_ref=self.target_ref,
            contract_id=self.contract_id,
            contract_version=self.contract_version,
            node_id=self.node_id,
            resource_request=self.resource_request,
            estimated_cny=self.estimated_cny,
            cost_known=self.cost_known,
            envelope_version=self.envelope_version,
            expires_at=self.expires_at,
        )

    def has_intact_payload(self) -> bool:
        """Whether the stored hash still describes the stored content."""
        return self.content_hash() == self.payload_hash

    def public_view(self) -> dict[str, Any]:
        """The Action projection from `schemas/premium/action.schema.json`.

        Deliberately not `model_dump`: the schema is a contract with clients,
        and `additionalProperties: false` means an internal field leaking into
        the projection is a broken client rather than a harmless extra.
        """
        return {
            "action_id": self.action_id,
            "project_id": self.project_id,
            "kind": self.kind.value,
            "payload_hash": self.payload_hash,
            "payload_artifact_ref": self.payload_artifact_ref,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "requesting_binding": self.requesting_binding,
            "target_ref": self.target_ref,
            "required_approval_ref": self.required_approval_ref,
            "envelope_version": self.envelope_version,
            "budget_reservation_ref": self.budget_reservation_ref,
            "expires_at": self.expires_at,
            "status": self.projected_status,
            "created_at": self.created_at,
            "attempts": [
                {
                    "attempt_id": attempt.attempt_id,
                    "claimed_at": attempt.claimed_at,
                    "outcome": attempt.outcome.value,
                    "external_ref": attempt.external_ref,
                    "note": attempt.note,
                }
                for attempt in self.attempts
            ],
            "prior_action_id": self.prior_action_id,
        }


_TERMINAL = frozenset(
    {
        ActionStatus.COMPLETED,
        ActionStatus.FAILED_CONFIRMED,
        ActionStatus.REJECTED,
        ActionStatus.EXPIRED,
        ActionStatus.CANCELLED,
    }
)


class BudgetAccount(Record):
    """The paid budget one project has explicitly configured for one cluster.

    Absence of a row is not "unlimited": it is a paid limit of zero. The task
    requires the Owner to configure spending, so the default lives in the
    resolution function rather than in a row that could look like consent.
    A row can also be paused by an overrun, and a paused account refuses new
    reservations until an operator resolves it.
    """

    account_id: str = Field(default_factory=new_id)
    project_id: str
    cluster: str = Field(default="default", min_length=1)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    limit_cny: str = "0"
    configured_by: str = Field(min_length=1)
    configured_at: datetime = Field(default_factory=utcnow)
    created_at: datetime = Field(default_factory=utcnow)
    paused_at: datetime | None = None
    pause_reason: str | None = None
    overrun_at: datetime | None = None

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        if value != "CNY":
            raise ValueError("V0 budgets are denominated in CNY")
        return value

    @field_validator("limit_cny", mode="before")
    @classmethod
    def _limit(cls, value: object) -> str:
        formatted = cny(value)  # type: ignore[arg-type]
        assert formatted is not None
        return formatted

    @property
    def is_paused(self) -> bool:
        """Whether new submissions are refused by an overrun or operator pause."""
        return self.paused_at is not None

    @property
    def limit(self) -> Decimal:
        """The configured limit as a decimal."""
        return Decimal(self.limit_cny)


class BudgetReservation(Record):
    """The budget promise made before an external submission.

    The field names follow `schemas/premium/budget_reservation.schema.json` so
    the row, the API projection and the domain object cannot disagree about
    what a reservation says. `status` is `RESERVED` before the effect and stays
    `RESERVED` or `UNKNOWN` until the effect's cost is known — an unknown
    result never becomes a zero-cost settlement.
    """

    reservation_id: str = Field(default_factory=new_id)
    project_id: str
    action_id: str
    cluster: str = Field(default="default", min_length=1)
    status: BudgetReservationStatus = BudgetReservationStatus.RESERVED
    cost_known: bool = True
    estimated_cost_cny: str | None = None
    cost_upper_bound_cny: str | None = None
    actual_cost_cny: str | None = None
    resource_hours: str | None = None
    resource_hours_upper_bound: str | None = None
    overrun_event_ref: str | None = None
    reserved_at: datetime = Field(default_factory=utcnow)
    settled_at: datetime | None = None
    released_at: datetime | None = None

    @field_validator(
        "estimated_cost_cny",
        "cost_upper_bound_cny",
        "actual_cost_cny",
        "resource_hours",
        "resource_hours_upper_bound",
        mode="before",
    )
    @classmethod
    def _amounts(cls, value: object) -> str | None:
        return cny(value)  # type: ignore[arg-type]

    @property
    def holds_budget(self) -> bool:
        """Whether this reservation still consumes the account."""
        return self.status in (
            BudgetReservationStatus.RESERVED,
            BudgetReservationStatus.UNKNOWN,
        )

    @property
    def committed_cny(self) -> Decimal:
        """What this reservation currently counts against the account.

        A settled reservation counts its *actual* cost, never its estimate:
        recording an overrun is the point of keeping both numbers. An open
        reservation counts its estimate, or zero when the site could not price
        the work — the resource-hour limits are then the hard bound, which is
        why `cost_known` is a separate field rather than an estimate of zero.
        """
        if self.status is BudgetReservationStatus.SETTLED:
            return Decimal(self.actual_cost_cny or "0")
        if self.holds_budget:
            return Decimal(self.estimated_cost_cny or "0")
        return Decimal("0")


def hash_action_content(
    *,
    kind: ActionKind,
    actor: str,
    payload: dict[str, Any],
    payload_artifact_ref: str | None,
    target_ref: str | None,
    contract_id: str | None,
    contract_version: int | None,
    node_id: str | None,
    resource_request: ResourceRequest,
    estimated_cny: str | None,
    cost_known: bool,
    envelope_version: int,
    expires_at: datetime,
) -> str:
    """A SHA-256 over every fact an approval covers.

    The output is the `sha256:<hex>` spelling the schemas require. JSON is
    canonicalised (sorted keys, no insignificant whitespace, UTF-8) so that two
    processes that construct the same content produce the same hash, and a
    change anywhere in the content — recipient, attachment, contract version,
    resource request, cost estimate, envelope version, actor or expiry —
    produces a different one. That is what makes a stale approval detectable
    rather than merely discouraged.

    `payload` is hashed as given: it is the exact body the human is approving,
    not a normalised summary. Float values are accepted because headers and
    parameters are arbitrary JSON, but money in it should already be a string.
    """
    body = {
        "kind": kind.value,
        "actor": actor,
        "payload": payload,
        "payload_artifact_ref": payload_artifact_ref,
        "target_ref": target_ref,
        "contract_id": contract_id,
        "contract_version": contract_version,
        "node_id": node_id,
        "resource_request": {
            "cpu_hours": format(Decimal(str(resource_request.cpu_hours)).normalize(), "f"),
            "gpu_hours": format(Decimal(str(resource_request.gpu_hours)).normalize(), "f"),
            "wall_clock_hours": format(
                Decimal(str(resource_request.wall_clock_hours)).normalize(), "f"
            ),
        },
        "estimated_cny": estimated_cny,
        "cost_known": cost_known,
        "envelope_version": envelope_version,
        "expires_at": _canonical_instant(expires_at),
    }
    canonical = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canonical_instant(moment: datetime) -> str:
    """A UTC instant spelled the same way on every round trip."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def approval_snapshot(action: Action) -> dict[str, Any]:
    """The facts an `ApprovalRequest.payload` must carry for one Action.

    This is the bridge between the existing approval table and the Action it
    authorizes. The approval row keeps its historical shape (a question with a
    payload); everything the pre-execution check compares is written here at
    creation time, so a stale envelope, hash, contract, recipient or budget
    bound is visible without re-deriving it from a prompt.
    """
    recipients = {
        key: list(action.payload.get(key, []) or [])
        for key in ("to", "cc", "bcc")
    }
    attachments = action.payload.get("attachment_hashes", []) or []
    return {
        "action_id": action.action_id,
        "payload_hash": action.payload_hash,
        "contract_id": action.contract_id,
        "contract_version": action.contract_version,
        "target_ref": action.target_ref,
        "recipient": action.target_ref,
        "to": recipients["to"],
        "cc": recipients["cc"],
        "bcc": recipients["bcc"],
        "attachment_hashes": list(attachments),
        "cost_upper_bound_cny": action.estimated_cny,
        "envelope_version": action.envelope_version,
        "expires_at": _canonical_instant(action.expires_at),
        "principal_user_id": None,
    }


def verify_approval_snapshot(action: Action, snapshot: dict[str, Any]) -> None:
    """Refuse an approval whose stored snapshot no longer matches the Action.

    Called before every execution, after the approval is re-read. Each branch
    names the field that changed, because "approval is stale" is not actionable
    and "the recipient list changed" is.

    Raises:
        StaleApproval: The snapshot describes different content, a different
            envelope version, a different expiry, or a different cost bound.
    """
    if snapshot.get("action_id") != action.action_id:
        raise StaleApproval(
            f"approval names action {snapshot.get('action_id')!r}, not {action.action_id!r}"
        )
    if snapshot.get("payload_hash") != action.payload_hash:
        raise StaleApproval(
            "the approved payload hash does not match this Action; content "
            "changed after approval and a new Action is required"
        )
    if action.has_intact_payload() is False:
        raise StaleApproval(
            "the stored payload no longer hashes to its recorded payload_hash"
        )
    # The hash binds the payload, but the approval record names the recipient
    # set and attachment hashes explicitly because a human approved *those*.
    # Comparing them here as well means a stored approval whose envelope was
    # rewritten to a different recipient list is refused even if the Action's
    # own payload hash still matches; the approval is checked on both sides of
    # the binding rather than only on the Action's.
    for field in ("to", "cc", "bcc"):
        stored = list(snapshot.get(field, []) or [])
        actual = list(action.payload.get(field, []) or [])
        if stored != actual:
            raise StaleApproval(
                f"the approved {field} recipient list changed after approval"
            )
    approved_attachments = list(snapshot.get("attachment_hashes", []) or [])
    actual_attachments = list(action.payload.get("attachment_hashes", []) or [])
    if approved_attachments != actual_attachments:
        raise StaleApproval(
            "the approved attachment set changed after approval; the human did "
            "not approve these bytes"
        )
    for key in ("contract_id", "contract_version", "target_ref"):
        if snapshot.get(key) != getattr(action, key):
            raise StaleApproval(
                f"{key} changed after approval; the old approval does not cover it"
            )
    if snapshot.get("envelope_version") != action.envelope_version:
        raise StaleApproval("the AuthorityEnvelope version changed after approval")
    if snapshot.get("expires_at") != _canonical_instant(action.expires_at):
        raise StaleApproval("the approval expiry no longer matches the Action")
    if snapshot.get("cost_upper_bound_cny") != action.estimated_cny:
        raise StaleApproval("the approved cost bound no longer matches the budget")


def is_mail(action: Action) -> bool:
    """Whether this Action is a mail send, whose failure projection differs."""
    return action.kind is ActionKind.MAIL_SEND
