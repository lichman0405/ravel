"""Slurm's vocabulary, and how RAVEL reads it.

A cluster says a job is `OUT_OF_MEMORY` or `NODE_FAIL` or `CANCELLED by 1000`.
RAVEL says a job is FAILED and says whether trying again is worth anything. The
two are kept side by side rather than translated away — `JobStatus.backend_state`
carries Slurm's word and `JobStatus.state` carries RAVEL's — because a reader
comparing them is how a wrong translation gets found.

**Two rules decide every row, and they are worth stating separately** because
one of them is a scientific position rather than an engineering one.

*Who failed.* `NODE_FAIL`, `BOOT_FAIL` and `PREEMPTED` are the machine's fault.
The work never had a chance to produce a wrong answer, so the attempt is worth
repeating: `INFRA_RETRYABLE`. `FAILED` and `OUT_OF_MEMORY` are the work's — the
job ran and did not get through — and repeating it would be repeating the same
experiment hoping for a different result: `NON_RETRYABLE`.

*What a resource limit means.* `TIMEOUT` and `DEADLINE` do not become retryable
by being the scheduler's decision. A wall-clock limit is a **term of the
contract**: Master set it, Review approved it, and the run exceeded it. The
honest reading is that the work does not fit inside the terms it was given —
which is a fact about the work, not a fault in the cluster, and the one role
that can change a contract term is Master. RAVEL reports `TIMED_OUT` and
`NON_RETRYABLE`, Master reads it, and Master decides. A backend that quietly
retried would be spending a second allocation to ask the same question.

**An unrecognised state makes no claim.** A Slurm whose version adds a state, or
an administrator who configures a site-specific one, gets `FAILED` with
`failure_class=None` and Slurm's raw word in the detail. RAVEL reads `None` as
non-retryable, which is the conservative reading, and the detail says what was
actually seen so that the missing row is obvious rather than mysterious.
"""

from __future__ import annotations

from dataclasses import dataclass

from ravel.domain.enums import FailureClass, JobState

#: What RAVEL reads each Slurm state as: the state, the class of failure if the
#: job is over and went wrong, and one sentence for a reader.
#:
#: Slurm's own documentation lists these; the comment against each group says
#: which rule above put it there.
SLURM_STATES: dict[str, tuple[JobState, FailureClass | None, str]] = {
    # Not started. `REQUEUED` is here rather than terminal because a requeued
    # job is waiting again — it has not ended, it has been moved to the back.
    "PENDING": (JobState.SUBMITTED, None, "the job is queued and has not started"),
    "CONFIGURING": (JobState.SUBMITTED, None, "the job's nodes are being prepared"),
    "REQUEUED": (JobState.SUBMITTED, None, "the job was put back in the queue"),
    "RESIZING": (JobState.SUBMITTED, None, "the job's allocation is being changed"),
    # Started. `COMPLETING` and `STOPPED` and `SUSPENDED` are all states of a
    # job that has run, which is the distinction RAVEL draws.
    "RUNNING": (JobState.RUNNING, None, "the job is running"),
    "COMPLETING": (JobState.RUNNING, None, "the job has finished and is being tidied up"),
    "STOPPED": (JobState.RUNNING, None, "the job was stopped by the scheduler and may resume"),
    "SUSPENDED": (JobState.RUNNING, None, "the job is suspended and may resume"),
    # Ended well.
    "COMPLETED": (JobState.COMPLETED, None, "the job completed"),
    # Ended badly, and the work is what went wrong.
    "FAILED": (JobState.FAILED, FailureClass.NON_RETRYABLE, "the job failed"),
    "OUT_OF_MEMORY": (
        JobState.FAILED,
        FailureClass.NON_RETRYABLE,
        "the job was killed for using more memory than it was given",
    ),
    "SPECIAL_EXIT": (
        JobState.FAILED,
        FailureClass.NON_RETRYABLE,
        "the job ended in a state an administrator configured",
    ),
    # Ended badly, and the machine is what went wrong. Repeating the attempt
    # asks the same question of a different node.
    "NODE_FAIL": (JobState.FAILED, FailureClass.INFRA_RETRYABLE, "the node failed"),
    "BOOT_FAIL": (JobState.FAILED, FailureClass.INFRA_RETRYABLE, "the node failed to boot"),
    "PREEMPTED": (
        JobState.FAILED,
        FailureClass.INFRA_RETRYABLE,
        "the job was preempted by a higher-priority job",
    ),
    "REVOKED": (
        JobState.FAILED,
        FailureClass.INFRA_RETRYABLE,
        "the job was revoked when a higher-priority job was requeued",
    ),
    # A resource limit the contract set. See the module docstring: not retryable
    # and not a machine fault — the work did not fit inside its terms.
    "TIMEOUT": (
        JobState.TIMED_OUT,
        FailureClass.NON_RETRYABLE,
        "the job reached the wall-clock limit of its allocation",
    ),
    "DEADLINE": (
        JobState.TIMED_OUT,
        FailureClass.NON_RETRYABLE,
        "the job reached the deadline its queue sets",
    ),
    # Stopped by somebody. No failure class: nothing failed, and RAVEL's own
    # `decide_retry` stops on a cancelled job rather than reading a class.
    "CANCELLED": (JobState.CANCELLED, None, "the job was cancelled"),
}

