"""Bounded model turns: persistent retry budget, cooldown, and escalation.

A model turn that is not answered is not a reason to end a project, and it is
also not a reason to keep asking forever. This module is the budget between
those two: three attempts per round, three rounds, a 60-second cooldown after
the first round and a 300-second cooldown after the second, and an operator
gate after the ninth attempt.

**The budget is a row, not a counter in the loop.** A `ProjectLoop` is a
process-local object; a supervisor restart builds a new one, and an in-memory
count would begin again at zero while the condition that produced the calls was
still true. `retry_episodes` is keyed by *(project, question fingerprint,
scientific state version)*, so the same unanswered question finds the same row
after a restart and keeps its spent budget. That is the whole of AC1: the
counter is authoritative because PostgreSQL is.

**A timer never unlocks anything.** Cooldown is a delay between attempts within
the nine-call budget, never a reset of it — after the cooldown expires the next
attempt is attempt four, not attempt one. Once the ninth attempt has been made
the episode is `AWAITING_OPERATOR`, and no amount of time moves it: waiting is
not a scientific event. Two things do unlock it. An operator can say "try
again", which retires the episode, records who said so, and opens a replacement
with a fresh budget. Or the scientific state the question is about can change —
a new verdict, a contract reference, a node status, an artifact — which gives
the question a new *version* and therefore a new episode. The version is
computed from the situation the loop read, so both paths are the same rule:
a new episode starts when the thing being asked about is no longer the same
thing.

**A long HPC run or a human-lab wait is not a model stall.** Only the four
questions the loop puts to Master and Review are counted here. A node in
RUNNING or WAITING_EXTERNAL owes its seat one turn per episode; nobody is being
asked the same unanswered question once a poll. The clock advancing past a
timeout while a backend works is a wait, and it is deliberately outside this
budget.

**One escalation per episode.** The row that reaches `AWAITING_OPERATOR` is the
durable escalation, and `pending_escalations` is the projection a TUI or an
operator route reads: one row with one `escalation_event_id`, not a message per
tick. RAVEL's event vocabulary is closed and none of its types means "a runtime
stall", so the retry row — written in PostgreSQL, never deleted, and projected
exactly once — is the durable record rather than a mislabelled project event.

**Concurrency and turn time.** Two model requests per project and six across
the deployment, with a 180-second timeout on one turn. The caps belong to the
deployment, not to a loop, so one `ModelRequestLimiter` is shared by every loop
a supervisor drives; a per-loop semaphore would multiply by the number of
projects and bound nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from ravel.domain.clock import utcnow
from ravel.domain.enums import RetryEpisodeStatus, RetryQuestion
from ravel.domain.ids import new_id
from ravel.state.database import Database
from ravel.state.tables import RetryEpisodeRow

if TYPE_CHECKING:  # pragma: no cover - import cycle broken at runtime
    from ravel.execution.loop import Situation

__all__ = [
    "DEFAULT_COOLDOWNS",
    "AttemptState",
    "ModelRequestLimiter",
    "Reservation",
    "RetryEpisode",
    "RetryEpisodeStore",
    "RetryPolicy",
    "RetryWiring",
    "next_attempt_state",
    "problem_fingerprint",
    "question_of",
    "question_state_version",
    "turn_gate",
]

#: The cooldowns between rounds, as the task states them. Named so a caller
#: that has a reason to shorten them — the test environment, whose wall clock
#: cannot be accelerated — can say which values it is departing from.
DEFAULT_COOLDOWNS: tuple[float, ...] = (60.0, 300.0)

#: The statuses that mean "this episode is still the live one". The partial
#: unique index on the table is over exactly this set; a retired episode is
#: history and does not stand in the way of the replacement that follows it.
OPEN_EPISODE_STATUSES: tuple[RetryEpisodeStatus, ...] = (
    RetryEpisodeStatus.ACTIVE,
    RetryEpisodeStatus.COOLDOWN,
    RetryEpisodeStatus.AWAITING_OPERATOR,
)
_OPEN_VALUES: tuple[str, ...] = tuple(status.value for status in OPEN_EPISODE_STATUSES)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """The limits one question's unanswered turns are held to.

    The numbers are the task's, not tunables invented here: three attempts in
    a round, three rounds, 60 seconds between the first two rounds and 300
    between the second and third, nine attempts in total, and a 180-second
    turn timeout. Concurrency is a deployment property and travels with the
    limiter, but its defaults are here because a caller constructing a policy
    should not have to know two objects to find them.
    """

    attempts_per_round: int = 3
    rounds: int = 3
    cooldowns_seconds: tuple[float, ...] = DEFAULT_COOLDOWNS
    turn_timeout_seconds: float = 180.0
    per_project_requests: int = 2
    global_requests: int = 6

    def __post_init__(self) -> None:
        if self.attempts_per_round < 1:
            raise ValueError("attempts_per_round must be at least one")
        if self.rounds < 1:
            raise ValueError("rounds must be at least one")
        if len(self.cooldowns_seconds) != max(self.rounds - 1, 0):
            raise ValueError(
                "one cooldown per round boundary: expected "
                f"{max(self.rounds - 1, 0)}, got {len(self.cooldowns_seconds)}"
            )
        if any(seconds < 0 for seconds in self.cooldowns_seconds):
            raise ValueError("cooldowns cannot be negative")
        if self.turn_timeout_seconds <= 0:
            raise ValueError("turn_timeout_seconds must be positive")
        if self.per_project_requests < 1 or self.global_requests < 1:
            raise ValueError("request limits must be at least one")

    @property
    def max_attempts(self) -> int:
        """The whole budget: attempts per round times rounds."""
        return self.attempts_per_round * self.rounds

    def cooldown_after(self, round_number: int) -> timedelta | None:
        """The wait after finishing `round_number`, or `None` after the last."""
        if round_number >= self.rounds:
            return None
        return timedelta(seconds=self.cooldowns_seconds[round_number - 1])

    def round_of(self, attempt: int) -> int:
        """Which round an attempt number belongs to, counting from one."""
        return (attempt - 1) // self.attempts_per_round + 1


def now_utc() -> datetime:
    """The clock a wiring uses unless a test supplies its own."""
    return utcnow()


def problem_fingerprint(problem_key: str) -> str:
    """The stable identity of one question, independent of its prose.

    A digest rather than the key itself because the key is `what:ids` and can
    be long; what the row needs is an exact, indexable identity, and the full
    key is stored beside it for a person reading the table.
    """
    return hashlib.sha256(problem_key.encode("utf-8")).hexdigest()


def question_of(problem_key: str) -> RetryQuestion:
    """The closed question kind a loop key names.

    Raises:
        ValueError: The key does not begin with a known question. A loop that
            invented a fifth kind would otherwise be recorded under a value the
            database check constraint refuses at write time, which is a worse
            place to learn it.
    """
    return RetryQuestion(problem_key.partition(":")[0])


@dataclass(frozen=True, slots=True)
class AttemptState:
    """Where an episode stands after its most recent attempt."""

    attempts: int
    round_number: int
    status: RetryEpisodeStatus
    cooldown_until: datetime | None


def next_attempt_state(
    attempts: int, now: datetime, policy: RetryPolicy
) -> AttemptState:
    """The state an episode enters after attempt number `attempts`.

    Pure, and the only place the policy's arithmetic lives: the store applies
    the answer, and a test can assert the sequence 1..9 without a database.
    """
    if attempts < 1:
        raise ValueError("attempts starts at one")
    if attempts >= policy.max_attempts:
        return AttemptState(
            attempts=attempts,
            round_number=policy.rounds,
            status=RetryEpisodeStatus.AWAITING_OPERATOR,
            cooldown_until=None,
        )
    round_number = policy.round_of(attempts)
    if attempts % policy.attempts_per_round == 0:
        cooldown = policy.cooldown_after(round_number)
        if cooldown is not None:
            return AttemptState(
                attempts=attempts,
                round_number=round_number,
                status=RetryEpisodeStatus.COOLDOWN,
                cooldown_until=now + cooldown,
            )
    return AttemptState(
        attempts=attempts,
        round_number=round_number,
        status=RetryEpisodeStatus.ACTIVE,
        cooldown_until=None,
    )


def turn_gate(
    status: RetryEpisodeStatus,
    cooldown_until: datetime | None,
    now: datetime,
) -> str:
    """Whether an episode's question may be dispatched now, and why not.

    Returns ``"dispatch"``, ``"cooldown"``, ``"awaiting_operator"`` or
    ``"retired"``. A cooldown that has expired is a dispatch: the timer delays
    the next attempt within the budget, and the attempt it admits is still the
    next one. `AWAITING_OPERATOR` is never a dispatch, however long the clock
    has run — that is the difference between a cooldown and an ending.
    """
    if status is RetryEpisodeStatus.RETIRED:
        return "retired"
    if status is RetryEpisodeStatus.AWAITING_OPERATOR:
        return "awaiting_operator"
    if (
        status is RetryEpisodeStatus.COOLDOWN
        and cooldown_until is not None
        and now < cooldown_until
    ):
        return "cooldown"
    return "dispatch"


def _compare_key(moment: datetime | None) -> str:
    """A timestamp as a sortable string for the state digest."""
    return "" if moment is None else moment.isoformat()


def question_state_version(situation: Situation, problem_key: str) -> str:
    """A digest of the scientific state one question is being asked about.

    The version is scoped to the question: a `pre-flight` question is about the
    nodes it names, an `ending` is about the whole project, and an unrelated
    node finishing elsewhere does not give a stalled question a fresh budget.
    What is included is what a scientific answer could turn on — the project's
    status; each participating node's status, type, contract references,
    decision, artifact references and dependencies; the pre-run verdict it was
    last given; and the participating open deviations.

    It is deliberately *not* the retry bookkeeping itself. If the version were
    derived from the episode's attempt count, every attempt would unlock the
    next one and the budget would be circular.
    """
    what, _, rest = problem_key.partition(":")
    named = {part for part in rest.split(",") if part}
    nodes = [
        node
        for node in situation.nodes
        if not named or node.node_id in named
    ]
    deviations = [
        deviation
        for deviation in situation.open_deviations
        if not named or deviation.deviation_id in named
    ]

    digest = hashlib.sha256()
    digest.update(b"ravel-retry-state-v1\n")
    digest.update(f"question={what}\n".encode())
    digest.update(f"project={situation.project.status.value}\n".encode())

    for node in sorted(nodes, key=lambda item: item.node_id):
        outcome = situation.pre_run.get(node.node_id)
        fields = (
            node.node_id,
            node.node_type.value,
            node.status.value,
            node.acceptance_contract_ref or "",
            node.execution_contract_ref or "",
            node.decision_ref or "",
            ",".join(sorted(node.dependencies)),
            ",".join(node.artifact_refs),
            outcome.value if outcome is not None else "",
        )
        digest.update("\x1f".join(fields).encode("utf-8"))
        digest.update(b"\n")

    for deviation in sorted(deviations, key=lambda item: item.deviation_id):
        digest.update(
            "\x1f".join(
                (
                    deviation.deviation_id,
                    deviation.node_id,
                    deviation.execution_contract_ref,
                    deviation.resolved_by_decision_ref or "",
                    _compare_key(deviation.resolved_at),
                )
            ).encode("utf-8")
        )
        digest.update(b"\n")

    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class Reservation:
    """The answer to "may this attempt be made now".

    A blocked reservation carries no attempt: the row was not advanced, and the
    caller must not advance it either. A dispatched one was advanced *before*
    the model call on purpose — a process that dies during the call has still
    spent the attempt, which is what makes the nine-call ceiling hold across
    restarts rather than being a hope about clean shutdowns.
    """

    dispatch: bool
    reason: str
    episode_id: str
    question: RetryQuestion
    problem_key: str
    state_version: str
    attempts: int
    round_number: int
    cooldown_until: datetime | None
    escalated_at: datetime | None
    escalation_event_id: str | None


@dataclass(frozen=True, slots=True)
class RetryEpisode:
    """One persisted episode, as read back for a projection or a test."""

    episode_id: str
    project_id: str
    question: RetryQuestion
    problem_key: str
    fingerprint: str
    state_version: str
    status: RetryEpisodeStatus
    attempts: int
    round_number: int
    cooldown_until: datetime | None
    escalated_at: datetime | None
    escalation_event_id: str | None
    operator_actor: str | None
    operator_retried_at: datetime | None
    retired_reason: str | None
    retired_at: datetime | None
    last_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: RetryEpisodeRow) -> RetryEpisode:
        return cls(
            episode_id=row.episode_id,
            project_id=row.project_id,
            question=RetryQuestion(row.question),
            problem_key=row.problem_key,
            fingerprint=row.fingerprint,
            state_version=row.state_version,
            status=RetryEpisodeStatus(row.status),
            attempts=row.attempts,
            round_number=row.round_number,
            cooldown_until=row.cooldown_until,
            escalated_at=row.escalated_at,
            escalation_event_id=row.escalation_event_id,
            operator_actor=row.operator_actor,
            operator_retried_at=row.operator_retried_at,
            retired_reason=row.retired_reason,
            retired_at=row.retired_at,
            last_attempt_at=row.last_attempt_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_EPISODE_STATUSES


class RetryEpisodeStore:
    """The authoritative retry budget, one row per open question.

    Every method takes `now` rather than reading a clock: the caller's clock is
    the loop's, tests inject a deterministic one, and the policy arithmetic is
    then reproducible without sleeping. The database writes are short
    transactions of their own; none of them spans a model call.
    """

    def __init__(self, database: Database, policy: RetryPolicy | None = None) -> None:
        self.database = database
        self.policy = policy or RetryPolicy()

    # ── The hot path ───────────────────────────────────────────────────────

    def reserve(
        self,
        *,
        project_id: str,
        problem_key: str,
        state_version: str,
        now: datetime,
    ) -> Reservation:
        """Advance one question's episode, or say why it may not advance.

        Transactionally: the read takes the episode row's lock, so two loops
        that somehow drove one project cannot both be admitted for the ninth
        attempt. Superseding a version first is what makes a scientific state
        change the unlock — the old episode is retired in the same transaction
        that opens its replacement, so there is never a moment with two open
        episodes for one question.
        """
        question = question_of(problem_key)
        fingerprint = problem_fingerprint(problem_key)

        with self.database.transaction() as session:
            superseded = (
                session.execute(
                    select(RetryEpisodeRow)
                    .where(
                        RetryEpisodeRow.project_id == project_id,
                        RetryEpisodeRow.fingerprint == fingerprint,
                        RetryEpisodeRow.state_version != state_version,
                        RetryEpisodeRow.status.in_(_OPEN_VALUES),
                    )
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            for stale in superseded:
                self._retire(stale, reason="state_version_changed", now=now)

            row = (
                session.execute(
                    select(RetryEpisodeRow)
                    .where(
                        RetryEpisodeRow.project_id == project_id,
                        RetryEpisodeRow.fingerprint == fingerprint,
                        RetryEpisodeRow.state_version == state_version,
                        RetryEpisodeRow.status.in_(_OPEN_VALUES),
                    )
                    .with_for_update()
                )
                .scalar_one_or_none()
            )
            if row is None:
                row = RetryEpisodeRow(
                    episode_id=new_id(),
                    project_id=project_id,
                    question=question.value,
                    problem_key=problem_key,
                    fingerprint=fingerprint,
                    state_version=state_version,
                    status=RetryEpisodeStatus.ACTIVE.value,
                    attempts=0,
                    round_number=1,
                    cooldown_until=None,
                    escalated_at=None,
                    escalation_event_id=None,
                    operator_actor=None,
                    operator_retried_at=None,
                    retired_reason=None,
                    retired_at=None,
                    last_attempt_at=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()

            status = RetryEpisodeStatus(row.status)
            reason = turn_gate(status, row.cooldown_until, now)
            if reason != "dispatch":
                return self._reservation(row, dispatch=False, reason=reason)

            if status is RetryEpisodeStatus.COOLDOWN:
                # The timer has run out. The budget does not: the attempt
                # recorded below is the next one in the same nine-call episode.
                row.status = RetryEpisodeStatus.ACTIVE.value
                row.cooldown_until = None

            result = next_attempt_state(row.attempts + 1, now, self.policy)
            row.attempts = result.attempts
            row.round_number = result.round_number
            row.status = result.status.value
            row.cooldown_until = result.cooldown_until
            row.last_attempt_at = now
            row.updated_at = now
            if result.status is RetryEpisodeStatus.AWAITING_OPERATOR:
                row.escalated_at = now
                if row.escalation_event_id is None:
                    # The durable escalation's identity. One row, one id, one
                    # pending item — a consumer that has seen this id has seen
                    # this episode's notification.
                    row.escalation_event_id = new_id()
            return self._reservation(row, dispatch=True, reason="dispatch")

    def retire_unasked(
        self,
        *,
        project_id: str,
        problem_keys: set[str],
        now: datetime,
    ) -> int:
        """Retire live episodes for questions this round is no longer asking.

        A question ends when its condition stops holding — a node was cleared, a
        verdict was submitted, Master ended the project. Its budget goes with
        it: the same key returning later is a new question about a new state,
        and inheriting the old episode's spent attempts would punish a project
        for having been stuck before. Returns how many rows were retired.
        """
        with self.database.transaction() as session:
            statement = select(RetryEpisodeRow).where(
                RetryEpisodeRow.project_id == project_id,
                RetryEpisodeRow.status.in_(_OPEN_VALUES),
            )
            if problem_keys:
                statement = statement.where(
                    RetryEpisodeRow.problem_key.not_in(problem_keys)
                )
            rows = session.execute(statement.with_for_update()).scalars().all()
            for row in rows:
                self._retire(row, reason="question_ended", now=now)
            return len(rows)

    # ── The operator and the projection ────────────────────────────────────

    def pending_escalations(self, project_id: str) -> list[RetryEpisode]:
        """The episodes waiting on a person, one per stalled question.

        This is the "pending item" half of the escalation: the loop keeps
        polling, but it dispatches nothing for these, and a reader that renders
        this list renders each stall once because each episode appears once.
        """
        with self.database.read_only() as session:
            rows = (
                session.execute(
                    select(RetryEpisodeRow)
                    .where(
                        RetryEpisodeRow.project_id == project_id,
                        RetryEpisodeRow.status
                        == RetryEpisodeStatus.AWAITING_OPERATOR.value,
                    )
                    .order_by(
                        RetryEpisodeRow.created_at, RetryEpisodeRow.episode_id
                    )
                )
                .scalars()
                .all()
            )
            return [RetryEpisode.from_row(row) for row in rows]

    def episodes(
        self, project_id: str, *, include_retired: bool = False
    ) -> list[RetryEpisode]:
        """Every episode for a project, oldest first — for a projection or a test."""
        with self.database.read_only() as session:
            statement = select(RetryEpisodeRow).where(
                RetryEpisodeRow.project_id == project_id
            )
            if not include_retired:
                statement = statement.where(
                    RetryEpisodeRow.status.in_(_OPEN_VALUES)
                )
            rows = (
                session.execute(
                    statement.order_by(
                        RetryEpisodeRow.created_at, RetryEpisodeRow.episode_id
                    )
                )
                .scalars()
                .all()
            )
            return [RetryEpisode.from_row(row) for row in rows]

    def operator_retry(
        self,
        *,
        project_id: str,
        actor: str,
        now: datetime,
        question: RetryQuestion | None = None,
    ) -> list[RetryEpisode]:
        """An operator says "ask again": retire the episode and open a new one.

        The actor and the moment are recorded on the retired row, which is the
        record of who intervened, and carried onto the replacement as
        provenance. Only an explicit call reaches this path — nothing in the
        loop or the supervisor retries on a person's behalf, which is what
        makes an operator retry a decision rather than a timeout with a nicer
        name.

        Returns the replacement episodes, oldest question first.
        """
        if not actor.strip():
            raise ValueError("an operator retry must name the actor")
        with self.database.transaction() as session:
            statement = select(RetryEpisodeRow).where(
                RetryEpisodeRow.project_id == project_id,
                RetryEpisodeRow.status.in_(_OPEN_VALUES),
            )
            if question is not None:
                statement = statement.where(
                    RetryEpisodeRow.question == question.value
                )
            stalled = (
                session.execute(
                    statement.order_by(
                        RetryEpisodeRow.created_at, RetryEpisodeRow.episode_id
                    ).with_for_update()
                )
                .scalars()
                .all()
            )
            replacement_rows: list[RetryEpisodeRow] = []
            for row in stalled:
                self._retire(
                    row,
                    reason="operator_retry",
                    now=now,
                    actor=actor,
                )
                replacement = RetryEpisodeRow(
                    episode_id=new_id(),
                    project_id=row.project_id,
                    question=row.question,
                    problem_key=row.problem_key,
                    fingerprint=row.fingerprint,
                    state_version=row.state_version,
                    status=RetryEpisodeStatus.ACTIVE.value,
                    attempts=0,
                    round_number=1,
                    cooldown_until=None,
                    escalated_at=None,
                    escalation_event_id=None,
                    operator_actor=actor,
                    operator_retried_at=None,
                    retired_reason=None,
                    retired_at=None,
                    last_attempt_at=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(replacement)
                replacement_rows.append(replacement)
            session.flush()
            return [RetryEpisode.from_row(row) for row in replacement_rows]

    # ── Internals ──────────────────────────────────────────────────────────

    @staticmethod
    def _retire(
        row: RetryEpisodeRow,
        *,
        reason: str,
        now: datetime,
        actor: str | None = None,
    ) -> None:
        row.status = RetryEpisodeStatus.RETIRED.value
        row.retired_reason = reason
        row.retired_at = now
        row.cooldown_until = None
        row.updated_at = now
        if actor is not None:
            row.operator_actor = actor
            row.operator_retried_at = now

    @staticmethod
    def _reservation(
        row: RetryEpisodeRow, *, dispatch: bool, reason: str
    ) -> Reservation:
        return Reservation(
            dispatch=dispatch,
            reason=reason,
            episode_id=row.episode_id,
            question=RetryQuestion(row.question),
            problem_key=row.problem_key,
            state_version=row.state_version,
            attempts=row.attempts,
            round_number=row.round_number,
            cooldown_until=row.cooldown_until,
            escalated_at=row.escalated_at,
            escalation_event_id=row.escalation_event_id,
        )


class ModelRequestLimiter:
    """The deployment's cap on concurrent model requests.

    Two per project and six across the deployment, acquired together: a
    project's second concurrent turn may use the deployment's sixth slot, and a
    seventh project must wait for one of the first. The global semaphore is
    taken last and released first, so no holder of the global slot can be
    waiting on a per-project one — which is what would be needed for the two
    limits to deadlock each other.
    """

    def __init__(self, *, per_project: int, global_requests: int) -> None:
        if per_project < 1 or global_requests < 1:
            raise ValueError("request limits must be at least one")
        self.per_project = per_project
        self.global_requests = global_requests
        self._project_slots: dict[str, asyncio.Semaphore] = {}
        self._global = asyncio.Semaphore(global_requests)

    @asynccontextmanager
    async def slot(self, project_id: str) -> AsyncIterator[None]:
        """Hold one per-project and one global slot for the block's duration."""
        project = self._project_slots.get(project_id)
        if project is None:
            project = asyncio.Semaphore(self.per_project)
            self._project_slots[project_id] = project
        async with project, self._global:
            yield


