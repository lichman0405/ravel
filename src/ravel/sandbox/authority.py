"""Who may ask a sandbox for what, and how much of it.

There are two independent bounds, and both are needed:

- **Role.** A sandbox runs commands. The Master decides what science happens and
  the Reviewer judges it; neither executes. Research needs a browser, and the
  two Worker roles need command execution. The closed list here is the same
  kind of list as `ActionKind`: a role that is not on it is refused, not given
  a narrower sandbox.
- **Quota.** The deployment configures production ceilings (2 CPUs, 4 GiB, 256
  PIDs, 10 GiB scratch, 120 seconds by default), and a project's
  `AuthorityEnvelope` may be stricter. Resolution returns the smaller of the
  two on every axis. It never returns a value larger than either input, and a
  request that names a larger value than the configured ceiling is refused
  rather than quietly reduced — a caller asking beyond its grant is operating
  on a wrong belief, and the error is the useful part.

The limits are data, not prompt instructions. A model that "remembers" it was
granted four CPUs receives two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ravel.domain.contracts import AuthorityEnvelope
from ravel.sandbox.models import (
    GIB,
    MIB,
    SandboxLimits,
    SandboxValidationError,
)

#: The agent roles that may hold a sandbox. Worker roles execute; Research
#: renders sources. Master and Review are decision and judgement, respectively,
#: and a shell is neither.
SANDBOX_ROLES: frozenset[str] = frozenset(
    {"RESEARCH", "COMPUTE_WORKER", "EXPERIMENTAL_WORKER"}
)


def normalize_role(role: str) -> str:
    """The comparison spelling of a role name.

    The composition writes `RAVEL_ROLE` as the enum's value (lowercase, hyphen
    for the Worker roles), but a client hand-constructing authority is an
    ordinary thing in a test and a deployment migration may hand over another
    spelling. Both mean the same role; accepting the difference is not a
    widening of the list.
    """
    return role.strip().replace("-", "_").upper()


def agent_actor_for_role(role: str) -> str:
    """The `AgentRole` value an Action records for a sandbox role.

    `ActionService` compares an actor against the fixed five-role list, and that
    list spells the Worker roles with a hyphen. Keeping the mapping here means
    the sandbox layer and the Action layer cannot drift into two spellings of
    one identity.
    """
    normalized = normalize_role(role)
    try:
        return {
            "RESEARCH": "research",
            "COMPUTE_WORKER": "compute-worker",
            "EXPERIMENTAL_WORKER": "experimental-worker",
        }[normalized]
    except KeyError as exc:
        raise SandboxValidationError(f"{role!r} is not a sandbox agent role") from exc


@dataclass(frozen=True, slots=True)
class SandboxAuthority:
    """One caller's claim about which project and role it is acting for.

    The supervisor checks the claim against the handle and the closed role
    list. It does not treat the claim as authentication by itself: reaching the
    supervisor requires the deployment token, and the claim is what that
    authenticated caller says it is doing. Keeping the two separate is why a
    token leaked to a Research process still cannot execute in another
    project's handle — the handle's project is checked against this value and
    the request path.
    """

    project_id: str
    binding_id: str
    agent_role: str
    lease_epoch: int | None = None
    envelope: AuthorityEnvelope | None = None

    def __post_init__(self) -> None:
        if not self.project_id or not self.project_id.strip():
            raise SandboxValidationError("sandbox authority must name a project")
        if not self.binding_id or not self.binding_id.strip():
            raise SandboxValidationError(
                "sandbox authority must name the lease binding it acts under"
            )
        role = normalize_role(self.agent_role)
        if role not in SANDBOX_ROLES:
            raise SandboxValidationError(
                f"role {self.agent_role!r} may not hold a sandbox; the roles that "
                f"may are {sorted(SANDBOX_ROLES)}. Execution is separated from "
                "decision and review, so those two roles have no shell"
            )
        if self.lease_epoch is not None and self.lease_epoch < 0:
            raise SandboxValidationError("lease_epoch cannot be negative")
        if self.envelope is not None and self.envelope.project_id != self.project_id:
            raise SandboxValidationError(
                "the authority envelope belongs to a different project than the "
                "sandbox authority; refusing to combine two projects' grants"
            )

    @property
    def role(self) -> str:
        return normalize_role(self.agent_role)

    def require_project(self, project_id: str) -> None:
        """Refuse a request aimed at a project other than this authority's."""
        if project_id != self.project_id:
            raise SandboxValidationError(
                f"sandbox authority is bound to project {self.project_id!r} and "
                f"cannot act on {project_id!r}"
            )

    def check_epoch(self, epoch: int | None) -> None:
        """Refuse a handle provisioned under a different lease epoch.

        An absent handle epoch is the deployment without leases; an authority
        that states an epoch against it is not refused, because the supervisor
        has nothing to compare. When both are present they must agree: a new
        lease is a new grant, and an old handle is not carried across it.
        """
        if epoch is not None and self.lease_epoch is not None and epoch != self.lease_epoch:
            raise SandboxValidationError(
                f"the sandbox handle was provisioned under lease epoch {epoch} but "
                f"the caller acts under epoch {self.lease_epoch}; reconnect under "
                "the current lease instead of reusing the old handle"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "binding_id": self.binding_id,
            "agent_role": self.role,
            "lease_epoch": self.lease_epoch,
            "envelope": self.envelope.model_dump(mode="json") if self.envelope else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SandboxAuthority:
        envelope = data.get("envelope")
        return cls(
            project_id=str(data.get("project_id", "")),
            binding_id=str(data.get("binding_id", "")),
            agent_role=str(data.get("agent_role", "")),
            lease_epoch=data.get("lease_epoch"),
            envelope=AuthorityEnvelope.model_validate(envelope) if envelope else None,
        )


@dataclass(frozen=True, slots=True)
class SandboxCeilings:
    """The deployment's production quota ceilings.

    These are configuration, not a project grant. The defaults are the T19
    card's numbers. A per-request `SandboxLimits` may be stricter; it may not
    be wider.
    """

    cpu_cores: float = 2.0
    memory_bytes: int = 4 * GIB
    pids: int = 256
    scratch_bytes: int = 10 * GIB
    timeout_seconds: float = 120.0

    def as_dict(self) -> dict[str, Any]:
        return SandboxLimits(
            cpu_cores=self.cpu_cores,
            memory_bytes=self.memory_bytes,
            pids=self.pids,
            scratch_bytes=self.scratch_bytes,
            timeout_seconds=self.timeout_seconds,
        ).as_dict()


def validate_limits(limits: SandboxLimits) -> None:
    """Refuse non-positive or absurd limits before they reach Docker.

    Docker accepts `--cpus 0` as "no limit" on some versions and rejects it on
    others; either answer is worse than a refusal. Zero scratch is likewise not
    a usable sandbox and would make every command fail with a confusing error.
    """
    if limits.cpu_cores <= 0:
        raise SandboxValidationError("cpu_cores must be greater than zero")
    if limits.memory_bytes < 16 * MIB:
        raise SandboxValidationError("memory_bytes must be at least 16 MiB")
    if limits.pids < 16:
        raise SandboxValidationError("pids must be at least 16")
    if limits.scratch_bytes < MIB:
        raise SandboxValidationError("scratch_bytes must be at least 1 MiB")
    if limits.timeout_seconds <= 0:
        raise SandboxValidationError("timeout_seconds must be greater than zero")


def resolve_limits(
    requested: SandboxLimits | None,
    ceilings: SandboxCeilings,
    authority: SandboxAuthority | None = None,
) -> SandboxLimits:
    """The limits a sandbox will actually be given.

    `requested` is checked against the ceiling first and refused if wider.
    Then the authority envelope, when it carries a wall-clock limit, may reduce
    the command timeout. The result is the intersection, so every field is
    either the request or something smaller.

    Raises:
        SandboxValidationError: A requested value is non-positive, or exceeds
            the configured production ceiling.
    """
    requested = requested or SandboxLimits(
        cpu_cores=ceilings.cpu_cores,
        memory_bytes=ceilings.memory_bytes,
        pids=ceilings.pids,
        scratch_bytes=ceilings.scratch_bytes,
        timeout_seconds=ceilings.timeout_seconds,
    )
    validate_limits(requested)

    comparisons = (
        ("cpu_cores", requested.cpu_cores, ceilings.cpu_cores),
        ("memory_bytes", requested.memory_bytes, ceilings.memory_bytes),
        ("pids", requested.pids, ceilings.pids),
        ("scratch_bytes", requested.scratch_bytes, ceilings.scratch_bytes),
        ("timeout_seconds", requested.timeout_seconds, ceilings.timeout_seconds),
    )
    for name, value, ceiling in comparisons:
        if value > ceiling:
            raise SandboxValidationError(
                f"{name}={value} exceeds this deployment's sandbox ceiling {ceiling}; "
                "a sandbox may be narrower than the ceiling, never wider"
            )

    timeout = requested.timeout_seconds
    envelope = authority.envelope if authority is not None else None
    if envelope is not None:
        wall_clock = envelope.budget_time_limits.max_wall_clock_hours
        if wall_clock is not None:
            timeout = min(timeout, wall_clock * 3600.0)
        compute = envelope.budget_time_limits.max_compute_hours
        if compute is not None:
            # A compute budget is CPU-hours, not wall clock. A single command
            # cannot consume more than its wall clock times its cores, so a
            # budget smaller than that product is a real bound on this command,
            # and dividing by the cores is the wall-clock form of it. The
            # comparison uses the request's CPU count, so a budget for less
            # than one core-second still yields a small positive timeout.
            cpu_seconds = compute * 3600.0
            timeout = min(timeout, cpu_seconds / max(requested.cpu_cores, 0.001))

    if timeout <= 0:
        raise SandboxValidationError(
            "the authority envelope leaves no wall-clock time for a sandbox command"
        )

    return SandboxLimits(
        cpu_cores=requested.cpu_cores,
        memory_bytes=requested.memory_bytes,
        pids=requested.pids,
        scratch_bytes=requested.scratch_bytes,
        timeout_seconds=timeout,
    )

