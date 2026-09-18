"""The rolling horizon, and the work it is derived from.

RAVEL plans coarsely far ahead and concretely only a little ahead. These tests
assert the second half of that: which stages Master may commit executable nodes
to, and how the horizon moves as the DAG progresses.

Nothing here touches a database. The horizon is a function of the roadmap and
the per-stage work counts, and asserting it as a pure function is what keeps the
integration tests about persistence rather than about arithmetic.
"""

from __future__ import annotations

import pytest

from ravel.domain.planning import (
    HORIZON_LOOKAHEAD,
    HorizonCheck,
    HorizonError,
    PhaseWork,
    PlanningHorizon,
    planning_horizon,
)
from ravel.domain.project import RoadmapPhase

PROJECT = "proj-a"


def _phase(name: str, order: int) -> RoadmapPhase:
    return RoadmapPhase(project_id=PROJECT, name=name, order=order)


def _roadmap(*names: str) -> tuple[RoadmapPhase, ...]:
    return tuple(_phase(name, order) for order, name in enumerate(names))


def _work(**counts: int) -> dict[str, PhaseWork]:
    """Work counts keyed by stage name: `_work(s1=3, s2=0)`.

    A stage is named here only when it has nodes; a stage absent from the
    mapping has never been expanded, which is a different fact from a stage
    whose nodes have all finished.
    """
    return {name: PhaseWork(nodes=count, unfinished=count) for name, count in counts.items()}


def _settled(**counts: int) -> dict[str, PhaseWork]:
    return {name: PhaseWork(nodes=count, unfinished=0) for name, count in counts.items()}


# ── What the horizon is ─────────────────────────────────────────────────────


def test_a_project_with_no_roadmap_has_no_horizon() -> None:
    """There is nothing to expand, and the refusal says so rather than crashing."""
    horizon = planning_horizon(())
    assert horizon.current is None
    assert horizon.reachable == ()
    check = horizon.check("Stage 1")
    assert not check.allowed
    assert "no roadmap phase" in check.reason


def test_the_horizon_reaches_the_current_stage_and_two_more() -> None:
    """The spec's "at most 1-2 next research stages", counted exactly."""
    horizon = planning_horizon(_roadmap("S1", "S2", "S3", "S4", "S5"), work=_work(S1=2))
    assert horizon.current is not None
    assert horizon.current.name == "S1"
    assert horizon.reachable_names == ("S1", "S2", "S3")
    assert "S4" not in horizon.reachable_names


def test_a_stage_with_unfinished_work_is_the_current_stage() -> None:
    horizon = planning_horizon(
        _roadmap("S1", "S2", "S3"),
        work={**_settled(S1=4), **_work(S2=3)},
    )
    assert horizon.current is not None
    assert horizon.current.name == "S2"


def test_the_horizon_advances_when_a_stage_settles() -> None:
    """Nothing advances it: the DAG settling is what advances it."""
    before = planning_horizon(_roadmap("S1", "S2", "S3"), work=_work(S1=2))
    after = planning_horizon(_roadmap("S1", "S2", "S3"), work={**_settled(S1=2), **_work(S2=1)})
    assert before.current is not None and before.current.name == "S1"
    assert after.current is not None and after.current.name == "S2"


def test_a_stage_that_has_not_been_expanded_at_all_is_the_current_stage() -> None:
    horizon = planning_horizon(_roadmap("S1", "S2"), work=_settled(S1=3))
    assert horizon.current is not None
    assert horizon.current.name == "S2"
    assert horizon.reachable_names == ("S2",)


def test_a_settled_stage_behind_the_project_is_closed() -> None:
    """The horizon looks forward from where the project is, not backward.

    Reopening a finished stage would rewrite what that stage consisted of after
    its results were already read, which is the same objection as editing a
    frozen acceptance contract. Follow-up work belongs to the stage the project
    is on now, or to a stage added after it.
    """
    horizon = planning_horizon(
        _roadmap("S1", "S2", "S3"), work={**_settled(S1=3), **_work(S2=1)}
    )
    assert horizon.reachable_names == ("S2", "S3")
    check = horizon.check("S1")
    assert not check.allowed
    assert "beyond the planning horizon" in check.reason


