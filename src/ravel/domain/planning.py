"""The rolling horizon: how far ahead the executable plan may reach.

RAVEL plans at two resolutions. The roadmap is coarse and may run far ahead —
it says where the project is going. The Scientific DAG is concrete and may run
only a little ahead, because a detailed plan for stage five, written before
stage two has produced anything, is a guess wearing the clothes of a plan. The
spec states the limit directly:

    Master should fully expand current stage and at most 1-2 next research
    stages when confidence supports it.
    -- docs/03_SCIENTIFIC_DAG.md, section 3

This module is that sentence as a value. `planning_horizon` reads the roadmap
and what the DAG already holds and answers one question: may Master commit
concrete nodes to this stage yet.

**The horizon is derived, never stored.** There is no `expanded` flag on a
phase and no cursor on the project, because either would be a second copy of a
fact the DAG already carries, and a second copy is a thing that can disagree
with the first. A stage is under way exactly when it has nodes that have not
reached a terminal status; the horizon advances when that stops being true, and
nothing has to remember to advance it.

Nothing here touches the database. The function takes the roadmap and the
per-phase work counts and returns a value that can be printed, tested, and
quoted in the refusal a caller receives.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ravel.domain.project import RoadmapPhase

#: How many stages beyond the current one may be committed to concrete nodes.
#:
#: Two, because the spec says "at most 1-2 next research stages". It is a
#: ceiling rather than a default in spirit: a caller asking for more is not
#: making a tuning choice, it is asking RAVEL to abandon the rolling horizon.
HORIZON_LOOKAHEAD = 2


class HorizonError(ValueError):
    """A mutation would have committed concrete work beyond the horizon."""


@dataclass(frozen=True, slots=True)
class PhaseWork:
    """What the executable DAG currently holds for one roadmap phase."""

    nodes: int = 0
    #: Nodes not in a terminal status. A stage with work still in flight is the
    #: stage the project is on, whatever order it was given.
    unfinished: int = 0

    @property
    def is_settled(self) -> bool:
        """Whether this stage is behind the project.

        A stage is settled once it has been planned and everything planned for
        it has finished — passed, failed, or cancelled. All three are endings:
        a stage whose work was cancelled is not a stage still being worked on.
        """
        return self.nodes > 0 and self.unfinished == 0


@dataclass(frozen=True, slots=True)
class HorizonCheck:
    """The result of asking whether a stage may be expanded yet."""

    allowed: bool
    reason: str

    def raise_if_denied(self) -> None:
        """Turn a denial into an exception.

        Raises:
            HorizonError: The stage is outside the horizon.
        """
        if not self.allowed:
            raise HorizonError(self.reason)


@dataclass(frozen=True, slots=True)
class PlanningHorizon:
    """The part of the roadmap Master may currently commit nodes to."""

    #: Every phase, ordered. Empty when the project has no roadmap yet.
    phases: tuple[RoadmapPhase, ...] = ()
    #: Index into `phases` of the stage the project is on.
    current_index: int | None = None
    lookahead: int = HORIZON_LOOKAHEAD

    @property
    def current(self) -> RoadmapPhase | None:
        """The stage the project is on, or `None` if there is no roadmap."""
        return None if self.current_index is None else self.phases[self.current_index]

    @property
    def reachable(self) -> tuple[RoadmapPhase, ...]:
        """The stages that may be expanded: the current one and its lookahead."""
        if self.current_index is None:
            return ()
        return self.phases[self.current_index : self.current_index + self.lookahead + 1]

    @property
    def reachable_names(self) -> tuple[str, ...]:
        """The names of the expandable stages."""
        return tuple(phase.name for phase in self.reachable)

    @property
    def names(self) -> tuple[str, ...]:
        """The name of every stage on the roadmap."""
        return tuple(phase.name for phase in self.phases)

    def check(self, phase: str | None) -> HorizonCheck:
        """Whether a node may be committed to a stage."""
        if phase is None:
            return HorizonCheck(
                True,
                "the node is not claimed as work for any roadmap phase, so no "
                "stage is being planned ahead of its inputs",
            )
        if self.current is None:
            return HorizonCheck(
                False,
                f"this project has no roadmap phase named {phase!r}; commit the "
                "roadmap before expanding a stage into executable nodes",
            )
        if phase in self.reachable_names:
            return HorizonCheck(
                True, f"{phase!r} is inside the horizon of {self.current.name!r}"
            )
        if phase in self.names:
            return HorizonCheck(
                False,
                f"{phase!r} is beyond the planning horizon; RAVEL commits nodes to "
                f"{self.current.name!r} and at most {self.lookahead} stage(s) after it "
                f"({', '.join(self.reachable_names)}), so planning {phase!r} now would "
                "fix detail before the results that should shape it exist",
            )
        return HorizonCheck(
            False,
            f"this project has no roadmap phase named {phase!r}; it has "
            f"{', '.join(self.names) if self.names else 'none'}",
        )

    def require(self, phase: str | None) -> None:
        """Raise unless a node may be committed to a stage.

        Raises:
            HorizonError: The stage is outside the horizon.
        """
        self.check(phase).raise_if_denied()


def planning_horizon(
    phases: Sequence[RoadmapPhase],
    *,
    work: Mapping[str, PhaseWork] | None = None,
    lookahead: int = HORIZON_LOOKAHEAD,
) -> PlanningHorizon:
    """The horizon implied by a roadmap and the work already committed to it.

    The current stage is the first one that is not settled — either it has no
    nodes yet, or some of its nodes have not finished. If every stage is
    settled, the current stage is the last one, so a project that has exhausted
    its roadmap can still be given follow-up work on the stage it ended on
    rather than being frozen out of its own plan.

    Raises:
        ValueError: `lookahead` is outside `0..HORIZON_LOOKAHEAD`.
    """
    if not 0 <= lookahead <= HORIZON_LOOKAHEAD:
        raise ValueError(
            f"lookahead must be between 0 and {HORIZON_LOOKAHEAD}; a larger value "
            "is not a wider horizon, it is no horizon"
        )

    ordered = tuple(sorted(phases, key=lambda phase: phase.order))
    if not ordered:
        return PlanningHorizon(phases=(), current_index=None, lookahead=lookahead)

    progress = work or {}
    index = next(
        (
            position
            for position, phase in enumerate(ordered)
            if not progress.get(phase.name, PhaseWork()).is_settled
        ),
        len(ordered) - 1,
    )
    return PlanningHorizon(phases=ordered, current_index=index, lookahead=lookahead)
