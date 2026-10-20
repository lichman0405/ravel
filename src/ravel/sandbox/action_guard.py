"""The Action check that stands between an agent and a sandbox command.

An agent that can run arbitrary argv is an agent that can bypass every tool in
the MCP surface. The sandbox is memory-safe and isolated, but "the container
would let it" is not the question this layer answers. The question is whether
a person (or a Master under an envelope) approved *this command*. That is an
Action: a recorded proposal, a claimed executor, an immutable payload hash and
a budget reservation.

`DatabaseActionGuard` compares four facts before the supervisor is asked to run
anything:

- the Action is a `SANDBOX_EXEC` in the authority's project, not a neighbour's;
- it was claimed, by the role that is now asking;
- the claim belongs to the same binding as the caller's lease; and
- the command digest recorded on the Action equals the digest of the argv the
  supervisor is about to execute.

A mismatch is a refusal, not a warning. The supervisor repeats the digest
comparison on its side, so a compromised provider cannot swap the command
between the check and the exec.

`StaticActionGuard` is the same contract over a small in-memory grant table.
It exists for tests that drive a real container but have no reason to set up a
PostgreSQL project, and for a container image whose tool server has been given
a pre-claimed grant. It is still a grant with a digest and a role; it is not a
blanket "allow".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ravel.actions.service import ActionService
from ravel.domain.actions import ActionConflict
from ravel.domain.enums import ActionKind, ActionStatus
from ravel.sandbox.authority import agent_actor_for_role
from ravel.sandbox.models import (
    RunRef,
    RunState,
    command_digest,
)
from ravel.state.database import Database
from ravel.state.repositories.base import NotFound


def sandbox_command_payload(
    *,
    argv: list[str] | tuple[str, ...],
    cwd: str,
    env_refs: list[str] | tuple[str, ...] = (),
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """The payload an approved `SANDBOX_EXEC` Action carries.

    The argv itself is deliberately absent. A payload is stored, and argv can
    contain a credential a careless caller passed as an argument; the digest is
    enough to prove that the command about to run is the command that was
    approved, and the run record keeps the redacted form for the audit trail.
    """
    return {
        "command_digest": command_digest(argv=argv, cwd=cwd, env_refs=env_refs),
        "cwd": cwd,
        "env_refs": sorted(set(env_refs)),
        "timeout_seconds": float(timeout_seconds),
    }


def _authority_field(authority: Any, name: str) -> Any:
    if isinstance(authority, dict):
        return authority.get(name)
    return getattr(authority, name, None)


def _actor_and_epoch(authority: Any) -> tuple[str, int | None]:
    role = str(_authority_field(authority, "agent_role") or "")
    return agent_actor_for_role(role), _authority_field(authority, "lease_epoch")


@dataclass(frozen=True, slots=True)
class StaticGrant:
    """One recorded permission for a test or a pre-warmed tool server."""

    action_id: str
    project_id: str
    command_digest: str
    actor: str
    binding_id: str = ""
    lease_epoch: int | None = None


class StaticActionGuard:
    """A digest-bound grant table with the same refusals as the database one."""

    def __init__(self, grants: list[StaticGrant] | tuple[StaticGrant, ...] = ()) -> None:
        self._grants = {grant.action_id: grant for grant in grants}
        self.outcomes: list[tuple[str, RunRef]] = []

    @classmethod
    def granting(
        cls,
        *,
        action_id: str,
        project_id: str,
        argv: list[str] | tuple[str, ...],
        cwd: str,
        env_refs: list[str] | tuple[str, ...] = (),
    ) -> StaticActionGuard:
        """One guard holding one correct digest, for the common test shape."""
        return cls(
            [
                StaticGrant(
                    action_id=action_id,
                    project_id=project_id,
                    command_digest=command_digest(
                        argv=argv, cwd=cwd, env_refs=env_refs
                    ),
                    actor="",
                )
            ]
        )

    def verify(
        self,
        *,
        action_id: str,
        project_id: str,
        command_digest: str,
        authority: Any,
    ) -> None:
        grant = self._grants.get(action_id)
        if grant is None:
            raise PermissionError(f"no SANDBOX_EXEC grant records Action {action_id!r}")
        if grant.project_id != project_id:
            raise PermissionError(
                f"Action {action_id!r} belongs to project {grant.project_id!r}, not "
                f"{project_id!r}"
            )
        if grant.command_digest != command_digest:
            raise PermissionError(
                "the command digest does not match the one recorded for Action "
                f"{action_id!r}; refusing to run a different command than was granted"
            )
        actor, epoch = _actor_and_epoch(authority)
        if grant.actor and grant.actor != actor:
            raise PermissionError(
                f"Action {action_id!r} was granted to {grant.actor!r}, not {actor!r}"
            )
        if (
            grant.lease_epoch is not None
            and epoch is not None
            and grant.lease_epoch != epoch
        ):
            raise PermissionError(
                f"Action {action_id!r} was granted under lease epoch "
                f"{grant.lease_epoch}, not {epoch}"
            )

    def record_outcome(
        self, *, action_id: str, run: RunRef, authority: Any
    ) -> None:
        self.outcomes.append((action_id, run))


class DatabaseActionGuard:
    """The production guard: a claimed `SANDBOX_EXEC` in PostgreSQL.

    The guard writes the ending of the Action after the run returns. A
    successful command is recorded as accepted and completed at zero cost (the
    sandbox is local compute; its cost is the deployment's, not a metered
    external spend). Failed, timed-out, and cancelled runs are confirmed
    failures and release the reservation. `LOST` — the supervisor restarted
    while the command may have been running — is recorded as unknown rather
    than failed, because claiming it definitely did nothing would be a guess
    recorded as a fact.
    """

    def __init__(self, database: Database, project_id: str) -> None:
        self.database = database
        self.project_id = project_id

    def verify(
        self,
        *,
        action_id: str,
        project_id: str,
        command_digest: str,
        authority: Any,
    ) -> None:
        if project_id != self.project_id:
            raise PermissionError(
                f"this guard is bound to {self.project_id!r}, not {project_id!r}"
            )
        actor, epoch = _actor_and_epoch(authority)
        with self.database.read_only() as session:
            try:
                action = ActionService(session, self.project_id).get(action_id)
            except NotFound as exc:
                # An Action id from another project (or no Action at all) is a
                # permission refusal here, not a not-found that escapes the
                # guard as an untyped repository error.
                raise PermissionError(
                    f"no SANDBOX_EXEC Action {action_id!r} exists in project "
                    f"{self.project_id!r}"
                ) from exc
        if action.project_id != self.project_id:
            raise PermissionError(
                f"Action {action_id!r} belongs to another project"
            )
        if action.kind is not ActionKind.SANDBOX_EXEC:
            raise PermissionError(
                f"Action {action_id!r} is a {action.kind.value}; a sandbox command "
                "requires a SANDBOX_EXEC Action"
            )
        if action.status is not ActionStatus.CLAIMED:
            raise PermissionError(
                f"Action {action_id!r} is {action.status.value}; a sandbox command "
                "may run only under a CLAIMED Action"
            )
        if action.actor != actor:
            raise PermissionError(
                f"Action {action_id!r} was requested by {action.actor!r}, and this "
                f"caller is {actor!r}; a role may execute only the work it proposed"
            )
        if action.claimed_by != actor:
            raise PermissionError(
                f"Action {action_id!r} was claimed by {action.claimed_by!r}, not "
                f"{actor!r}"
            )
        binding = str(_authority_field(authority, "binding_id") or "")
        if binding and action.requesting_binding != binding:
            raise PermissionError(
                f"Action {action_id!r} was requested under binding "
                f"{action.requesting_binding!r}, not the caller's {binding!r}"
            )
        recorded = str(action.payload.get("command_digest", ""))
        if recorded != command_digest:
            raise PermissionError(
                "the command digest does not match the digest recorded on Action "
                f"{action_id!r}; refusing to run a different command than was approved"
            )
        recorded_epoch = action.payload.get("lease_epoch")
        if (
            isinstance(recorded_epoch, int)
            and epoch is not None
            and recorded_epoch != epoch
        ):
            raise PermissionError(
                f"Action {action_id!r} was approved under lease epoch "
                f"{recorded_epoch}, not {epoch}"
            )

    def record_outcome(
        self, *, action_id: str, run: RunRef, authority: Any
    ) -> None:
        actor, _epoch = _actor_and_epoch(authority)
        with self.database.transaction() as session:
            service = ActionService(session, self.project_id)
            action = service.get(action_id)
            if action.status in (
                ActionStatus.COMPLETED,
                ActionStatus.FAILED_CONFIRMED,
                ActionStatus.UNKNOWN,
                ActionStatus.CANCELLED,
                ActionStatus.EXPIRED,
            ):
                return
            if run.state is RunState.SUCCEEDED:
                try:
                    service.record_accepted(
                        action_id, external_ref=run.run_id, reported_by=actor
                    )
                    service.complete(
                        action_id,
                        actual_cny="0",
                        external_ref=run.run_id,
                        reported_by=actor,
                    )
                except ActionConflict:
                    # Somebody else already reported this ending. The run is
                    # still the record of what the sandbox did; do not turn a
                    # duplicate report into a failed execution.
                    return
            elif run.state is RunState.LOST:
                service.record_unknown(
                    action_id,
                    reason=(
                        "the sandbox supervisor lost track of the run; its effect "
                        "is unknown and the reservation stays held"
                    ),
                    reported_by=actor,
                )
            elif run.state in (RunState.FAILED, RunState.TIMED_OUT, RunState.CANCELLED):
                try:
                    service.record_failed_confirmed(
                        action_id,
                        reason=f"the sandbox run ended {run.state.value}",
                        reported_by=actor,
                    )
                except ActionConflict:
                    return


__all__ = [
    "DatabaseActionGuard",
    "StaticActionGuard",
    "StaticGrant",
    "sandbox_command_payload",
]
