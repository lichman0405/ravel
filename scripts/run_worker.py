#!/usr/bin/env python3
"""The process that runs a project's nodes, for a deployment rather than a test.

    .venv/bin/python scripts/run_worker.py
    .venv/bin/python scripts/run_worker.py --compute-scenario COMPUTE_FAILURE

Temporal is durable execution and RAVEL is the record: this process holds
nothing that a restart would lose. Kill it mid-run and a replacement picks up
the same workflow, because the history is in Temporal and the job is in
PostgreSQL. Nothing here keeps state in memory that matters, which is why the
only thing a deployment needs from this script is that it stays running.

What it registers are the V0 backends, and both are mocks. They are the honest
answer at this pin — `docs/06` puts real compute and a real LIMS out of V0's
scope — and every artifact they produce is marked as simulated in the record
and refused by the Evidence Ledger. They are registered here rather than
defaulted inside the runtime so that the file a deployment reads says which
backends it is running, and so that replacing one is editing this list.

The scenarios are the acceptance catalogue's, by name. A deployment normally
plays the successful path and is pointed at a failure scenario deliberately,
to watch what the loop does about it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from ravel.backends.mocks import MockComputeBackend, MockLabBackend
from ravel.config import Settings
from ravel.domain.enums import NodeType
from ravel.execution.backends import BackendRegistry
from ravel.execution.temporal.worker import run_worker_until_signalled
from ravel.state.database import Database
from ravel.state.store import S3ArtifactStore

#: The scenarios a deployment can start on. Named here so `--help` lists them;
#: the catalogue is still the authority, and a name it does not hold fails when
#: the backend is built rather than when the first node reaches it.
COMPUTE_SCENARIOS = (
    "COMPUTE_SUCCESS",
    "COMPUTE_RETRYABLE_INFRA_FAILURE",
    "COMPUTE_SCIENTIFIC_FAILURE",
    "COMPUTE_MISSING_OUTPUT",
    "COMPUTE_TIMEOUT",
)
LAB_SCENARIOS = (
    "LAB_SUCCESS",
    "LAB_LONG_WAIT",
    "LAB_DEVIATION_PRESSURE",
    "LAB_MISSING_RAW_DATA",
    "LAB_OPERATOR_QUESTION_UNDEFINED",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run RAVEL's execution worker.")
    parser.add_argument(
        "--compute-scenario",
        default="COMPUTE_SUCCESS",
        choices=COMPUTE_SCENARIOS,
        help="what the mock compute backend plays (default: COMPUTE_SUCCESS)",
    )
    parser.add_argument(
        "--lab-scenario",
        default="LAB_SUCCESS",
        choices=LAB_SCENARIOS,
        help="what the mock laboratory backend plays (default: LAB_SUCCESS)",
    )
    parser.add_argument(
        "--step-seconds",
        type=float,
        default=0.2,
        help="how long a mock spends in each state it passes through",
    )
    return parser.parse_args(argv)


def build_registry(
    settings: Settings, database: Database, args: argparse.Namespace
) -> BackendRegistry:
    """The backends this deployment runs.

    The store is opened once and shared: both mocks write artifacts through it,
    and a store per backend would be two clients to the same bucket.
    """
    store = S3ArtifactStore(settings)
    store.ensure_bucket()
    registry = BackendRegistry()
    registry.register(
        NodeType.COMPUTATION,
        MockComputeBackend(
            database=database,
            store=store,
            scenario=args.compute_scenario,
            step_seconds=args.step_seconds,
        ),
    )
    registry.register(
        NodeType.EXPERIMENT,
        MockLabBackend(database=database, store=store, scenario=args.lab_scenario),
    )
    return registry


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # Built here so the mocks and the worker's activities share one engine: a
    # mock that registered a job through one pool and was polled through
    # another would be two views of the same table with a transaction between
    # them, for no gain.
    database = Database.from_settings(settings)
    registry = build_registry(settings, database, args)

    print(
        f"\n  worker on {settings.temporal_task_queue} at {settings.temporal_host}\n"
        f"  compute: {args.compute_scenario}\n"
        f"  lab:     {args.lab_scenario}\n"
        f"  these are mocks; everything they produce is marked simulated\n"
        f"\n  Ctrl-C to stop\n"
    )
    try:
        asyncio.run(run_worker_until_signalled(settings, registry=registry))
    except KeyboardInterrupt:
        print("\n  stopped\n")
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