def test_the_horizon_stops_at_the_last_stage() -> None:
    """A project that exhausted its roadmap can still be given follow-up work.

    Without this, finishing every stage would freeze the project out of its own
    plan, and the only move left would be to invent a stage — which is a larger
    decision than adding one more node to the stage just finished.
    """
    horizon = planning_horizon(_roadmap("S1", "S2"), work=_settled(S1=2, S2=2))
    assert horizon.current is not None
    assert horizon.current.name == "S2"
    assert horizon.reachable_names == ("S2",)


def test_stages_are_read_in_order_whatever_order_they_arrive_in() -> None:
    shuffled = (_phase("S3", 2), _phase("S1", 0), _phase("S2", 1))
    horizon = planning_horizon(shuffled, work=_work(S1=1))
    assert horizon.names == ("S1", "S2", "S3")
    assert horizon.current is not None and horizon.current.name == "S1"


def test_a_stage_is_settled_by_cancelled_work_too() -> None:
    """A stage whose work was cancelled is a stage nobody is working on."""
    horizon = planning_horizon(_roadmap("S1", "S2"), work={**_settled(S1=2), **_work(S2=1)})
    assert horizon.current is not None
    assert horizon.current.name == "S2"


# ── What the horizon refuses ────────────────────────────────────────────────


def test_a_stage_beyond_the_horizon_is_refused() -> None:
    horizon = planning_horizon(_roadmap("S1", "S2", "S3", "S4"), work=_work(S1=1))
    check = horizon.check("S4")
    assert not check.allowed
    assert "beyond the planning horizon" in check.reason
    assert "S1, S2, S3" in check.reason


def test_a_stage_that_is_not_on_the_roadmap_is_refused_differently() -> None:
    """A typo and a premature plan are different mistakes and read differently."""
    horizon = planning_horizon(_roadmap("S1", "S2"), work=_work(S1=1))
    check = horizon.check("S9")
    assert not check.allowed
    assert "no roadmap phase named 'S9'" in check.reason
    assert "beyond the planning horizon" not in check.reason


def test_a_node_outside_any_stage_is_not_a_stage_commitment() -> None:
    """Some nodes are not stage work — a decision, a review of the whole route.

    The horizon governs what a stage is committed to; it has nothing to say
    about a node that claims no stage, and pretending otherwise would force
    every node into a stage it does not belong to.
    """
    horizon = planning_horizon(_roadmap("S1", "S2"), work=_work(S1=1))
    assert horizon.check(None).allowed


def test_require_raises_where_check_reports() -> None:
    horizon = planning_horizon(_roadmap("S1", "S2", "S3", "S4"), work=_work(S1=1))
    horizon.require("S1")
    with pytest.raises(HorizonError, match="beyond the planning horizon"):
        horizon.require("S4")


# ── Lookahead ───────────────────────────────────────────────────────────────


def test_lookahead_zero_confines_the_horizon_to_the_current_stage() -> None:
    horizon = planning_horizon(_roadmap("S1", "S2"), work=_work(S1=1), lookahead=0)
    assert horizon.reachable_names == ("S1",)
    assert not horizon.check("S2").allowed


@pytest.mark.parametrize("lookahead", [-1, HORIZON_LOOKAHEAD + 1, 10])
def test_a_lookahead_wider_than_the_spec_allows_is_refused(lookahead: int) -> None:
    """Asking for more is not a tuning choice; it is asking for no horizon."""
    with pytest.raises(ValueError, match="lookahead must be between"):
        planning_horizon(_roadmap("S1", "S2", "S3", "S4"), lookahead=lookahead)


# ── The pieces ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("work", "settled"),
    [
        (PhaseWork(), False),
        (PhaseWork(nodes=3, unfinished=0), True),
        (PhaseWork(nodes=3, unfinished=1), False),
        (PhaseWork(nodes=0, unfinished=0), False),
    ],
)
def test_a_stage_is_settled_only_when_it_was_planned_and_finished(
    work: PhaseWork, settled: bool
) -> None:
    assert work.is_settled is settled


def test_a_check_reports_why_it_denied() -> None:
    denial = HorizonCheck(False, "S4 is beyond the planning horizon")
    with pytest.raises(HorizonError, match="S4"):
        denial.raise_if_denied()
    HorizonCheck(True, "").raise_if_denied()


def test_an_empty_horizon_denies_every_named_stage() -> None:
    empty = PlanningHorizon()
    with pytest.raises(HorizonError):
        empty.require("S1")
