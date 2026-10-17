"""P11-04: a contract that names an environment, and the answer when RAVEL cannot build it.

The preparation layer sits between an Execution Contract and the work it orders,
and the sentence it exists to make true is: *a run happens in a workspace RAVEL
built from the contract, or it does not happen at all.* The integration gate
(`tests/integration/temporal/test_preparation.py`) proves the path — contract,
inputs, files, hashes, manifest, the request a backend is handed. What only the
real stack shows is the two things this module is about.

**First: a refusal reaches the seat that has to answer it.** The workflow parks
the node at `WAITING_DECISION` and writes down why, and the whole architecture of
the project is that Master decides. A reason recorded in a table no seat can read
is a question with no question in it, and this is not a hypothetical: in a live
five-agent run a Master was handed a node that had refused preparation, read the
record, found nothing, and cancelled the work — verbatim, "the state records no
verdict from a seat, no run reconciliation from RAVEL, and no unexecutability
reason". The refusal had been written the whole time. The prompt that hands
Master the node is asserted here too, because it named the reasons a node stops
and the list was one short.

**Second: a refusal is a statement about this deployment, not about the work.**
The same contract prepares where the environment can be built and refuses where
it cannot, with a sentence naming the machine — which is what keeps Master
choosing between revising the terms and redirecting the node instead of
re-planning around an experiment something has quietly judged.

The worker is the deployment's own. `headless` starts it with the store and the
materializers `ExecutionRuntime.from_settings` gives a real one, so what these
cases exercise is the registry a deployment has rather than one assembled for
the occasion.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from tests.acceptance.phase10_support import seat_scope, seat_tool
from tests.e2e.conftest import Headless
from tests.integration.conftest import Prepared

from ravel.domain.enums import NodeStatus, NodeType
from ravel.domain.preparation import PreparationRefusal
from ravel.domain.roles import AgentRole
from ravel.dsh.agents import HarnessAgent
from ravel.execution.loop import read_situation
from ravel.state.repositories.records import RecordRepositories

pytestmark = [pytest.mark.phase11, pytest.mark.timeout(900)]

ACTOR = "compute-worker"

#: An environment no materializer in the deployment answers. `vasp` rather than
#: an invented name, because the class the refusal carries is about a gap RAVEL
#: could close — this deployment cannot build it, which is not the same claim as
#: nobody can.
UNAVAILABLE_SOFTWARE = "vasp"

#: The generation run the refusal is caught in. The contract is version one
#: because `prepare` writes one and freezes it, and the refusal has to report
#: the version it refused rather than the one the node is on now.
CONTRACT_VERSION = 1


async def run_node(headless: Headless, prepared: Prepared) -> None:
    """Run the node to whatever ending the run reaches."""
    handle = await headless.client.start_node_run(
        project_id=prepared.project_id,
        node_id=prepared.node_id,
        actor_id=ACTOR,
        execution_contract_version=prepared.contract.version,
    )
    await handle.result()


async def master_state(headless: Headless, project_id: str) -> dict[str, Any]:
    """What a Master session reads, through the role's own tool."""

    async def read() -> dict[str, Any]:
        context = seat_scope(headless.database, project_id, AgentRole.MASTER)
        return await seat_tool("read_project_state", context)()

    return await read()


def preparations(headless: Headless, node_id: str) -> list[dict[str, Any]]:
    """The node's preparation rows, oldest first, as they were written."""
    with headless.database.read_only() as session:
        return [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT outcome, refusal, materializer, workspace_path, reason, "
                    "execution_contract_version FROM execution_preparations "
                    "WHERE node_id = :n ORDER BY created_at"
                ),
                {"n": node_id},
            ).mappings()
        ]


def a_master_turn(headless: Headless, project_id: str) -> str:
    """The prompt a Master session is handed for its next turn."""
    return HarnessAgent(
        pool=None,  # type: ignore[arg-type]
        project_id=project_id,
        role=AgentRole.MASTER,
    )._master_prompt(read_situation(headless.database, project_id))


