#!/usr/bin/env python3
"""Temporal Execution Worker — not a RAVEL Agent.

    .venv/bin/python scripts/run_temporal_worker.py
    .venv/bin/python scripts/run_temporal_worker.py --compute-scenario COMPUTE_FAILURE

**This process is not one of the five agents.** RAVEL's agents are Master,
Research, Review, Compute Worker and Experimental Worker, and every one of them
is a DSH session that holds a role's tools. This is infrastructure: it is where
Temporal's activities run, and the three names are worth keeping apart —

    Worker Agent             a DSH session that acts under a frozen contract
    Temporal Worker Process  this process: the activity host, no authority
    Backend                  the thing that actually does the work

The name is Phase 10's, and the old one (`run_worker.py`) is why: it read as
"the worker", which is the Compute Worker's and the Experimental Worker's name
in this codebase, and a process that hosts activities has nothing in common
with either. It decides nothing. What it does is offer a task queue for the
durable layer to hand work to, and call whichever backend is registered for a
node's type.

Temporal is durable execution and RAVEL is the record: this process holds
nothing that a restart would lose. Kill it mid-run and a replacement picks up
the same workflow, because the history is in Temporal and the job is in
PostgreSQL. Nothing here keeps state in memory that matters, which is why the
only thing a deployment needs from this script is that it stays running.

What it registers is chosen here, and the choice is the deployment's. The
default is the pair of mocks: they are what makes a project runnable end to end
on one machine, and every artifact they produce is marked as simulated in the
record and refused by the Evidence Ledger. `--compute-backend slurm` replaces
the compute mock with the real thing — `ravel.backends.slurm.SlurmComputeBackend`
— which submits to a cluster over SSH and brings back real results, and
`--lab-backend human-lab` replaces the lab mock with
`ravel.backends.lab.HumanLabBackend`, which hands a prepared package to a person
and waits. The two real backends are independent: a deployment may run real
compute with a mock bench, or a real bench with mock compute, and the records
say which was which on every artifact.

**The laboratory backend needs no credential and no host**, which is the
difference between it and Slurm worth stating: what it needs is a person, and
there is nothing to validate at start-up. A deployment that selects it and has
no lab user standing at a bench simply has runs that wait — which is the honest
state, and why a wait has its own clock rather than being an error.

The registration is a list in this file rather than a lookup inside the runtime
so that the file a deployment reads says which backends it is running, and so
that replacing one is editing this list.

**A misconfigured cluster is refused here, at start-up.** A deployment that
asks for Slurm without a host gets an error before the worker starts rather
than a project that stalls when its first computation node reaches the queue.
The scenarios are the acceptance catalogue's, by name. A deployment normally
plays the successful path and is pointed at a failure scenario deliberately,
to watch what the loop does about it.
"""


from __future__ import annotations

import argparse
import asyncio
import sys

from ravel.backends.lab import HumanLabBackend
from ravel.backends.mocks import MockComputeBackend, MockLabBackend
from ravel.backends.slurm import (
    ParamikoTransport,
    SlurmComputeBackend,
    SlurmConfigurationError,
    SlurmTarget,
)
from ravel.config import Settings
from ravel.domain.enums import NodeType
from ravel.domain.services import TEMPORAL_WORKER
from ravel.execution.backends import BackendRegistry
from ravel.execution.temporal.worker import run_worker_until_signalled
from ravel.service import configure_logging
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


#: The node types a deployment can point somewhere real. Named here so that
#: `--help` says what exists; a name this does not hold is refused by argparse
#: before the worker starts.
COMPUTE_BACKENDS = ("mock", "slurm")
LAB_BACKENDS = ("mock", "human-lab")