@dataclass(frozen=True, slots=True)
class RetryWiring:
    """Everything a loop needs to make its turns bounded and observable.

    One object rather than four optional fields on `ProjectLoop`, because they
    are only meaningful together: a store without its policy would apply some
    other budget, and a limiter without a store would bound concurrency while
    letting the ninth attempt be followed by a tenth. `None` on the loop is the
    unwired path — the local one-shot script and the existing unit tests — where
    the loop's in-memory question budget still applies and nothing here is
    consulted.
    """

    policy: RetryPolicy
    store: RetryEpisodeStore
    limiter: ModelRequestLimiter
    clock: Callable[[], datetime]

    @classmethod
    def create(
        cls,
        database: Database,
        *,
        policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> RetryWiring:
        """Build the store and limiter from one policy.

        Constructed together so the cooldowns the store enforces and the
        timeout the loop applies cannot come from different policies.
        """
        resolved = policy or RetryPolicy()
        return cls(
            policy=resolved,
            store=RetryEpisodeStore(database, resolved),
            limiter=ModelRequestLimiter(
                per_project=resolved.per_project_requests,
                global_requests=resolved.global_requests,
            ),
            clock=clock or now_utc,
        )

    def now(self) -> datetime:
        """The wiring's clock."""
        return self.clock()