async def test_p11_04_a_contract_this_deployment_cannot_build_reaches_master(
    headless: Headless, prepare
) -> None:
    """The refusal is recorded, and the one seat that can act on it is told.

    Every field of the report is something RAVEL wrote when it refused: which
    class of refusal, in which words, under which version of the contract, and
    where the workspace would have been. Nothing in it is a summary and nothing
    in it is a judgement, which is what makes it safe to hand to the seat whose
    decision software is not allowed to make.
    """
    # The backend is registered and healthy, and the refusal is asserted
    # *against* that: a run that stopped because nothing could take the work
    # would be a different failure with a different answer, and a case that left
    # the registry empty could not tell the two apart.
    headless.compute("COMPUTE_SUCCESS")
    prepared = prepare(
        execution_requirements={"software": UNAVAILABLE_SOFTWARE},
        parameter_targets={"encut": "500"},
        resource_limits={"wall_clock_hours": "4", "cpus_per_node": "8"},
    )
    await run_node(headless, prepared)

    # Nothing ran: no Execution Record, no escalation, and no job for the
    # backend to have accepted. A refusal is not a failed run, because no run
    # happened — and the job count is a row rather than a call, so a backend
    # that was asked and answered nothing would still show up here.
    with headless.database.read_only() as session:
        records = RecordRepositories(session, prepared.project_id)
        assert records.executions.for_node(prepared.node_id) == []
        assert records.deviations.all() == []
        assert records.jobs.all(node_id=prepared.node_id) == []

    state = await master_state(headless, prepared.project_id)
    stopped = state["stopped"]

    assert [entry["node"] for entry in stopped] == [prepared.node.display_id]
    entry = stopped[0]
    assert entry["status"] == NodeStatus.WAITING_DECISION.value
    assert entry["verdict"] is None, (
        "no seat saw this contract, and reporting a verdict would invent one"
    )
    assert entry["run_reconciliation"] is None, (
        "the run was not lost — it refused, and the two are different questions"
    )

    refusal = entry["preparation_refusal"]
    assert refusal is not None, (
        "Master is handed a node that stopped, and the reason RAVEL wrote for it "
        f"is not in the read: {entry}"
    )
    assert refusal["refusal"] == PreparationRefusal.UNSUPPORTED_ENVIRONMENT.value
    assert UNAVAILABLE_SOFTWARE in refusal["reason"], (
        "the reason has to name the environment the contract asked for, or Master "
        f"cannot tell what to change: {refusal['reason']}"
    )
    assert refusal["execution_contract_version"] == CONTRACT_VERSION
    assert refusal["materializer"] == "", (
        "nothing built this, and naming a materializer would say something did"
    )
    assert refusal["required_outputs"] == list(prepared.contract.required_outputs)
    assert refusal["preparation_id"] and refusal["created_at"]

    # And the reason the read reports is the reason in the table, compared here
    # rather than trusted: a projection that rendered a plausible sentence of
    # its own would satisfy every assertion above.
    (row,) = preparations(headless, prepared.node_id)
    assert row["outcome"] == "REFUSED"
    assert refusal["reason"] == row["reason"]
    assert refusal["workspace_path"] == row["workspace_path"]

    # The turn that hands Master the node points at the field, because a reason
    # in a record no prompt mentions is a reason a session will not go and read.
    # This is the half of the defect the case is named for.
    prompt = a_master_turn(headless, prepared.project_id)
    assert "preparation_refusal" in prompt, (
        f"Master is handed a node that refused preparation without being told "
        f"where the reason is: {prompt}"
    )


async def test_p11_04_the_same_contract_prepares_where_the_environment_exists(
    headless: Headless, prepare
) -> None:
    """The other half of the claim: the refusal above is about the host.

    The same tool call, the same kind of contract, and a deployment that can
    build what the contract names. If the case above were saying something about
    the *contract*, this one would refuse as well — and the pair is what makes
    the class a statement about a machine rather than about the work.

    A bench package rather than a solver input, because what is being told apart
    is the deployment's registry and not a chemistry stack: the package is
    assembled from the contract and the node's own criteria, so nothing here
    depends on an installation being present.
    """
    headless.lab("LAB_SUCCESS")
    prepared = prepare(
        node_type=NodeType.EXPERIMENT,
        execution_requirements={"lab": "bench-chemistry"},
        procedure="Equilibrate each sample and record the conductivity.",
        parameter_targets={"temperature_c": "25"},
        resource_limits={"bench_hours": "6", "instrument": "TGA-2"},
        inputs=("batch-17 powder", "batch-18 powder"),
    )
    await run_node(headless, prepared)

    (row,) = preparations(headless, prepared.node_id)
    assert row["outcome"] == "PREPARED"
    assert row["materializer"] == "bench-chemistry"
    assert row["refusal"] is None

    state = await master_state(headless, prepared.project_id)
    entry = next(
        (e for e in state["stopped"] if e["node"] == prepared.node.display_id), None
    )
    assert entry is None or entry["preparation_refusal"] is None, (
        f"a contract this deployment can build was reported as refused: {entry}"
    )

    # And the run is not merely prepared but finished, in the workspace: the
    # node is where a completed run leaves it, with the record that says so.
    with headless.database.read_only() as session:
        node = RecordRepositories(session, prepared.project_id)
        assert node.executions.for_node(prepared.node_id) != []