#: What the real laboratory backend is called on the command line. The two
#: spellings are the same string on purpose: `HumanLabBackend.name` is what it
#: writes into every record it produces, and a deployment that had to translate
#: between the flag and the backend would be one place for the two to disagree.
HUMAN_LAB = "human-lab"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run RAVEL's execution worker.")
    parser.add_argument(
        "--compute-backend",
        default="mock",
        choices=COMPUTE_BACKENDS,
        help=(
            "where computation nodes are run: 'mock' simulates a result and "
            "marks it simulated, 'slurm' submits a real job to the cluster "
            "named by the RAVEL_SLURM_* settings (default: mock)"
        ),
    )
    parser.add_argument(
        "--lab-backend",
        default="mock",
        choices=LAB_BACKENDS,
        help=(
            "where experiment nodes are handed over: 'mock' simulates a bench "
            "and marks its output simulated, 'human-lab' hands a prepared "
            "package to a person through the Gateway and waits (default: mock)"
        ),
    )
    parser.add_argument(
        "--compute-scenario",
        default="COMPUTE_SUCCESS",
        choices=COMPUTE_SCENARIOS,
        help="what the mock compute backend plays (default: COMPUTE_SUCCESS)",
    )
    parser.add_argument(
        "--slurm-jobs-root",
        default=None,
        help=(
            "the remote directory RAVEL's workspaces live under; overrides "
            "RAVEL_SLURM_JOBS_ROOT, and ignored unless --compute-backend slurm"
        ),
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

    The store is opened once and shared: every backend writes artifacts through
    it, and a store per backend would be two clients to the same bucket.

    Raises:
        SlurmConfigurationError: `--compute-backend slurm` was asked for and the
            settings do not describe a cluster to reach.
    """
    store = S3ArtifactStore(settings)
    store.ensure_bucket()
    registry = BackendRegistry()
    registry.register(
        NodeType.COMPUTATION,
        (
            slurm_backend(settings, database, store, args)
            if args.compute_backend == "slurm"
            else MockComputeBackend(
                database=database,
                store=store,
                scenario=args.compute_scenario,
                step_seconds=args.step_seconds,
            )
        ),
    )
    registry.register(
        NodeType.EXPERIMENT,
        (
            # No store and no credential: this backend reads the records an
            # upload wrote, and a bench has nothing to authenticate to.
            HumanLabBackend(database=database)
            if args.lab_backend == HUMAN_LAB
            else MockLabBackend(database=database, store=store, scenario=args.lab_scenario)
        ),
    )
    return registry


def _describe_lab(args: argparse.Namespace) -> str:
    """What the worker is about to hand experiments to, for the banner.

    A laboratory is a person, so there is nothing here to name the way a host
    and an account name a cluster — and the difference is worth printing, since
    it is the difference between a run that finishes on its own and one that
    waits for somebody.
    """
    if args.lab_backend == HUMAN_LAB:
        return (
            f"{HUMAN_LAB}: packages are handed to a person, who uploads "
            "through the Gateway and answers by working"
        )
    return f"{args.lab_scenario} (a mock; its output is marked simulated)"


def slurm_backend(
    settings: Settings,
    database: Database,
    store: S3ArtifactStore,
    args: argparse.Namespace,
) -> SlurmComputeBackend:
    """The real compute backend, from the settings a deployment supplied.

    The password is read here, from `Settings` — where it arrived from the
    environment of *this* process — and passed to a `SlurmTarget` that the
    transport reads once per connection. It never enters RAVEL's database, never
    enters a job's `task_spec`, and is kept out of every tool server's
    environment by `Settings._LAUNCHER_ONLY`.

    Raises:
        SlurmConfigurationError: The host or the username is missing. Refused
            at start-up, where somebody is watching, rather than at the first
            node that reaches the queue.
    """
    if not settings.slurm_host:
        raise SlurmConfigurationError(
            "--compute-backend slurm needs a cluster: set RAVEL_SLURM_HOST and "
            "RAVEL_SLURM_USERNAME (and RAVEL_SLURM_PASSWORD or "
            "RAVEL_SLURM_KEY_FILENAME) before starting the worker"
        )
    if not settings.slurm_username:
        raise SlurmConfigurationError(
            "RAVEL_SLURM_HOST is set but RAVEL_SLURM_USERNAME is not, so there "
            "is no account to submit as"
        )
    target = SlurmTarget(
        host=settings.slurm_host,
        username=settings.slurm_username,
        port=settings.slurm_port,
        password=(
            settings.slurm_password.get_secret_value() if settings.slurm_password else None
        ),
        key_filename=str(settings.slurm_key_filename) if settings.slurm_key_filename else None,
        trust_unknown_host=settings.slurm_trust_unknown_host,
        connect_timeout_seconds=settings.slurm_connect_timeout_seconds,
        command_timeout_seconds=settings.slurm_command_timeout_seconds,
    )
    return SlurmComputeBackend(
        target=target,
        # A factory, not a connection: each operation opens its own, so a poll
        # that waits minutes for a durable timer is not a socket held open for
        # the length of it.
        connect=lambda: ParamikoTransport.connect(
            host=target.host,
            port=target.port,
            username=target.username,
            password=target.password,
            key_filename=target.key_filename,
            trust_unknown_host=target.trust_unknown_host,
            timeout=target.connect_timeout_seconds,
        ),
        database=database,
        store=store,
        jobs_root=args.slurm_jobs_root or settings.slurm_jobs_root,
    )


def _describe_compute(settings: Settings, args: argparse.Namespace) -> str:
    """What the worker is about to run computation on, for the banner.

    The host and the account, never the credential: this line goes to a
    terminal, to a log file, and into whatever collects them.
    """
    if args.compute_backend != "slurm":
        return f"{args.compute_scenario} (a mock; its output is marked simulated)"
    root = args.slurm_jobs_root or settings.slurm_jobs_root
    return (
        f"slurm at {settings.slurm_username}@{settings.slurm_host}:{settings.slurm_port}, "
        f"workspaces under {root}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    configure_logging(
        TEMPORAL_WORKER, level=settings.log_level, log_format=settings.log_format
    )

    # Built here so the mocks and the worker's activities share one engine: a
    # mock that registered a job through one pool and was polled through
    # another would be two views of the same table with a transaction between
    # them, for no gain.
    database = Database.from_settings(settings)
    try:
        registry = build_registry(settings, database, args)
    except (SlurmConfigurationError, ValueError) as error:
        # Printed rather than raised: this is a deployment that has not been
        # finished being configured, and a traceback is not the way to tell
        # somebody which variable is missing.
        print(f"\n  cannot start: {error}\n", file=sys.stderr)
        database.dispose()
        return 2

    print(
        f"\n  worker on {settings.temporal_task_queue} at {settings.temporal_host}\n"
        f"  compute: {_describe_compute(settings, args)}\n"
        f"  lab:     {_describe_lab(args)}\n"
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
