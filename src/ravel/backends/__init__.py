"""Mock compute and laboratory backends, and the scenarios they play.

`MockComputeBackend` and `MockLabBackend` implement `WorkBackend` without
computing anything or running any experiment. They are what lets the whole
runtime — durable execution, deviation handling, Review, the TUI — be exercised
on one machine, and every artifact they produce is marked simulated so that no
part of that exercise can be mistaken for a result.

`scenarios` reads `acceptance/MOCK_SCENARIOS.yaml`; `mocks` plays it. The
catalogue is the specification and the mocks are the implementation, which is
why a scenario the catalogue does not name cannot be played: `mocks` resolves
every scenario through `scenarios` at construction time.
"""

from ravel.backends.mocks import (
    DEFAULT_STEP_SECONDS,
    MockComputeBackend,
    MockLabBackend,
)
from ravel.backends.scenarios import (
    CATALOGUE_PATH,
    Catalogue,
    ComputeScenario,
    LabScenario,
    ScenarioError,
    catalogue,
)

__all__ = [
    "CATALOGUE_PATH",
    "DEFAULT_STEP_SECONDS",
    "Catalogue",
    "ComputeScenario",
    "LabScenario",
    "MockComputeBackend",
    "MockLabBackend",
    "ScenarioError",
    "catalogue",
]
