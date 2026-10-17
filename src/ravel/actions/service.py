"""The Action lifecycle: propose, approve, claim, report, reconcile.

This service does not perform external effects. It decides whether one may be
attempted, and records what happened when the caller reports back. The split is
what makes the pre-submission checks testable and keeps the checks in one place:
a scheduler, a mailer and a sandbox runner all claim through `claim`, and none
of them can reach the effect while an approval is stale, a membership is
revoked, the envelope has moved, or the budget is exhausted.

Two ordering rules are load-bearing:

- **Budget is reserved before an Action becomes `READY`.** A human's approval
  without a promise of money is not yet permission to spend it.
- **The claim is one row-locked compare-and-set.** A second claimant returns
  the original Action with `claimed=False` and must not submit anything. The
  external idempotency key is the second line of defence, not the first.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ravel.actions.idempotency import (
    attempt_number,
    compute_submission_key,
    normalise_payload,
)
from ravel.actions.repository import ActionRepository
from ravel.budgets.service import BudgetService
from ravel.config import Settings, get_settings
from ravel.domain.actions import (
    Action,
    ActionAttempt,
    ActionConflict,
    ActionError,
    ApprovalRequired,
    BudgetExceeded,
    ResourceRequest,
    StaleApproval,
    approval_snapshot,
    cny,
    effective_resource_limits,
    hash_action_content,
    verify_approval_snapshot,
)
from ravel.domain.clock import utcnow
from ravel.domain.contracts import AuthorityEnvelope, ExecutionContract
from ravel.domain.enums import (
    ActionAttemptOutcome,
    ActionKind,
    ActionStatus,
    ApprovalStatus,
    UserRole,
)
from ravel.domain.ids import new_id
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import can_transition_action
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.contracts import (
    AuthorityEnvelopeRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.identity import (
    ApprovalRepository,
    MembershipRepository,
    UserRepository,
)
from ravel.state.tables import ActionRow, UserRow

#: Agent roles are the non-user actors a tool can name. The set is closed for
#: the same reason the role list is: an Action attributed to an invented
#: identity has no membership rule to check and would be an audit hole.
_AGENT_ACTORS = frozenset(role.value for role in AgentRole)


class ActionService:
    """One project's Actions, from proposal to reconciled outcome."""

    def __init__(
        self,
        session: Session,
        project_id: str,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.project_id = project_id
        self.settings = settings or get_settings()
        self.actions = ActionRepository(session, project_id)
        self.approvals = ApprovalRepository(session, project_id)
        self.budgets = BudgetService(session, project_id, settings=self.settings)

    # ── Creation ────────────────────────────────────────────────────────────

    def create(
        self,
        *,
        kind: ActionKind | str,
        payload: dict[str, Any],
        actor: str,
        requesting_binding: str,
        target_ref: str | None = None,
        contract_id: str | None = None,
        contract_version: int | None = None,
        node_id: str | None = None,
        payload_artifact_ref: str | None = None,
        resource_request: ResourceRequest | None = None,
        estimated_cny: str | None = None,
        cost_known: bool = True,
        cluster: str = "default",
        prior_action_id: str | None = None,
        idempotency_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> Action:
        """Record a proposal and put it where its checks say it belongs.

        The Action starts in `DRAFT` in memory so the approval request and the
        Action can be written in one transaction: an approval that outlived a
        failed insert would be a question about nothing.

        A repeated idempotency key returns the original Action and its current
        state. That is not a convenience — it is the duplicate-submission rule.
        The content is deliberately *not* compared: the same HPC
        `submission_key` or mail `Message-ID` names one external submission,
        and a caller that reused it for different content must be told what was
        submitted, not handed a second attempt.

        Raises:
            PermissionError: The actor is neither a live project member nor a
                fixed agent role.
            ActionError: The kind-specific idempotency facts are missing, the
                execution contract is not the current frozen version, or the
                resource request exceeds the effective limits.
            BudgetExceeded: No budget can be promised for an Action that needs
                none approved by a human.
            ValueError: The payload cannot be the content of this kind.
        """
        chosen_kind = kind if isinstance(kind, ActionKind) else ActionKind(kind)
        self._require_actor(actor)
        if not requesting_binding:
            raise ActionError("an Action must name the binding that requested it")

        body = normalise_payload(chosen_kind, payload)
        key = self._idempotency_key(
            chosen_kind,
            body,
            explicit=idempotency_key,
            contract_version=contract_version,
            node_id=node_id,
        )
        if chosen_kind is ActionKind.COMPUTE_SUBMIT:
            body = self._compute_body(
                body,
                contract_version=contract_version,
                node_id=node_id,
            )

        # The lock is what turns "check then insert" into one decision. It is
        # transaction-scoped, and it is keyed by the external identity rather
        # than the Action id because two callers repeating one submission have
        # not agreed on an Action id yet.
        self._advisory_lock(f"ravel-action:{self.project_id}:{key}")
        existing = self.actions.find_by_idempotency_key_for_update(key)
        if existing is not None:
            action, _row = existing
            if action.kind is not chosen_kind:
                raise ActionConflict(
                    f"idempotency key {key!r} already names a {action.kind.value} "
                    f"Action, not {chosen_kind.value}"
                )
            return action

        now = utcnow()
        ttl = timedelta(hours=float(self.settings.action_approval_ttl_hours))
        requested_expiry = expires_at or (now + ttl)
        if requested_expiry.tzinfo is None:
            requested_expiry = requested_expiry.replace(tzinfo=now.tzinfo)
        expiry = min(requested_expiry, now + ttl)

        envelope_version, envelope = self.current_envelope()
        request = resource_request or ResourceRequest()
        self._check_resources(request, envelope)
        requested_contract = self._verify_contract(
            node_id=node_id,
            contract_id=contract_id,
            contract_version=contract_version,
            kind=chosen_kind,
        )

        estimate = cny(estimated_cny, field="estimated cost")
        if cost_known and estimate is None:
            # "Known at zero" is a claim the caller makes, not a default this
            # layer invents. A caller that does not know says cost_known=False.
            estimate = "0"
        if not cost_known:
            estimate = estimate  # an upper bound may still be recorded

        requirement = (
            envelope.requirement_for(chosen_kind.value) if envelope is not None else None
        )
        approval_ref = new_id() if requirement is not None else None

        action = Action(
            project_id=self.project_id,
            kind=chosen_kind,
            payload_hash="sha256:" + "0" * 64,  # replaced below, before insert
            payload=body,
            payload_artifact_ref=payload_artifact_ref,
            contract_id=requested_contract.contract_id if requested_contract else contract_id,
            contract_version=(
                requested_contract.version if requested_contract else contract_version
            ),
            node_id=node_id,
            actor=actor,
            requesting_binding=requesting_binding,
            target_ref=target_ref,
            required_approval_ref=approval_ref,
            envelope_version=envelope_version,
            expires_at=expiry,
            status=ActionStatus.DRAFT,
            cluster=cluster,
            prior_action_id=prior_action_id,
            resource_request=request,
            estimated_cny=estimate,
            cost_known=cost_known,
            idempotency_key=key,
        )
        action = action.model_copy(
            update={
                "payload_hash": hash_action_content(
                    kind=action.kind,
                    actor=action.actor,
                    payload=action.payload,
                    payload_artifact_ref=action.payload_artifact_ref,
                    target_ref=action.target_ref,
                    contract_id=action.contract_id,
                    contract_version=action.contract_version,
                    node_id=action.node_id,
                    resource_request=action.resource_request,
                    estimated_cny=action.estimated_cny,
                    cost_known=action.cost_known,
                    envelope_version=action.envelope_version,
                    expires_at=action.expires_at,
                )
            }
        )
        self.actions.add(action)
        self.session.flush()

        if requirement is not None:
            self.approvals.request_bound(
                approval_id=approval_ref,
                action_id=action.action_id,
                payload_hash=action.payload_hash,
                requested_by=actor,
                action=chosen_kind.value,
                rationale=(
                    requirement.rationale
                    or f"the AuthorityEnvelope requires {requirement.required_role.value} "
                    f"approval for {chosen_kind.value}"
                ),
                required_role=requirement.required_role,
                payload=approval_snapshot(action),
            )
            return self._move(action, ActionStatus.AWAITING_APPROVAL)

        return self._make_ready(action)

    # ── Approval outcomes ───────────────────────────────────────────────────

    def approve(self, action_id: str, *, resolved_by: str, note: str = "") -> Action:
        """Record a human's yes and release the Action to `READY`.

        The existing approval repository owns the resolver check: a live
        membership satisfying the request's required role, and an active user
        account. This method adds the Action-side checks — the window has not
        closed, the stored snapshot still matches, and a budget can be promised
        — and then reserves the budget before marking the Action ready.

        Raises:
            ActionConflict: The Action is not awaiting an answer, or its window
                has elapsed.
            PermissionError: The resolver's approval answer is not valid.
            StaleApproval: The stored content no longer matches the approval.
            BudgetExceeded: No budget can be promised for the approved amount.
        """
        action, row = self.actions.lock(action_id)
        if action.status is not ActionStatus.AWAITING_APPROVAL:
            raise ActionConflict(
                f"Action {action_id} is {action.status.value}, not awaiting approval"
            )
        self._expire_locked(action, row)
        if action.is_expired():
            raise ActionConflict(
                f"Action {action_id} expired at {action.expires_at.isoformat()}; "
                "its approval can no longer be answered"
            )
        if not action.required_approval_ref:
            raise ActionConflict(
                f"Action {action_id} says it needs approval but names none"
            )
        approval = self.approvals.resolve(
            action.required_approval_ref,
            ApprovalStatus.APPROVED,
            resolved_by=resolved_by,
            note=note,
        )
        verify_approval_snapshot(action, approval.payload)
        ready = self._make_ready(action, row=row)
        return ready

    def reject(self, action_id: str, *, resolved_by: str, note: str = "") -> Action:
        """Record a human's no and release anything the Action had reserved.

        Raises:
            ActionConflict: The Action is not awaiting an answer, or its window
                has elapsed.
            PermissionError: The resolver may not answer this request.
        """
        action, row = self.actions.lock(action_id)
        if action.status is not ActionStatus.AWAITING_APPROVAL:
            raise ActionConflict(
                f"Action {action_id} is {action.status.value}, not awaiting approval"
            )
        self._expire_locked(action, row)
        if action.is_expired():
            raise ActionConflict(
                f"Action {action_id} expired at {action.expires_at.isoformat()}; "
                "its approval can no longer be answered"
            )
        if not action.required_approval_ref:
            raise ActionConflict(
                f"Action {action_id} says it needs approval but names none"
            )
        self.approvals.resolve(
            action.required_approval_ref,
            ApprovalStatus.REJECTED,
            resolved_by=resolved_by,
            note=note,
        )
        self.budgets.release(action.action_id, reason="approval rejected")
        return self._move(action, ActionStatus.REJECTED, row=row)

    def sync_after_resolution(self, approval_id: str) -> Action | None:
        """Move an Action after its approval was resolved outside this service.

        The existing `/approvals/{id}/resolve` route predates Actions and is
        still the human's door. When its payload names an Action, the Action
        has to follow the answer in the same transaction — otherwise the route
        would resolve a question and leave the work in `AWAITING_APPROVAL`,
        which is the bypass the binding exists to close. An unrelated approval
        returns `None` and is untouched.
        """
        try:
            approval = self.approvals.get(approval_id=approval_id)
        except NotFound:
            return None
        action_id = approval.payload.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            return None
        try:
            action, row = self.actions.lock(action_id)
        except NotFound:
            return None
        if action.required_approval_ref != approval_id:
            return None
        if action.status is not ActionStatus.AWAITING_APPROVAL:
            return action
        if approval.status is ApprovalStatus.APPROVED:
            verify_approval_snapshot(action, approval.payload)
            return self._make_ready(action, row=row)
        if approval.status is ApprovalStatus.REJECTED:
            self.budgets.release(action.action_id, reason="approval rejected")
            return self._move(action, ActionStatus.REJECTED, row=row)
        if approval.status is ApprovalStatus.EXPIRED:
            self.budgets.release(action.action_id, reason="approval expired")
            return self._move(action, ActionStatus.EXPIRED, row=row)
        return action

    # ── Claim ───────────────────────────────────────────────────────────────

    def claim(
        self,
        action_id: str,
        *,
        claimed_by: str,
        principal_user_id: str | None = None,
    ) -> tuple[Action, bool]:
        """Attempt to become the one executor of an Action.

        Returns `(action, claimed)`. `claimed` is false when this call did not
        win, and it is false for every non-`READY` state: a duplicate claim of
        `CLAIMED`, `ACCEPTED`, `UNKNOWN` or a terminal Action must not submit
        anything a second time. The caller checks the flag before touching an
        external system.

        Before the transition, everything the Action was approved under is
        re-read: the claimant's live membership, the current AuthorityEnvelope
        version and its resource limits, the current execution contract version,
        the approval's resolution and resolver, the exact approval snapshot, the
        Action's expiry, and the budget account. This is the "re-auth every time"
        rule rather than a check done once at creation.

        Raises:
            PermissionError: The claimant is not a live member, the actor does
                not match, or the approving member's membership or account was
                revoked or deactivated after approval.
            ApprovalRequired: The current envelope requires an approval the
                Action does not have, or the approval is still pending.
            StaleApproval: The envelope, contract, content, recipient, cost or
                expiry changed after approval.
            BudgetExceeded: The account is paused or the reservation is no
                longer a live promise.
            ActionConflict: The Action expired (a successful expiry is recorded
                first), or its reservation is inconsistent.
        """
        action, row = self.actions.lock(action_id)
        if action.status in (
            ActionStatus.CLAIMED,
            ActionStatus.ACCEPTED,
            ActionStatus.UNKNOWN,
            ActionStatus.COMPLETED,
            ActionStatus.FAILED_CONFIRMED,
            ActionStatus.CANCELLED,
            ActionStatus.REJECTED,
            ActionStatus.EXPIRED,
        ):
            return action, False
        if action.is_expired():
            self._expire_locked(action, row)
            raise ActionConflict(
                f"Action {action_id} expired at {action.expires_at.isoformat()}; "
                "a new Action with a fresh approval is required"
            )
        if action.status is not ActionStatus.READY:
            if action.status in (
                ActionStatus.DRAFT,
                ActionStatus.AWAITING_APPROVAL,
            ):
                raise ApprovalRequired(
                    f"Action {action_id} is {action.status.value} and may not be "
                    "executed; only an approved READY Action may be claimed"
                )
            return action, False

        self._require_claimant(action, claimed_by, principal_user_id)
        version, envelope = self.current_envelope()
        if version != action.envelope_version:
            raise StaleApproval(
                f"the AuthorityEnvelope is version {version} and this Action was "
                f"approved under version {action.envelope_version}; a new Action "
                "and approval are required"
            )
        self._check_resources(action.resource_request, envelope)
        if (
            envelope is not None
            and envelope.requires_human_approval(action.kind.value)
            and not action.required_approval_ref
        ):
            raise ApprovalRequired(
                f"the current AuthorityEnvelope requires approval for "
                f"{action.kind.value}, and this Action has none"
            )
        if action.required_approval_ref:
            self._verify_approval(action)
        self._verify_contract_unchanged(action)
        self.budgets.verify(action)

        attempt = ActionAttempt(
            claimed_at=utcnow(),
            outcome=ActionAttemptOutcome.PENDING,
        )
        claimed = self._move(
            action,
            ActionStatus.CLAIMED,
            row=row,
            claimed_by=claimed_by,
            claimed_at=attempt.claimed_at,
            attempts=(*action.attempts, attempt),
        )
        return claimed, True

    # ── Outcomes ────────────────────────────────────────────────────────────

    def record_accepted(
        self,
        action_id: str,
        *,
        external_ref: str | None = None,
        note: str | None = None,
        reported_by: str | None = None,
    ) -> Action:
        """The external system accepted the work. Not success, and not cost.

        Raises:
            ActionConflict: The Action is not `CLAIMED`.
        """
        action, row = self.actions.lock(action_id)
        self._require_reporter(action, reported_by)
        if action.status is ActionStatus.ACCEPTED:
            return action
        if action.status is not ActionStatus.CLAIMED:
            raise ActionConflict(
                f"Action {action_id} is {action.status.value}; only a CLAIMED "
                "Action can be accepted"
            )
        return self._move(
            action,
            ActionStatus.ACCEPTED,
            row=row,
            accepted_at=utcnow(),
            external_ref=external_ref or action.external_ref,
            external_state="ACCEPTED",
            attempts=_finish_attempt(
                action.attempts, ActionAttemptOutcome.ACCEPTED, external_ref, note
            ),
        )

    def complete(
        self,
        action_id: str,
        *,
        actual_cny: str | None = None,
        external_ref: str | None = None,
        reported_by: str | None = None,
    ) -> Action:
        """The work finished, with a known actual cost.

        `actual_cny` is required. A caller that does not know the cost records
        `record_unknown` instead; settling at zero would convert "we do not
        know" into "it was free", which is the accounting error the reservation
        exists to prevent.

        Raises:
            ValueError: The actual cost is absent.
            ActionConflict: The Action is not `ACCEPTED`, or its reservation
                was released.
        """
        if actual_cny is None:
            raise ValueError(
                "completing an Action requires the actual cost; an unknown cost "
                "stays UNKNOWN and is never settled as zero"
            )
        action, row = self.actions.lock(action_id)
        self._require_reporter(action, reported_by)
        if action.status is ActionStatus.COMPLETED:
            return action
        if action.status is ActionStatus.UNKNOWN:
            raise ActionConflict(
                f"Action {action_id} has an unknown external outcome; reconcile it "
                "to ACCEPTED or FAILED_CONFIRMED before completing it"
            )
        if action.status is not ActionStatus.ACCEPTED:
            raise ActionConflict(
                f"Action {action_id} is {action.status.value}; only an ACCEPTED "
                "Action can be completed"
            )
        settled, overrun_ref = self.budgets.settle(action_id, actual_cny)
        return self._move(
            action,
            ActionStatus.COMPLETED,
            row=row,
            completed_at=utcnow(),
            external_ref=external_ref or action.external_ref,
            external_state="COMPLETED",
            overrun_event_ref=overrun_ref or action.overrun_event_ref,
            budget_reservation_ref=settled.reservation_id,
        )

    def record_unknown(
        self, action_id: str, *, reason: str, reported_by: str | None = None
    ) -> Action:
        """The submission's outcome is not known.

        The budget reservation stays held and the concurrency slot stays
        occupied. For mail, `Action.projected_status` shows this as
        `UNKNOWN_DELIVERY`; the internal state is still one `UNKNOWN`.

        Raises:
            ActionConflict: The Action is not `CLAIMED` or `ACCEPTED`.
        """
        action, row = self.actions.lock(action_id)
        self._require_reporter(action, reported_by)
        if action.status is ActionStatus.UNKNOWN:
            return action
        if action.status not in (ActionStatus.CLAIMED, ActionStatus.ACCEPTED):
            raise ActionConflict(
                f"Action {action_id} is {action.status.value}; an unknown result "
                "belongs to a claim that was actually attempted"
            )
        self.budgets.mark_unknown(action_id)
        return self._move(
            action,
            ActionStatus.UNKNOWN,
            row=row,
            uncertainty=reason,
            external_state="UNKNOWN",
            attempts=_finish_attempt(
                action.attempts, ActionAttemptOutcome.UNKNOWN, None, reason
            ),
        )

    def record_failed_confirmed(
        self,
        action_id: str,
        *,
        reason: str,
        actual_cny: str | None = None,
        reported_by: str | None = None,
    ) -> Action:
        """The external system definitely did not do the work.

        With no actual cost the reservation is released: the known outcome is
        that nothing was spent. If a cost was consumed by the failed attempt,
        pass it and the reservation settles to that amount.

        Raises:
            ActionConflict: The Action is not `CLAIMED`, `ACCEPTED` or `UNKNOWN`.
        """
        action, row = self.actions.lock(action_id)
        self._require_reporter(action, reported_by)
        if action.status is ActionStatus.FAILED_CONFIRMED:
            return action
        if action.status not in (
            ActionStatus.CLAIMED,
            ActionStatus.ACCEPTED,
            ActionStatus.UNKNOWN,
        ):
            raise ActionConflict(
                f"Action {action_id} is {action.status.value}; a confirmed failure "
                "requires an attempt that was made"
            )
        overrun_ref = action.overrun_event_ref
        if actual_cny is None:
            self.budgets.release(action_id, reason="known failure")
        else:
            _settled, overrun_ref = self.budgets.settle(action_id, actual_cny)
        return self._move(
            action,
            ActionStatus.FAILED_CONFIRMED,
            row=row,
            failure_reason=reason,
            external_state="FAILED_CONFIRMED",
            overrun_event_ref=overrun_ref,
            attempts=_finish_attempt(
                action.attempts, ActionAttemptOutcome.FAILED_CONFIRMED, None, reason
            ),
        )

    def reconcile(
        self,
        action_id: str,
        *,
        outcome: ActionAttemptOutcome | str,
        actual_cny: str | None = None,
        external_ref: str | None = None,
        note: str = "",
        reported_by: str | None = None,
    ) -> Action:
        """Resolve an `UNKNOWN` Action to `ACCEPTED` or `FAILED_CONFIRMED`.

        `ACCEPTED` leaves the budget reservation held: acceptance is not
        completion and the actual cost is still unknown. `FAILED_CONFIRMED`
        releases the reservation unless an actual cost is supplied.

        Raises:
            ValueError: The outcome is not one of the two reconciliation ends.
            ActionConflict: The Action is not `UNKNOWN`.
        """
        parsed = (
            outcome
            if isinstance(outcome, ActionAttemptOutcome)
            else ActionAttemptOutcome(str(outcome).upper())
        )
        if parsed not in (
            ActionAttemptOutcome.ACCEPTED,
            ActionAttemptOutcome.FAILED_CONFIRMED,
        ):
            raise ValueError(
                "reconciliation ends in ACCEPTED or FAILED_CONFIRMED; an unknown "
                "outcome that becomes another unknown is not reconciled"
            )
        action, row = self.actions.lock(action_id)
        self._require_reporter(action, reported_by)
        if action.status is not ActionStatus.UNKNOWN:
            raise ActionConflict(
                f"Action {action_id} is {action.status.value}, not UNKNOWN"
            )
        if parsed is ActionAttemptOutcome.ACCEPTED:
            return self._move(
                action,
                ActionStatus.ACCEPTED,
                row=row,
                accepted_at=action.accepted_at or utcnow(),
                external_ref=external_ref or action.external_ref,
                external_state="ACCEPTED",
            )
        overrun_ref = action.overrun_event_ref
        if actual_cny is None:
            self.budgets.release(action_id, reason="reconciled as failed")
        else:
            _settled, overrun_ref = self.budgets.settle(action_id, actual_cny)
        return self._move(
            action,
            ActionStatus.FAILED_CONFIRMED,
            row=row,
            failure_reason=note or "reconciled as failed",
            external_ref=external_ref or action.external_ref,
            external_state="FAILED_CONFIRMED",
            overrun_event_ref=overrun_ref,
        )

    def cancel(self, action_id: str, *, cancelled_by: str, reason: str = "") -> Action:
        """Cancel work that has not been claimed. Released budget, if any.

        A claimed Action cannot be cancelled: an external effect may already be
        in flight, and calling that cancellation would be a false record. Report
        `record_unknown` and reconcile instead.

        Raises:
            PermissionError: The caller is not a live member or the Action actor.
            ActionConflict: The Action has been claimed or is already terminal.
        """
        action, row = self.actions.lock(action_id)
        if action.status in (
            ActionStatus.CANCELLED,
            ActionStatus.REJECTED,
            ActionStatus.EXPIRED,
            ActionStatus.FAILED_CONFIRMED,
            ActionStatus.COMPLETED,
        ):
            return action
        if action.status is ActionStatus.CLAIMED:
            raise ActionConflict(
                f"Action {action_id} is CLAIMED; an external effect may be in "
                "flight, so it is reported UNKNOWN and reconciled rather than "
                "cancelled"
            )
        self._require_actor(cancelled_by)
        if cancelled_by != action.actor:
            membership = MembershipRepository(
                self.session, self.project_id
            ).for_user(cancelled_by)
            if (
                membership is None
                or not membership.is_active
                or membership.role not in (UserRole.PROJECT_OWNER, UserRole.ADMIN)
            ):
                raise PermissionError(
                    f"{cancelled_by!r} did not request Action {action_id} and is not "
                    "a live owner/admin who may cancel it"
                )
        self.budgets.release(action_id, reason="cancelled")
        if action.required_approval_ref:
            self.approvals.expire(
                action.required_approval_ref,
                note=f"Action {action_id} was cancelled",
            )
        return self._move(
            action,
            ActionStatus.CANCELLED,
            row=row,
            uncertainty=reason or action.uncertainty,
            failure_reason=reason or action.failure_reason,
        )

    def expire_due(self, *, at: datetime | None = None) -> list[Action]:
        """Expire every Action whose approval window has closed.

        Idempotent and safe to run in one transaction. An approval that was
        still pending is closed as `EXPIRED` in the same pass, so a human is
        never shown a question about work that can no longer start.
        """
        moment = at or utcnow()
        expired: list[Action] = []
        for candidate in self.actions.pending_expiry(at=moment):
            action, row = self.actions.lock(candidate.action_id)
            before = action.status
            self._expire_locked(action, row, at=moment)
            if before is not ActionStatus.EXPIRED:
                expired.append(action)
        return expired

    # ── Reads ───────────────────────────────────────────────────────────────

    def get(self, action_id: str) -> Action:
        """One Action in this project, or `NotFound`."""
        return self.actions.get(action_id=action_id)

    def list(
        self,
        *,
        status: ActionStatus | None = None,
        kind: ActionKind | None = None,
        limit: int = 200,
    ) -> list[Action]:
        """Newest first, optionally narrowed."""
        return self.actions.list_actions(status=status, kind=kind, limit=limit)

    def current_envelope(self) -> tuple[int, AuthorityEnvelope | None]:
        """The governing envelope version, and the envelope if one exists.

        A project with no envelope row is version 1 of an empty envelope. The
        row is *not* materialised: absence is a real state — RAVEL has not been
        given a narrower instruction yet — and writing a default row would make
        an omission look like a decision. The version still exists so an Action
        approved under "nothing configured" can detect a real envelope replacing
        it.
        """
        try:
            envelope = AuthorityEnvelopeRepository(
                self.session, self.project_id
            ).latest()
        except NotFound:
            return 1, None
        return envelope.version, envelope

    # ── Internal lifecycle ──────────────────────────────────────────────────

    def _make_ready(
        self, action: Action, *, row: ActionRow | None = None
    ) -> Action:
        """Reserve budget, then move to `READY`.

        The order is the rule: a `READY` Action has a budget promise, and a
        failed reservation leaves no executable state behind.
        """
        reservation = self.budgets.reserve(action)
        bound = action.model_copy(
            update={"budget_reservation_ref": reservation.reservation_id}
        )
        return self._move(bound, ActionStatus.READY, row=row)

    def _move(
        self,
        action: Action,
        status: ActionStatus,
        *,
        row: ActionRow | None = None,
        **updates: Any,
    ) -> Action:
        """Apply one legal transition and persist its mutable half.

        Raises:
            ActionConflict: The transition table does not permit the move.
        """
        check = can_transition_action(action.status, status)
        if not check.allowed:
            raise ActionConflict(check.reason)
        changed = action.model_copy(update={"status": status, **updates})
        self.actions.save(changed, row)
        return changed

    def _expire_locked(
        self,
        action: Action,
        row: ActionRow,
        *,
        at: datetime | None = None,
    ) -> None:
        """Expire an Action in place when its window has closed."""
        if not action.is_expired(at):
            return
        if action.status not in (
            ActionStatus.DRAFT,
            ActionStatus.AWAITING_APPROVAL,
            ActionStatus.READY,
        ):
            return
        self.budgets.release(action.action_id, reason="approval window elapsed")
        if action.required_approval_ref:
            self.approvals.expire(
                action.required_approval_ref,
                note=f"Action {action.action_id}'s approval window elapsed",
            )
        self._move(action, ActionStatus.EXPIRED, row=row)

    def _verify_approval(self, action: Action) -> None:
        """Re-read the approval and its human before execution.

        Raises:
            ApprovalRequired: The approval is still pending or was not approved.
            PermissionError: The resolving member is no longer live, their
                account is inactive, or they no longer hold the required role.
            StaleApproval: The stored snapshot no longer matches the Action.
        """
        approval = self.approvals.get(approval_id=action.required_approval_ref)
        if approval.status is not ApprovalStatus.APPROVED:
            raise ApprovalRequired(
                f"approval {approval.approval_id} is {approval.status.value}; "
                f"Action {action.action_id} may not execute"
            )
        if approval.action != action.kind.value:
            raise StaleApproval(
                f"approval {approval.approval_id} authorizes {approval.action!r}, "
                f"not this {action.kind.value} Action"
            )
        if not approval.resolved_by:
            raise ApprovalRequired(
                f"approval {approval.approval_id} records no human resolver"
            )
        membership = MembershipRepository(self.session, self.project_id).for_user(
            approval.resolved_by
        )
        if membership is None:
            raise PermissionError(
                f"{approval.resolved_by!r} approved this Action and is no longer a "
                f"member of {self.project_id}; a revoked member cannot authorize "
                "an execution"
            )
        if not membership.satisfies(approval.required_role):
            raise PermissionError(
                f"{approval.resolved_by!r} holds {membership.role.value} and the "
                f"approval requires {approval.required_role.value}"
            )
        if not UserRepository(self.session).get(approval.resolved_by).is_active:
            raise PermissionError(
                f"the account of {approval.resolved_by!r} is deactivated; its "
                "approval no longer authorizes execution"
            )
        verify_approval_snapshot(action, approval.payload)

    def _verify_contract_unchanged(self, action: Action) -> None:
        """Refuse an Action whose frozen execution contract has been replaced.

        Any Action that names a node and a contract is bound to it — compute
        submissions and lab handovers alike. An Action that names neither (a
        mail send) has no execution contract to re-read.

        Raises:
            StaleApproval: A newer execution contract version exists, or the
                recorded contract is gone.
        """
        if (
            action.node_id is None
            or action.contract_id is None
            or action.contract_version is None
        ):
            return
        try:
            latest = ExecutionContractRepository(
                self.session, self.project_id
            ).for_node(action.node_id)
        except NotFound as error:
            raise StaleApproval(
                "the execution contract this Action was approved under no longer "
                "exists"
            ) from error
        if (
            latest.contract_id != action.contract_id
            or latest.version != action.contract_version
        ):
            raise StaleApproval(
                f"the execution contract is now version {latest.version} "
                f"({latest.contract_id}), and this Action was approved under "
                f"version {action.contract_version} ({action.contract_id}); a new "
                "Action and approval are required"
            )

    def _verify_contract(
        self,
        *,
        node_id: str | None,
        contract_id: str | None,
        contract_version: int | None,
        kind: ActionKind,
    ) -> ExecutionContract | None:
        """Resolve the current execution contract for a compute Action.

        Raises:
            ActionError: A compute Action does not name a node and contract, or
                the named contract is not the newest frozen version.
            NotFound: No such contract.
        """
        if kind is not ActionKind.COMPUTE_SUBMIT:
            return None
        if not node_id or not contract_id or contract_version is None:
            raise ActionError(
                "a COMPUTE_SUBMIT Action requires node_id, contract_id and "
                "contract_version; without them its HPC submission key cannot be "
                "reproduced"
            )
        contract = ExecutionContractRepository(self.session, self.project_id).for_node(
            node_id, version=contract_version
        )
        latest = ExecutionContractRepository(self.session, self.project_id).for_node(
            node_id
        )
        if contract.contract_id != contract_id:
            raise ActionError(
                f"execution contract {contract_id!r} is not a version of node "
                f"{node_id!r}"
            )
        if latest.contract_id != contract.contract_id or latest.version != contract.version:
            raise StaleApproval(
                f"the node's execution contract is version {latest.version}; an "
                f"Action cannot be proposed under superseded version "
                f"{contract.version}"
            )
        return contract

    def _check_resources(
        self,
        request: ResourceRequest,
        envelope: AuthorityEnvelope | None,
    ) -> None:
        """Refuse a resource request wider than the effective limits.

        The deployment defaults and the envelope are intersected, so a default
        can never widen a stricter envelope.

        Raises:
            BudgetExceeded: A quantity exceeds the tighter limit.
        """
        limits = effective_resource_limits(
            envelope.budget_time_limits if envelope is not None else None,
            cpu_hours=self.settings.action_max_cpu_hours,
            gpu_hours=self.settings.action_max_gpu_hours,
            wall_clock_hours=self.settings.action_max_wall_clock_hours,
        )
        violation = request.describe_violation(limits)
        if violation is not None:
            raise BudgetExceeded(violation)

    def _require_actor(self, actor: str) -> None:
        """Check that an actor is a live member or a fixed agent role.

        Raises:
            PermissionError: The identifier names neither an active member nor
                one of the five agent roles.
        """
        user_row = self.session.get(UserRow, actor)
        if user_row is not None:
            if not user_row.is_active:
                raise PermissionError(f"account {actor!r} is deactivated")
            membership = MembershipRepository(self.session, self.project_id).for_user(
                actor
            )
            if membership is None:
                raise PermissionError(
                    f"{actor!r} holds no live membership in {self.project_id}"
                )
            return
        if actor not in _AGENT_ACTORS:
            raise PermissionError(
                f"{actor!r} is not a live project member or a RAVEL agent role; an "
                "Action must be attributable to one or the other"
            )

    def _require_reporter(self, action: Action, reported_by: str | None) -> None:
        """Only the executor -- or an owner reconciling -- may report an outcome.

        Reporting is not a read: it can release or settle a budget and move the
        Action to a terminal state. A caller that never claimed the Action must
        not be able to write its ending.
        """
        if action.claimed_by is None:
            raise PermissionError(
                f"Action {action.action_id} has no claimant recorded; its outcome "
                "cannot be reported"
            )
        reporter = reported_by or action.claimed_by
        if reporter == action.claimed_by:
            self._require_actor(reporter)
            return
        membership = MembershipRepository(self.session, self.project_id).for_user(
            reporter
        )
        if (
            membership is None
            or not membership.is_active
            or membership.role is not UserRole.PROJECT_OWNER
        ):
            raise PermissionError(
                f"{reporter!r} did not claim Action {action.action_id} and is not a "
                "live owner who may reconcile it"
            )
        if not UserRepository(self.session).get(reporter).is_active:
            raise PermissionError(f"account {reporter!r} is deactivated")

    def _require_claimant(
        self, action: Action, claimed_by: str, principal_user_id: str | None
    ) -> None:
        """Verify the executor's identity at claim time.

        A user identifier must name a live member and an active account. An
        agent role must be the role that requested the Action; otherwise any
        agent could execute work another role proposed.

        Raises:
            PermissionError: The claimant has no live standing.
        """
        subject = principal_user_id or claimed_by
        if self.session.get(UserRow, subject) is not None:
            self._require_actor(subject)
            if subject != action.actor:
                membership = MembershipRepository(
                    self.session, self.project_id
                ).for_user(subject)
                if (
                    membership is None
                    or membership.role not in (UserRole.PROJECT_OWNER, UserRole.ADMIN)
                ):
                    raise PermissionError(
                        f"{subject!r} did not request Action {action.action_id} and "
                        "is not a live owner/admin who may claim it"
                    )
            return
        if principal_user_id is not None:
            raise PermissionError(
                f"{principal_user_id!r} is not a user account and cannot be a "
                "human principal"
            )
        if claimed_by not in _AGENT_ACTORS:
            raise PermissionError(
                f"{claimed_by!r} is neither a live user member nor an agent role"
            )
        if claimed_by != action.actor:
            raise PermissionError(
                f"{claimed_by!r} did not request Action {action.action_id} "
                f"({action.actor!r} did); a role may execute only the work it asked "
                "to start"
            )

    def _compute_body(
        self,
        body: dict[str, Any],
        *,
        contract_version: int | None,
        node_id: str | None,
    ) -> dict[str, Any]:
        """Fill and verify the HPC submission key before hashing.

        Raises:
            ActionError: A node or contract version is missing, or the supplied
                key is not the hash of this attempt.
        """
        if not node_id or contract_version is None:
            raise ActionError(
                "a COMPUTE_SUBMIT Action requires node_id and contract_version to "
                "compute its HPC submission key"
            )
        attempt = attempt_number(body)
        expected = compute_submission_key(
            project_id=self.project_id,
            node_id=node_id,
            contract_version=contract_version,
            attempt=attempt,
        )
        supplied = body.get("submission_key")
        if supplied is not None and supplied != expected:
            raise ActionError(
                "submission_key does not hash this project, node, contract version "
                "and attempt; a hand-picked key cannot promise that a retry is the "
                "same submission"
            )
        body["submission_key"] = expected
        body.setdefault("attempt", attempt)
        return body

    def _idempotency_key(
        self,
        kind: ActionKind,
        payload: dict[str, Any],
        *,
        explicit: str | None,
        contract_version: int | None,
        node_id: str | None,
    ) -> str:
        """The project-unique key one external submission is recorded under.

        Raises:
            ActionError: A non-compute Action has no externally meaningful key.
        """
        if explicit:
            if not explicit.strip():
                raise ActionError("an explicit idempotency key may not be blank")
            return explicit.strip()
        if kind is ActionKind.COMPUTE_SUBMIT:
            if not node_id or contract_version is None:
                raise ActionError(
                    "a COMPUTE_SUBMIT Action requires node_id and contract_version "
                    "before an idempotency key can be computed"
                )
            return compute_submission_key(
                project_id=self.project_id,
                node_id=node_id,
                contract_version=contract_version,
                attempt=attempt_number(payload),
            )
        if kind is ActionKind.MAIL_SEND:
            return f"mail:{payload['message_id']}"
        raise ActionError(
            f"a {kind.value} Action requires an explicit idempotency_key; its "
            "external system has no identifier RAVEL can derive one from"
        )

    def _advisory_lock(self, key: str) -> None:
        """Serialise repeated creates of one external submission."""
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key}
        )


def _finish_attempt(
    attempts: tuple[ActionAttempt, ...],
    outcome: ActionAttemptOutcome,
    external_ref: str | None,
    note: str | None,
) -> tuple[ActionAttempt, ...]:
    """Mark the newest pending attempt with its outcome.

    A claim is always the last attempt, so the newest pending one is the one
    this report is about. If none is pending the record is returned unchanged;
    that happens for a repeated report and is not a new fact.
    """
    if not attempts:
        return attempts
    latest = attempts[-1]
    if latest.outcome is not ActionAttemptOutcome.PENDING:
        return attempts
    finished = latest.model_copy(
        update={
            "outcome": outcome,
            "external_ref": external_ref or latest.external_ref,
            "note": note or latest.note,
        }
    )
    return (*attempts[:-1], finished)


__all__ = ["ActionService"]
