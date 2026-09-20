#!/usr/bin/env python3
"""Drive one project: the loop, with the harness in Master's and Review's seats.

    .venv/bin/python scripts/run_project.py --project <project_id>
    .venv/bin/python scripts/run_project.py --project <project_id> --max-rounds 40

RAVEL's own software sequences a project — reading state, starting runs,
noticing endings — and the two seats that require judgement are filled by agents
on the pinned harness. That division is the point: the loop is deterministic
code, and what it asks for is a decision, never an execution. `ProjectLoop` is
the first half and `HarnessAgent` the second, and both already exist; this
script is the composition, which until now lived only in the end-to-end tests.

**It needs a model credential.** Every round that has something to decide asks
Master or Review, and a turn on the pinned harness reaches a real provider. With
no `DEEPSEEK_API_KEY` the turns fail rather than degrade — the loop counts them
as rounds that changed nothing and halts, saying so, instead of inventing a
decision. That is the intended behaviour and not a mode to run a project in.

Master's session is per project and per process: the harness at this pin cannot
reopen a session across a runtime restart, so a Master that dies is replaced
under the same Master identity with a fresh session id, and recovers from
PostgreSQL and the last checkpoint — never from chat history. See
`vendor/DSH_PIN.json`.

The project must already exist, with its contracts in force. Creating it is
`scripts/create_account.py`; planning its first stage is Master's, and happens
in the loop's first rounds.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from ravel.config import Settings
from ravel.domain.roles import AgentRole
from ravel.dsh.agents import HarnessAgent, WorkerAgent
from ravel.dsh.pool import create_pool
from ravel.execution.loop import ProjectLoop
from ravel.execution.node_runs import TemporalNodeRuns
from ravel.state.database import Database
from ravel.state.repositories.projects import ProjectRegistry


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drive one RAVEL project.")
    parser.add_argument("--project", required=True, help="the project's identifier")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=0.5,
        help="how long between rounds (default: 0.5)",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=200,
        help="stop after this many rounds rather than looping forever (default: 200)",
    )
    return parser.parse_args(argv)


async def drive(args: argparse.Namespace, settings: Settings) -> int:
    """Run the loop for one project, and say what it ended with."""
    database = Database.from_settings(settings)
    # Read before anything is started, so an unknown project is a sentence at
    # the terminal rather than a loop that spends a model turn discovering it.
    with database.read_only() as session:
        project = ProjectRegistry(session).get(args.project)

    pool = create_pool(settings)
    master = HarnessAgent(pool=pool, project_id=project.project_id, role=AgentRole.MASTER)
    review = HarnessAgent(pool=pool, project_id=project.project_id, role=AgentRole.REVIEW)
    compute_worker = WorkerAgent(
        pool=pool, project_id=project.project_id, role=AgentRole.COMPUTE_WORKER
    )
    experimental_worker = WorkerAgent(
        pool=pool, project_id=project.project_id, role=AgentRole.EXPERIMENTAL_WORKER
    )
    execution = await TemporalNodeRuns.connect(database, settings)

    print(
        f"\n  project  {project.display_id}  ({project.project_id})\n"
        f"  status   {project.status.value}\n"
        f"  harness  {settings.dsh_model} via {settings.dsh_provider}\n"
    )
    try:
        run = await ProjectLoop(
            database=database,
            project_id=project.project_id,
            master=master,
            review=review,
            execution=execution,
            compute_worker=compute_worker,
            experimental_worker=experimental_worker,
            poll_seconds=args.poll_seconds,
            max_rounds=args.max_rounds,
        ).run()
    finally:
        # Every scope, and before the engine goes: a runtime left alive holds a
        # session binding that would outlive the process that could serve it.
        master.close()
        review.close()
        compute_worker.close()
        experimental_worker.close()
        database.dispose()

    if run.halted:
        print(
            f"\n  halted at {run.status.value} after {run.rounds} rounds "
            f"({run.stalled_rounds} changed nothing)\n"
            f"  the project has not ended; run it again, or read why in the "
            f"record\n"
        )
        return 1
    print(f"\n  ended {run.status.value} after {run.rounds} rounds\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if settings.deepseek_api_key is None:
        # stderr, and so unbuffered: the log lines below go to stderr through
        # logging, and a notice on stdout would arrive after them, reading as a
        # conclusion rather than the warning it is.
        print(
            "\n  no DEEPSEEK_API_KEY: every turn Master and Review take will "
            "fail, and the loop will halt having decided nothing.\n",
            file=sys.stderr,
        )
    return asyncio.run(drive(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())
