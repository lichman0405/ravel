"""A14 and A19: what may not change, and who may not change it.

The two are the same claim approached from opposite ends. A14 says a frozen
criterion is frozen — the answer a node is judged against cannot move after the
node has run. A19 says only Master may move the plan. Together they are what
makes a result mean anything: if the criteria could be edited, a pass would be a
statement about the editor's mood, and if any role could add a node, the plan
would be whatever the last agent to run decided it was.

Both are enforced below the level a prompt reaches. A14 is a PostgreSQL trigger,
so no repository, migration, or hand-written `UPDATE` escapes it. A19 is checked
twice: the roster decides which tools a role's server registers at all, and the
service refuses a caller whose role is not Master's even if a tool is somehow
reached. Neither is a sentence in a system prompt, which is what the item's "not
merely prompt instruction" is asking about.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.dsh.mcp_probe import probe
from tests.e2e.conftest import LiveGateway
from tests.integration.conftest import Prepared
from tests.integration.gateway.conftest import PASSWORD, account, bearer
from tests.integration.roles.conftest import RoleEnvironment

from ravel.config import Settings
from ravel.domain.contracts import (
    AcceptanceContract,
    AcceptanceCriterion,
    CriterionProvenance,
)
from ravel.domain.dag import DagNode
from ravel.domain.decisions import AuthorityCheck, DecisionRecord
from ravel.domain.enums import Confidence, DecisionType, NodeType, UserRole
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.mcp.registry import DAG_MUTATION_TOOLS
from ravel.state.database import Database
from ravel.state.repositories.contracts import AcceptanceContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import DecisionRepository
from ravel.state.services.dag import DagMutationService, DecisionDraft

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]

#: Every role except Master, which is the set A19 is about.
WORKERS = tuple(role for role in AgentRole if role is not AgentRole.MASTER)

#: The statement `build_prepared` writes by default, named here rather than
#: repeated as a literal so that a change to it fails one assertion in one place.
FROZEN_STATEMENT = "Conductivity rises by at least 15%."

#: A node creation phrased the way a model would phrase one. Every argument is
#: one a model could supply, and none of them names a project — the tool has no
#: such parameter, because a tool that took one would let a session re-scope
#: itself by typing a different identifier.
A_NODE_CREATION: dict[str, Any] = {
    "node_type": "COMPUTATION",
    "objective": "Measure the conductivity of the second batch.",
    "rationale": "The first batch was inconclusive.",
}


def dag_node_count(database: Database, project_id: str) -> int:
    """How many nodes this project's DAG holds, read from PostgreSQL.

    Every refusal below is asserted twice — once on the error, once on the
    database — because a service that raised and wrote anyway would satisfy the
    first assertion and fail the point of it.
    """
    with database.read_only() as session:
        return len(DagRepository(session, project_id).nodes())


def a_node(project: Project, objective: str = "Measure the second batch.") -> DagNode:
    """One node, built the way the domain builds one."""
    return DagNode.create(
        project_id=project.project_id,
        node_type=NodeType.COMPUTATION,
        objective=objective,
        created_by=AgentRole.MASTER.value,
    )


# ── A14 ─────────────────────────────────────────────────────────────────────


def test_a14_a_frozen_criterion_cannot_be_edited_in_place(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """A14: "Attempt to alter executed node's frozen Acceptance Criteria is
    rejected."

    Rejected by PostgreSQL rather than by the repository, and the test goes
    around the repository to prove it: the `UPDATE` below is what a future
    migration or a repair script would issue, and it has to fail for the
    guarantee to be one. `docs/04` puts it plainly — a check application code
    performs is a check a bug can skip.

    The column is the one most worth refusing. Not the node's status, not its
    dependencies, but the *criterion*: "conductivity rises by 15%" becoming
    "rises by 5%" after the measurement came in low is the single edit that
    turns a failure into a pass while leaving every other record consistent.
    """
    prepared = prepare()
    assert prepared.acceptance is not None, "A COMPUTATION node runs against criteria"
    contract_id = prepared.acceptance.contract_id

    with (
        pytest.raises(IntegrityError, match="immutable"),
        database.engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE acceptance_contracts SET criteria = :criteria "
                "WHERE contract_id = :contract_id"
            ),
            {
                "criteria": '[{"statement": "Conductivity rises by at least 5%."}]',
                "contract_id": contract_id,
            },
        )

    with database.read_only() as session:
        after = AcceptanceContractRepository(session, prepared.project_id).get(
            contract_id=contract_id
        )
        assert [criterion.statement for criterion in after.criteria] == [FROZEN_STATEMENT], (
            "the refusal has to leave the row as it was, not merely report one"
        )


def test_a14_new_criteria_are_a_new_version_that_names_its_decision(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """A14's second sentence: "New criteria requires new version +
    DecisionRecord + new execution/node."

    All three, and each is checked rather than the sequence. The new version is
    a second row that supersedes the first; the decision is what makes the change
    one somebody can be asked about later; and the *node* is untouched, because a
    node that ran under one set of criteria is a fact about what happened rather
    than a slot to be refilled.

    The old version survives saying what it said. That is the whole reason to
    version instead of overwriting: a reader asking "what was this judged
    against" gets the answer that was true at the time.
    """
    prepared = prepare()
    assert prepared.acceptance is not None
    project_id = prepared.project_id
    superseded = prepared.acceptance.contract_id

    with database.transaction() as session:
        decision = DecisionRepository(session, project_id).add(
            DecisionRecord(
                project_id=project_id,
                decision_type=DecisionType.REVISE_ACCEPTANCE_CRITERIA,
                rationale="The threshold was written against the wrong instrument range.",
                confidence=Confidence.MEDIUM,
                authority_check=AuthorityCheck(
                    actor_role=AgentRole.MASTER.value,
                    permitted=True,
                    rationale="Success criteria are Master's to revise, with a decision.",
                ),
            )
        )
        contracts = AcceptanceContractRepository(session, project_id)
        revised = contracts.add(
            AcceptanceContract(
                project_id=project_id,
                node_id=prepared.node_id,
                version=2,
                criteria=(
                    AcceptanceCriterion(
                        statement="The series spans 18-24 C at the stated ramp rate.",
                        threshold="18..24 C",
                        # `PROVISIONAL`, which is the honest label for a
                        # threshold RAVEL set for itself. The alternatives all
                        # name an external source, and citing one here would be
                        # claiming a reference this revision does not have.
                        provenance=CriterionProvenance.PROVISIONAL,
                    ),
                ),
                supersedes=superseded,
                decision_ref=decision.decision_id,
            )
        )
        contracts.freeze(revised.contract_id)

    with database.read_only() as session:
        contracts = AcceptanceContractRepository(session, project_id)
        newest = contracts.for_node(prepared.node_id)
        assert newest.version == 2
        assert newest.supersedes == superseded
        assert newest.decision_ref is not None, (
            "a revised criterion that names no decision is an edit nobody made"
        )
        assert newest.is_frozen

        first = contracts.for_node(prepared.node_id, version=1)
        assert [criterion.statement for criterion in first.criteria] == [FROZEN_STATEMENT], (
            "the version that was judged against is still there, saying what it said"
        )

        decisions = DecisionRepository(session, project_id).all()
        assert [record.decision_type for record in decisions] == [
            DecisionType.REVISE_ACCEPTANCE_CRITERIA
        ]


# ── A19 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", WORKERS, ids=lambda role: role.value)
async def test_a19_a_non_master_roles_server_does_not_register_a_dag_mutation(
    role: AgentRole,
    project: Project,
    database: Database,
    integration_settings: Settings,
    tmp_path: Path,
) -> None:
    """A19's first half, asked of the running server rather than of the table.

    `TOOL_ROLES` says which role may hold which tool, and this launches the
    server each role actually gets and reads the roster off the wire. A table is
    a claim; a registered tool is the thing itself, and the two stay the same
    claim only while somebody checks.

    The roster is asserted as a *disjointness* rather than as an expected set,
    because what matters here is not which tools a Research session has — that is
    `tests/integration/roles` — but that none of them is one of the three that
    write the DAG.
    """
    environment = RoleEnvironment(settings=integration_settings, brief_dir=tmp_path / "briefs")
    result = await probe(
        environment.for_project(project, role),
        calls=(("add_dag_node", A_NODE_CREATION),),
    )

    registered = set(result.tools)
    assert registered & DAG_MUTATION_TOOLS == set(), (
        f"{role.value}'s server registers {sorted(registered & DAG_MUTATION_TOOLS)}, "
        "which writes the Scientific DAG"
    )

    assert result.whoami is not None
    assert result.whoami["role"] == role.value
    assert result.whoami["may_mutate_dag"] is False

    # And when it asks anyway. The tool is not registered, so the call fails at
    # the protocol level, which is a refusal — and the two things that make it
    # one are asserted: it failed, and nothing was written.
    call = result.calls[0]
    assert call.failed, f"a DAG mutation from {role.value}'s server was not refused"
    assert dag_node_count(database, project.project_id) == 0, (
        f"{role.value} left a node in the DAG after being refused"
    )


def test_a19_the_mutation_service_refuses_a_non_master_actor(
    database: Database, project: Project
) -> None:
    """A19's second half: "denied at service authorization level".

    The roster is the outer door; this is the inner one, and the one that matters
    if a tool is ever wired to the wrong server or a second caller is written.
    `DagMutationService.add_node` asks the actor's role before it writes.

    Per role rather than once, because "not Master" is a set and the failure a
    single check would hide is one role being left out of the set. The node and
    the draft are built once and reused: a refusal that consumed either would
    show up as the *last* assertion failing, which is where a mutation that
    half-happened would surface.
    """
    draft = DecisionDraft(
        decision_type=DecisionType.CREATE_NODE,
        rationale="The second batch needs measuring.",
        confidence=Confidence.MEDIUM,
    )
    node = a_node(project)

    for role in WORKERS:
        with database.transaction() as session, pytest.raises(PermissionError):
            DagMutationService(session, project.project_id).add_node(
                node, role=role, decision=draft
            )

    assert dag_node_count(database, project.project_id) == 0

    # And Master may, which is what makes the refusals a permission rather than a
    # method that never worked.
    with database.transaction() as session:
        DagMutationService(session, project.project_id).add_node(
            node, role=AgentRole.MASTER, decision=draft
        )

    assert dag_node_count(database, project.project_id) == 1


async def test_a19_no_route_lets_a_member_change_the_dag(
    database: Database, live_gateway: LiveGateway, project: Project
) -> None:
    """A19 for the third kind of caller: a person holding a credential.

    `docs/09` is explicit that an owner directs a project by talking to Master
    and never by editing the graph, so the Gateway must have no route that
    reaches a mutation. This asks the running application over a real socket with
    a real signed token, because a route that exists behind a dependency which
    refuses would look absent to a careful reader and present to a client — and
    it is the client that decides what a member can do.

    The read first, and it is not decoration: a 404 means "no such route" only if
    the same token reaches the routes that do exist. `GET .../dag` answering 200
    is what makes the four refusals below a statement about the DAG rather than
    about a token this test failed to obtain.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)

    async with httpx.AsyncClient(base_url=live_gateway.url, timeout=20.0) as client:
        signed_in = await client.post(
            "/auth/login", json={"username": "ada", "password": PASSWORD}
        )
        assert signed_in.status_code == 200, signed_in.text
        headers = bearer(signed_in.json()["access_token"])

        readable = await client.get(f"/projects/{project.project_id}/dag", headers=headers)
        assert readable.status_code == 200, (
            "the owner cannot even read the DAG, so the refusals below would say "
            f"nothing about writing it: {readable.text}"
        )

        for path in (
            "/dag",
            "/dag/nodes",
            f"/projects/{project.project_id}/dag",
            f"/projects/{project.project_id}/dag/nodes",
        ):
            for method in ("post", "put", "patch", "delete"):
                send = getattr(client, method)
                # `httpx` gives `delete` no way to carry a body, which matches
                # the protocol — a DELETE body has no defined meaning — so the
                # three verbs that take one are asked with an empty object and
                # DELETE is asked bare.
                response = await (
                    send(path, headers=headers)
                    if method == "delete"
                    else send(path, headers=headers, json={})
                )
                assert response.status_code in {404, 405}, (
                    f"{method.upper()} {path} answered {response.status_code}; "
                    "a member reached something that writes the DAG"
                )

    assert dag_node_count(database, project.project_id) == 0