#: What Slurm writes after a state to say who did it — `CANCELLED by 1000`,
#: `FAILED by user`. The word before it is the state.
_STATE_NOISE = (" by ",)


@dataclass(frozen=True, slots=True)
class SlurmReading:
    """What RAVEL made of one Slurm state string."""

    state: JobState
    #: RAVEL's word for what went wrong, or `None`. `None` on a terminal state
    #: means RAVEL will not retry it.
    failure_class: FailureClass | None
    #: The sentence for a reader — Slurm's own word when the state is not one
    #: this module knows, so that an unfamiliar state is visible rather than
    #: silently mapped onto the nearest one.
    detail: str
    #: Slurm's word, as it arrived, after normalisation.
    backend_state: str

    @property
    def known(self) -> bool:
        """Whether this module has a row for the state Slurm reported."""
        return self.backend_state in SLURM_STATES


def normalize_state(raw: str) -> str:
    """Slurm's state string reduced to the token the table is keyed by.

    Three things arrive that the table does not hold: a trailing `+`, which
    Slurm appends to mean "the state changed while you were looking"; a suffix
    naming who acted (`CANCELLED by 1000`); and mixed case, since `squeue -o
    %T` and `sacct -o State` differ. All three are noise around the same word,
    and normalising them here is what lets `SLURM_STATES` be a table a reader
    can check against Slurm's own list.
    """
    text = raw.strip().upper()
    for noise in _STATE_NOISE:
        if noise.upper() in text:
            text = text.split(noise.upper(), 1)[0]
    text = text.split(" ", 1)[0]
    return text.rstrip("+")


def read_state(raw: str) -> SlurmReading:
    """Read one Slurm state string in RAVEL's vocabulary.

    An unrecognised state is `FAILED` with no failure class and a detail that
    quotes what arrived. Guessing a mapping would be inventing a fact about a
    run; refusing to map at all would leave a workflow polling a job that will
    never move. Reporting a failure RAVEL cannot classify is the one answer
    that is both terminal and honest.
    """
    backend_state = normalize_state(raw)
    row = SLURM_STATES.get(backend_state)
    if row is None:
        return SlurmReading(
            state=JobState.FAILED,
            failure_class=None,
            detail=(
                f"the scheduler reported {raw.strip()!r}, which RAVEL does not "
                "recognise; it is reported as a failure with no class, which "
                "RAVEL does not retry"
            ),
            backend_state=backend_state,
        )
    state, failure_class, detail = row
    return SlurmReading(
        state=state, failure_class=failure_class, detail=detail, backend_state=backend_state
    )


def read_exit_code(allocation: str, batch: str) -> tuple[int | None, str]:
    """Read sacct's `ExitCode` pair as a number and a sentence.

    sacct spells an exit status `returned:signalled` — `0:0` is a clean exit,
    `1:0` is exit 1, `0:9` is a kill by signal 9. The batch step's code is the
    script's own, which is the one that says how the software ended; the
    allocation's is `0:0` for any job Slurm considers completed.

    Returns `(None, reason)` when the pair cannot be read, rather than a zero
    that would be indistinguishable from success.
    """
    for raw in (batch, allocation):
        text = raw.strip()
        if not text or ":" not in text:
            continue
        returned, _, signalled = text.partition(":")
        try:
            code = int(returned)
            signal_number = int(signalled)
        except ValueError:
            return None, f"the scheduler reported an exit code of {text!r}, which is not a number"
        if signal_number:
            return None, f"the job was killed by signal {signal_number}"
        return code, ""
    return None, "the scheduler reported no exit code"


__all__ = [
    "SLURM_STATES",
    "SlurmReading",
    "normalize_state",
    "read_exit_code",
    "read_state",
]
