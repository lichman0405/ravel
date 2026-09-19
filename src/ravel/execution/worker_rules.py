"""The rules a Worker applies without judging anything.

Every function here answers a question with a lookup rather than an opinion,
and putting them in one file is what makes that checkable. `docs/02_AGENT_MODEL.md`
§5 gives the Experimental Worker exactly one rule — *is this action explicitly
allowed by the Execution Contract?* — and a rule that simple should be
executable code rather than prose an agent re-derives, because prose drifts and
two agents reading it drift differently.

Two consequences follow from taking that seriously, and both are visible in the
signatures below:

**The Worker never asks whether a request is scientifically reasonable.** It
asks whether the contract named it. A deviation is therefore not a finding that
somebody proposed something wrong; it is the observation that the contract is
silent, which is a different statement and is recorded as one.

**A Worker never answers a question it was asked.** An operator asking "can
reagent A be replaced with B?" gets `ESCALATE`, not an answer, however obvious
the answer looks. Answering would be the Worker making a scientific decision,
and the three-way separation puts that with Master.

Nothing here touches the database or a model: it takes what a backend reported
and what the contract says, and returns a verdict with its reason attached.
"""

from __future__ import annotations

from dataclasses import dataclass

from ravel.domain.contracts import ExecutionContract
from ravel.domain.enums import WorkerMessageKind
from ravel.execution.backends import DeviationReport


@dataclass(frozen=True, slots=True)
class WorkerStatement:
    """Something a Worker says, with the kind of thing it is.

    The kind is one of the four in `WorkerMessageKind`, which is a closed list
    for the reason a Worker's vocabulary has to be closed at all: a Worker that
    can say anything can negotiate terms its contract never gave it.
    """

    kind: WorkerMessageKind
    body: str


@dataclass(frozen=True, slots=True)
class DeviationVerdict:
    """Whether the contract permits what a backend reported, and why.

    The reason travels with the verdict because it becomes the deviation's
    description, and "why did this stop" is the question Master has to answer
    next. A verdict that said only `False` would leave the reader to reconstruct
    which part of the contract was silent.
    """

    permitted: bool
    requested_action: str
    reason: str

    @property
    def statement(self) -> WorkerStatement:
        """What the Worker says about this verdict.

        A permitted report is confirmed and the run carries on; a refused one
        is escalated, because the only role that may widen a contract is the
        one that wrote it.
        """
        if self.permitted:
            return WorkerStatement(
                kind=WorkerMessageKind.CONFIRM,
                body=f"the execution contract permits {self.requested_action}",
            )
        return WorkerStatement(
            kind=WorkerMessageKind.ESCALATE,
            body=(
                f"the execution contract does not permit {self.requested_action}: "
                f"{self.reason}. This requires Master's decision; the Worker has "
                "not acted and has not answered the question."
            ),
        )


def adjudicate(
    report: DeviationReport, contract: ExecutionContract
) -> DeviationVerdict:
    """Whether the contract explicitly permits what a backend reported.

    Three checks, in the order of how specific the report is, and each is a
    lookup against a closed list:

    - a **substitution** is permitted only when the contract lists that exact
      pair;
    - a **parameter value** is permitted only when the contract declares a range
      for that parameter and the value falls inside it — and the value checked
      is the one the backend can actually reach, not the one it was asked for,
      because the reachable one is what would happen;
    - anything else is an **action**, permitted only when the contract names it.

    Silence is refusal throughout. A contract that does not mention a parameter
    has not permitted every value of it, and the `reason` says which silence was
    found so that Master can tell "not considered" from "considered and
    refused".
    """
    if report.substitution is not None:
        given, instead = report.substitution
        action = report.requested_action or f"SUBSTITUTE {given} -> {instead}"
        permitted = contract.permits_substitution(given, instead)
        reason = (
            f"the contract lists the substitution {given!r} -> {instead!r}"
            if permitted
            else f"the contract does not list the substitution {given!r} -> {instead!r}"
        )
    elif report.parameter and report.value is not None:
        action = report.requested_action or f"SET {report.parameter}"
        permitted = contract.permits_value(report.parameter, report.value)
        window = contract.permitted_range(report.parameter)
        # `window is not None` is redundant beside `permitted` — a value cannot
        # be inside a range that was never declared — but it is written out so
        # that the branch below reads the two ends without a type checker having
        # to reason about a function it cannot see into.
        if permitted and window is not None:
            reason = (
                f"the contract permits {report.parameter} in "
                f"{window[0]:g}..{window[1]:g} and {report.value:g} is inside it"
            )
        elif window is None:
            reason = f"the contract declares no range for {report.parameter}"
        else:
            reason = (
                f"the contract permits {report.parameter} in "
                f"{window[0]:g}..{window[1]:g} and {report.value:g} is outside it"
            )
    else:
        action = report.requested_action
        permitted = contract.permits(action)
        reason = (
            f"the contract lists the action {action!r}"
            if permitted
            else f"the contract does not list the action {action!r}"
        )

    if report.description:
        reason = f"{reason}; the backend reported: {report.description}"
    return DeviationVerdict(
        permitted=permitted, requested_action=action, reason=reason
    )


def on_incomplete_delivery(missing: tuple[str, ...]) -> WorkerStatement:
    """What a Worker says when required outputs did not arrive.

    `REQUEST_MISSING_INFORMATION` rather than `INFORM`: the Worker is asking for
    something specific and the answer changes what happens next. The names are
    in the body because "something is missing" is not actionable and the list is
    what the person supplying it needs.
    """
    return WorkerStatement(
        kind=WorkerMessageKind.REQUEST_MISSING_INFORMATION,
        body=(
            "the following required outputs were not delivered: "
            + ", ".join(missing)
            + ". Supply them, or confirm that they were not produced."
        ),
    )
