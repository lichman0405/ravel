"""`acceptance/MOCK_SCENARIOS.yaml`, read as typed values.

The catalogue is an acceptance artifact rather than a fixture: it names the
behaviours the mock backends must be able to produce, and the Phase 6 gate is
that each named scenario produces exactly the transition it specifies. Reading
it here, instead of restating it in Python, is what keeps the two from drifting
— a scenario added to the file with no behaviour behind it fails when the file
is read, which is the failure worth having, and it fails before anything runs.

**Unknown keys are refused.** The catalogue is small and hand-written, so a key
that no code recognises is either a typo or a scenario somebody believed was
implemented. Silently ignoring it would leave the file claiming a behaviour
RAVEL does not have.

Two shapes need explaining, because they are the catalogue's and not this
module's invention:

- `first_attempt` / `second_attempt` describe a scenario by how each attempt
  *ends* rather than by the states it passes through. The states before the
  ending are supplied here — a job that failed was submitted and ran first, and
  saying so costs nothing.
- `report: {requested_pressure_bar: 10, available_pressure_bar: 5}` names one
  quantity twice: what was asked for and what the lab can reach. The parameter
  is the name they share, and **the value that gets checked against the contract
  is the available one**, because that is what would be acted on. A catalogue
  entry whose two halves name different quantities is refused rather than
  guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ravel.config import REPO_ROOT
from ravel.domain.enums import FailureClass, JobState
from ravel.execution.backends import DeviationReport

#: Where the acceptance catalogue lives.
CATALOGUE_PATH = REPO_ROOT / "acceptance" / "MOCK_SCENARIOS.yaml"

#: The keys each kind of entry may carry. Everything else is refused.
_COMPUTE_KEYS = frozenset(
    {
        "id",
        "sequence",
        "outputs",
        "failure_class",
        "escalation_required",
        "first_attempt",
        "second_attempt",
        "requires_contract_retry_permission",
    }
)
_LAB_KEYS = frozenset(
    {
        "id",
        "wait_seconds_test_mode",
        "response",
        "response_after_external_signal",
        "report",
        "expected_worker_state",
        "first_delivery",
        "expected_action",
        "message",
        "contract_substitution_allowed",
    }
)

_TOP_LEVEL_KEYS = frozenset({"version", "compute", "experiment"})


class ScenarioError(ValueError):
    """Raised when the catalogue cannot be read as the scenarios it names."""


@dataclass(frozen=True, slots=True)
class ComputeScenario:
    """One named compute behaviour, as a sequence of states per attempt."""

    scenario_id: str
    #: The states attempt N passes through, in order. The last entry governs
    #: every attempt beyond the ones the catalogue names.
    sequences: tuple[tuple[JobState, ...], ...]
    failure_class: FailureClass | None
    delivers_complete_outputs: bool
    #: True when the catalogue says the failure may only be retried if the
    #: contract grants it. Read by the test that authors the contract, so the
    #: contract and the scenario cannot disagree about what is being tested.
    requires_contract_retry_permission: bool

    def sequence_for(self, attempt: int) -> tuple[JobState, ...]:
        """The states one attempt passes through.

        Raises:
            ValueError: The attempt number is below one.
        """
        if attempt < 1:
            raise ValueError(f"attempts are numbered from 1, not {attempt}")
        return self.sequences[min(attempt, len(self.sequences)) - 1]


@dataclass(frozen=True, slots=True)
class LabScenario:
    """One named laboratory behaviour."""

    scenario_id: str
    #: How long the lab takes before it answers. The catalogue's number is
    #: explicitly a test-mode one, so it is used as given rather than scaled.
    wait_seconds: float
    #: True when the lab will not finish until something outside RAVEL tells it
    #: to. The wait is then the scenario, not a duration.
    waits_for_signal: bool
    #: What the lab reports it was asked for and cannot do, if anything.
    report: DeviationReport | None
    #: Which of the contract's required outputs the lab delivers when it
    #: answers. A name mapped to false is one that was required and did not
    #: come.
    delivery: dict[str, bool]
    #: What an operator asked, when the scenario is an operator asking.
    operator_message: str
    #: What the catalogue says the Worker must end up doing.
    expected_worker_state: str
    expected_action: str
    #: Whether the contract is meant to permit the substitution in question.
    substitution_allowed: bool

    @property
    def delivered_outputs(self) -> tuple[str, ...]:
        """The required outputs this lab actually sends."""
        return tuple(name for name, sent in self.delivery.items() if sent)


@dataclass(frozen=True, slots=True)
class Catalogue:
    """Every scenario the acceptance run is allowed to ask for."""

    compute: dict[str, ComputeScenario]
    experiment: dict[str, LabScenario]

    def compute_scenario(self, scenario_id: str) -> ComputeScenario:
        """One compute scenario by name.

        Raises:
            KeyError: The catalogue names no such scenario.
        """
        return _named(self.compute, scenario_id, "compute")

    def lab_scenario(self, scenario_id: str) -> LabScenario:
        """One laboratory scenario by name.

        Raises:
            KeyError: The catalogue names no such scenario.
        """
        return _named(self.experiment, scenario_id, "experiment")


def _named(scenarios: dict[str, Any], scenario_id: str, kind: str) -> Any:
    try:
        return scenarios[scenario_id]
    except KeyError:
        raise KeyError(
            f"no {kind} scenario named {scenario_id!r}; the catalogue has "
            f"{sorted(scenarios) or 'none'}"
        ) from None


@lru_cache(maxsize=1)
def catalogue(path: Path | None = None) -> Catalogue:
    """The acceptance catalogue, read once.

    Cached because it is a checked-in file that cannot change under a running
    process, and because every mock construction asking the filesystem the same
    question would be a read per job for an answer that never differs.
    """
    resolved = path or CATALOGUE_PATH
    document = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ScenarioError(f"{resolved} does not contain a mapping")

    unknown = set(document) - _TOP_LEVEL_KEYS
    if unknown:
        raise ScenarioError(
            f"{resolved} has unrecognised top-level keys: {sorted(unknown)}"
        )

    return Catalogue(
        compute={
            entry["id"]: _compute_scenario(entry, resolved)
            for entry in _entries(document, "compute", resolved)
        },
        experiment={
            entry["id"]: _lab_scenario(entry, resolved)
            for entry in _entries(document, "experiment", resolved)
        },
    )


def _entries(document: dict[str, Any], section: str, path: Path) -> list[dict[str, Any]]:
    """The entries under one top-level section, each checked for a name."""
    raw = document.get(section, [])
    if not isinstance(raw, list):
        raise ScenarioError(f"{path}: {section!r} is not a list")
    for entry in raw:
        if not isinstance(entry, dict):
            raise ScenarioError(f"{path}: {section!r} contains a non-mapping entry")
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ScenarioError(f"{path}: a {section!r} entry has no id")
    return raw


def _known(entry: dict[str, Any], known: frozenset[str], path: Path) -> None:
    """Refuse a key nothing reads."""
    unknown = set(entry) - known
    if unknown:
        raise ScenarioError(
            f"{path}: scenario {entry['id']!r} has unrecognised keys "
            f"{sorted(unknown)}; either they are misspelled or nothing "
            "implements the behaviour they describe"
        )


def _compute_scenario(entry: dict[str, Any], path: Path) -> ComputeScenario:
    _known(entry, _COMPUTE_KEYS, path)
    failure = entry.get("failure_class")
    outputs = entry.get("outputs") or {}
    if not isinstance(outputs, dict):
        raise ScenarioError(f"{path}: {entry['id']!r} has a non-mapping 'outputs'")
    return ComputeScenario(
        scenario_id=entry["id"],
        sequences=_sequences(entry, path),
        failure_class=FailureClass(failure) if failure else None,
        delivers_complete_outputs=bool(outputs.get("complete", True)),
        requires_contract_retry_permission=bool(
            entry.get("requires_contract_retry_permission", False)
        ),
    )


def _sequences(
    entry: dict[str, Any], path: Path
) -> tuple[tuple[JobState, ...], ...]:
    """The states each attempt passes through, from either shape of entry."""
    if "sequence" in entry:
        return (_states(entry, entry["sequence"], path),)
    if "first_attempt" in entry and "second_attempt" in entry:
        # Written as what each attempt ends as. A job that ends was submitted
        # and ran first; the catalogue does not say so because nothing depends
        # on it, and this supplies it rather than leaving the sequence to begin
        # at its own ending.
        return tuple(
            (JobState.SUBMITTED, JobState.RUNNING, _state(entry, entry[name], path))
            for name in ("first_attempt", "second_attempt")
        )
    raise ScenarioError(
        f"{path}: compute scenario {entry['id']!r} has neither a 'sequence' nor a "
        "first/second attempt pair, so it describes no behaviour"
    )


def _states(
    entry: dict[str, Any], sequence: object, path: Path
) -> tuple[JobState, ...]:
    if not isinstance(sequence, list) or not sequence:
        raise ScenarioError(
            f"{path}: compute scenario {entry['id']!r} has an empty 'sequence'"
        )
    return tuple(_state(entry, name, path) for name in sequence)


def _state(entry: dict[str, Any], name: object, path: Path) -> JobState:
    try:
        return JobState(str(name))
    except ValueError:
        raise ScenarioError(
            f"{path}: compute scenario {entry['id']!r} names {name!r}, which is not "
            f"a job state; known states: {sorted(state.value for state in JobState)}"
        ) from None


def _lab_scenario(entry: dict[str, Any], path: Path) -> LabScenario:
    _known(entry, _LAB_KEYS, path)
    delivery = entry.get("first_delivery") or {}
    if not isinstance(delivery, dict):
        raise ScenarioError(f"{path}: {entry['id']!r} has a non-mapping 'first_delivery'")
    return LabScenario(
        scenario_id=entry["id"],
        wait_seconds=float(entry.get("wait_seconds_test_mode", 0)),
        waits_for_signal=bool(entry.get("response_after_external_signal", False)),
        report=_report(entry, path),
        delivery={str(name): bool(sent) for name, sent in delivery.items()},
        operator_message=str(entry.get("message", "")),
        expected_worker_state=str(entry.get("expected_worker_state", "")),
        expected_action=str(entry.get("expected_action", "")),
        substitution_allowed=bool(entry.get("contract_substitution_allowed", False)),
    )


def _report(entry: dict[str, Any], path: Path) -> DeviationReport | None:
    """The out-of-contract condition an entry describes, if it describes one."""
    fields = entry.get("report")
    if fields is not None:
        if not isinstance(fields, dict):
            raise ScenarioError(f"{path}: {entry['id']!r} has a non-mapping 'report'")
        return _range_report(entry, fields, path)

    message = entry.get("message")
    if not message:
        return None
    # An operator's question about a substitution. The pair is not in the
    # catalogue — the question is prose — so it is carried as the report's
    # description and matched against whatever the contract lists. A contract
    # that lists nothing permits nothing, which is what the scenario asserts.
    return DeviationReport(
        requested_action="SUBSTITUTE",
        description=str(message),
        substitution=_questioned_substitution(entry, str(message), path),
    )


def _range_report(
    entry: dict[str, Any], fields: dict[str, Any], path: Path
) -> DeviationReport:
    """A `requested_X` / `available_X` pair, as one parameter in question."""
    requested = {
        name[len("requested_") :]: value
        for name, value in fields.items()
        if name.startswith("requested_")
    }
    available = {
        name[len("available_") :]: value
        for name, value in fields.items()
        if name.startswith("available_")
    }
    if set(requested) != set(available) or len(requested) != 1:
        raise ScenarioError(
            f"{path}: {entry['id']!r} reports {sorted(fields)}, which is not one "
            "quantity named twice; write it as 'requested_<name>' beside "
            "'available_<name>'"
        )
    parameter = next(iter(requested))
    asked, reachable = requested[parameter], available[parameter]
    return DeviationReport(
        requested_action=f"SET {parameter}",
        description=(
            f"the contract asks for {parameter}={asked} and this lab can reach "
            f"{parameter}={reachable}"
        ),
        parameter=parameter,
        value=float(reachable),
    )


def _questioned_substitution(
    entry: dict[str, Any], message: str, path: Path
) -> tuple[str, str] | None:
    """The substitution an operator's question is about.

    The catalogue gives the question as prose, so the pair is read from the
    message: everything between `replaced with` and the question mark. A
    message that does not carry one is refused, because a report with no
    substitution and no parameter would fall through to being an unknown
    *action* — permitted or not for a reason that has nothing to do with what
    was asked.
    """
    marker = "replaced with"
    if marker not in message:
        raise ScenarioError(
            f"{path}: scenario {entry['id']!r} has a message that names no "
            f"substitution: {message!r}"
        )
    # "Can reagent A be replaced with B?" -> what replaces what is the token
    # before the marker and everything after it. Read as prose because the
    # catalogue states it as prose, and refused rather than guessed at when the
    # sentence does not have that shape.
    before, _, after = message.partition(marker)
    given = before.strip().rstrip("?").rsplit(maxsplit=1)[-1].strip("?.,")
    instead = after.strip().strip("?.,")
    if not given or not instead:
        raise ScenarioError(
            f"{path}: scenario {entry['id']!r} names only half a substitution: {message!r}"
        )
    return given, instead
