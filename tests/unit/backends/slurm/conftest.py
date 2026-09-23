"""The two fixtures these tests drive the backend through.

The fake itself — `ScriptedCluster`, `Connections`, and the command surface —
is `tests/support/slurm.py`, because the integration suite collects a job's
output through the same cluster and a conftest is loaded for its own directory
only. What belongs here is what is particular to these tests: a cluster with
nothing on it, and a connection factory that records what it opened.
"""

from __future__ import annotations

import pytest
from tests.support.slurm import (  # noqa: F401 - re-exported for the tests' imports
    JOBS_ROOT,
    PASSWORD,
    Connections,
    ScriptedCluster,
)


@pytest.fixture
def cluster() -> ScriptedCluster:
    """A cluster with nothing on it."""
    return ScriptedCluster()


@pytest.fixture
def connections(cluster: ScriptedCluster) -> Connections:
    """A factory over the scripted cluster that records what it opens."""
    return Connections(cluster=cluster)
