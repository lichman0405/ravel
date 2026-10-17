"""What a preparation record is allowed to say.

The record is the only trace of the step between a frozen contract and a
started run, and it carries two very different things: a workspace that exists,
or a refusal and why. The invariant tested here is that it cannot carry the
wrong one — a refusal with no class cannot be routed to Master's next step, and
a preparation with no workspace is a Worker with nowhere to run. Both are
schema, so neither can be got wrong by an activity that forgets to check.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ravel.domain.preparation import (
    PreparationCheck,
    PreparationOutcome,
    PreparationRecord,
    PreparationRefusal,
)

#: Both outcomes, spelled out rather than taken from the enum, so that adding
#: a third is a deliberate act in this file too.
ALL_OUTCOMES = (PreparationOutcome.PREPARED, PreparationOutcome.REFUSED)

#: Both refusal classes, for the same reason.
ALL_REFUSALS = (
    PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
    PreparationRefusal.INCONSISTENT_CONTRACT,
    PreparationRefusal.UNSUPPORTED_ENVIRONMENT,
    PreparationRefusal.ENVIRONMENT_UNAVAILABLE,
)


def _prepared(**overrides: object) -> PreparationRecord:
    """A preparation that built something, with nothing else decided here."""
    fields: dict[str, object] = {
        "project_id": "proj-a",
        "node_id": "n1",
        "execution_contract_ref": "ctr-1",
        "execution_contract_version": 1,
        "outcome": PreparationOutcome.PREPARED,
        "materializer": "raspa",
        "materializer_version": "1",
        "workspace_path": "/runtime/prepared/proj-a/n1/v1",
        "required_outputs": ("results.txt",),
    }
    fields.update(overrides)
    return PreparationRecord(**fields)  # type: ignore[arg-type]


def _refused(**overrides: object) -> PreparationRecord:
    """A preparation that built nothing, and says why."""
    fields: dict[str, object] = {
        "project_id": "proj-a",
        "node_id": "n1",
        "execution_contract_ref": "ctr-1",
        "execution_contract_version": 1,
        "outcome": PreparationOutcome.REFUSED,
        "refusal": PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
        "reason": "the contract names no temperature for the run",
    }
    fields.update(overrides)
    return PreparationRecord(**fields)  # type: ignore[arg-type]


def test_the_outcomes_are_exactly_these_two() -> None:
    """A third outcome would be a third thing a reader has to handle.

    Comparing the written-out tuple against the enum makes adding one a
    deliberate act in this file rather than something a reader of the
    vocabulary discovers downstream.
    """
    assert tuple(ALL_OUTCOMES) == tuple(PreparationOutcome)


def test_a_prepared_record_names_the_workspace_it_built() -> None:
    record = _prepared()
    assert record.is_prepared
    assert record.workspace_path == "/runtime/prepared/proj-a/n1/v1"
    assert record.materializer == "raspa"
    assert record.refusal is None


def test_a_refusal_must_say_which_kind_it_was() -> None:
    """A refusal with no class is a refusal nothing can route."""
    with pytest.raises(ValidationError) as refused:
        _refused(refusal=None)
    assert "which kind of refusal" in str(refused.value)


def test_a_refusal_must_say_why_in_words() -> None:
    """Master reads this sentence; the class alone does not say what was missing."""
    with pytest.raises(ValidationError) as refused:
        _refused(reason="   ")
    assert "why in words" in str(refused.value)


def test_a_refusal_is_not_reported_as_a_workspace() -> None:
    """The two outcomes cannot both be true.

    A record that said `PREPARED` while carrying a refusal class would be read
    by a Worker as a directory to run in and by Master as a contract that could
    not be built, and whichever read it first would be the one that decided
    what happened.
    """
    with pytest.raises(ValidationError) as refused:
        _prepared(refusal=PreparationRefusal.INCONSISTENT_CONTRACT)
    assert "cannot carry the refusal" in str(refused.value)


def test_a_record_cannot_be_prepared_without_a_workspace() -> None:
    """There is no such thing as a preparation that built nothing and succeeded."""
    with pytest.raises(ValidationError) as refused:
        _prepared(workspace_path="")
    assert "must name the workspace" in str(refused.value)


def test_a_refusal_may_record_a_directory_it_had_begun_writing() -> None:
    """Allowed, and not required: a materializer that failed part way through
    knows where it was writing, and a record of that is more useful than a
    blank. What it may not do is claim the outcome was PREPARED."""
    record = _refused(workspace_path="/runtime/prepared/proj-a/n1/v1")
    assert not record.is_prepared
    assert record.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER


def test_a_prepared_record_names_the_code_that_built_it() -> None:
    """So that what it wrote can be told apart from what another one would write."""
    with pytest.raises(ValidationError) as refused:
        _prepared(materializer="")
    assert "materializer" in str(refused.value)


@pytest.mark.parametrize("refusal", ALL_REFUSALS)
def test_every_refusal_class_can_be_recorded(refusal: PreparationRefusal) -> None:
    """Each member is reachable, so none of them is decoration."""
    record = _refused(refusal=refusal)
    assert record.refusal is refusal
    assert not record.is_prepared


def test_the_record_is_immutable() -> None:
    """A preparation is a statement about a moment, like every other record."""
    record = _prepared()
    with pytest.raises(ValidationError):
        record.workspace_path = "/somewhere/else"  # type: ignore[misc]


def test_a_check_records_what_was_verified_and_not_only_that_it_passed() -> None:
    check = PreparationCheck(name="parameter_targets_within_ranges", passed=False, detail="x")
    assert not check.passed
    assert check.detail == "x"


def test_a_check_must_name_what_it_checked() -> None:
    with pytest.raises(ValidationError):
        PreparationCheck(name="", passed=True)
