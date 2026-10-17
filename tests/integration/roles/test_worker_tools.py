"""Phase 6 gate: the Workers, driven through their own tool servers.

`docs/02` §5 gives the Experimental Worker one rule — *is this action explicitly
allowed by the Execution Contract?* — and states that it does not use judgement
to tell an execution question from a scientific one. That rule is executable:
`ravel.execution.worker_rules` answers it by lookup, and these tests drive the
running server that exposes it over stdio.

Three properties are worth the subprocesses:

- **The answer comes from RAVEL, not from the model.** `request_action` takes
  what was asked for and returns the contract's verdict with its reason. There
  is no tool by which a Worker decides anything.
- **A refusal stops the task.** A Worker that raised a deviation and carried on
  would leave a question waiting on Master while the work it was about had
  already happened.
- **The record is left where Master can find it.** The deviation, the escalation
  and the node's status are written together, so "what is this waiting on" has
  an answer that is in PostgreSQL rather than in a session.

Everything runs against a real database, because the claims are about rows: that
a permitted action writes a message and no deviation, that a refused one writes
both, and that no other role can reach either.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.dsh.mcp_probe import probe
from tests.integration.conftest import DEFAULT_OUTPUTS, Prepared
from tests.integration.roles.conftest import RoleEnvironment

from ravel.domain.enums import NodeStatus, WorkerMessageKind
from ravel.domain.execution import DeviationRecord, WorkerMessage
from ravel.domain.preparation import (
    PreparationOutcome,
    PreparationRecord,
    PreparationRefusal,
)
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.mcp.registry import DAG_MUTATION_TOOLS
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.preparations import PreparationRepository
from ravel.state.repositories.records import RecordRepositories

#: When the preparation rows these tests write were made. Chosen rather than
#: read from the clock so that "the newest" is a fact the test decided: two
#: records written in one test would otherwise be ordered by how fast the
#: machine got from one line to the next.
READ_AT = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)

WORKERS = (AgentRole.COMPUTE_WORKER, AgentRole.EXPERIMENTAL_WORKER)

#: Every role but the Workers. The question "does my contract permit this" is
#: asked by the role that acts under a contract, so the others have no tool for
#: it — asserted against the running servers rather than against the table.
NOT_A_WORKER = tuple(role for role in AgentRole if role not in WORKERS)

#: What a Worker holds. The invocation, the waiting, the collection and the
#: completeness check are the durable layer's, so what is left here is what a
#: Worker genuinely does itself: begin the one task it was convened for, read
#: its terms, read what happened, ask, and (for the Experimental Worker) say one
#: of four things to a lab.
#:
#: `start_execution` is Phase 10's addition and the reason it is here rather
#: than in the durable layer: work begins where the *execution role* is, and a
#: Worker seat that only watched would leave the start with whatever scheduler
#: happened to be nearest. It carries a node id and nothing else — no method, no
#: parameter, no objective — and the DAG decides whether that node may run, so
#: what the Worker holds is the door rather than the judgement.
COMPUTE_TOOLS = frozenset(
    {
        "whoami",
        "start_execution",
        "read_execution_contract",
        "read_execution_status",
        "request_action",
    }
)

#: A contract whose terms every test can rely on: one action, one range, one
#: substitution, and two required outputs.
TERMS: dict[str, Any] = {
    "allowed_actions": ("run_measurement",),
    "allowed_ranges": {"temperature": "300..400"},
    "allowed_substitutions": ("Pd/C -> Pt/C",),
}


def _record(
    database: Database,
    project: Project,
    prepared: Prepared,
    *,
    at: datetime,
    outcome: PreparationOutcome,
    workspace_path: str = "",
    materializer: str = "",
    execution_metadata: dict[str, str] | None = None,
    refusal: PreparationRefusal | None = None,
    reason: str = "",
) -> None:
    """Write one preparation for a node, the way preparation writes one.

    Through the real repository, because what the tool reads is rows: a
    fixture that reached into the tool's own inputs would prove nothing about
    the lookup that decides which of several preparations a Worker is shown.
    """
    with database.transaction() as session:
        PreparationRepository(session, project.project_id).record(
            PreparationRecord(
                project_id=project.project_id,
                node_id=prepared.node_id,
                execution_contract_ref=prepared.contract.contract_id,
                execution_contract_version=prepared.contract.version,
                outcome=outcome,
                workspace_path=workspace_path,
                materializer=materializer,
                materializer_version="1" if materializer else "",
                required_outputs=prepared.contract.required_outputs,
                execution_metadata=dict(execution_metadata or {}),
                refusal=refusal,
                reason=reason,
                created_at=at,
            )
        )


def _deviations(database: Database, project_id: str) -> list[DeviationRecord]:
    with database.read_only() as session:
        return RecordRepositories(session, project_id).deviations.all()


def _messages(database: Database, project_id: str, node_id: str) -> list[WorkerMessage]:
    with database.read_only() as session:
        return RecordRepositories(session, project_id).messages.for_node(node_id)


def _status(database: Database, project_id: str, node_id: str) -> NodeStatus:
    with database.read_only() as session:
        return DagRepository(session, project_id).node(node_id).status


def _ask(node_id: str, **question: Any) -> tuple[str, dict[str, Any]]:
    """One `request_action` call, as the model would phrase it."""
    return ("request_action", {"node_id": node_id, **question})


# ── The roster ──────────────────────────────────────────────────────────────


async def test_a_compute_worker_holds_only_what_it_acts_under(
    role_environment: RoleEnvironment, project: Project
) -> None:
    """One way to begin, two ways to read, one way to ask, and no way to plan."""
    result = await probe(
        role_environment.for_project(project, AgentRole.COMPUTE_WORKER)
    )

    assert set(result.tools) == COMPUTE_TOOLS
    assert set(result.tools).isdisjoint(DAG_MUTATION_TOOLS)
    assert result.whoami is not None
    assert result.whoami["may_mutate_dag"] is False


async def test_the_experimental_worker_adds_only_the_lab_voice(
    role_environment: RoleEnvironment, project: Project
) -> None:
    """The same set, plus the four things a Worker may say to a lab.

    The difference between the two Workers is who they talk to, not what they
    may decide: both begin their own kind of task under a frozen contract, and
    neither may move a node.
    """
    result = await probe(
        role_environment.for_project(project, AgentRole.EXPERIMENTAL_WORKER)
    )

    assert set(result.tools) == COMPUTE_TOOLS | {"send_message"}
    assert set(result.tools).isdisjoint(DAG_MUTATION_TOOLS)


async def test_the_compute_worker_cannot_speak_to_a_lab(
    role_environment: RoleEnvironment, project: Project
) -> None:
    """Refused at the transport: the tool is not registered in that server."""
    result = await probe(
        role_environment.for_project(project, AgentRole.COMPUTE_WORKER),
        calls=(("send_message", {"node_id": "n-1", "kind": "INFORM", "body": "hi"}),),
    )

    call = result.calls[0]
    assert call.failed
    assert "Unknown tool" in (call.error or "")


@pytest.mark.parametrize("role", NOT_A_WORKER)
async def test_only_a_worker_may_ask_the_contract(
    role: AgentRole,
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """The rule belongs to the role that acts under the terms, and to no other.

    A Master that could ask on a Worker's behalf would be answering its own
    question, and a Reviewer that could ask would be negotiating the terms it
    later judges against.
    """
    prepared = prepare(**TERMS)

    result = await probe(
        role_environment.for_project(project, role),
        calls=(_ask(prepared.node_id, requested_action="run_xrd"),),
    )

    call = result.calls[0]
    assert call.failed, f"{role.value} was allowed to ask a contract"
    assert "Unknown tool" in (call.error or "")
    assert _deviations(database, project.project_id) == []
    assert _messages(database, project.project_id, prepared.node_id) == []


# ── What a Worker is told ───────────────────────────────────────────────────


async def test_the_contract_is_read_rather_than_recalled(
    role_environment: RoleEnvironment, project: Project, prepare: Callable[..., Prepared]
) -> None:
    """The frozen terms, with the parts a Worker acts on spelled out.

    The tool also returns what this session may and may not do, because the rule
    "an action the contract does not name is forbidden" is easier to follow when
    it is stated where the terms are.
    """
    prepared = prepare(**TERMS)
    result = await probe(
        role_environment.for_project(project, AgentRole.EXPERIMENTAL_WORKER),
        calls=(("read_execution_contract", {"node_id": prepared.node_id}),),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    contract = call.payload["contract"]
    assert contract["contract_id"] == prepared.contract.contract_id
    assert contract["version"] == prepared.contract.version
    assert contract["is_frozen"] is True
    assert contract["allowed_actions"] == ["run_measurement"]
    assert contract["allowed_ranges"] == {"temperature": "300..400"}
    assert contract["allowed_substitutions"] == ["Pd/C -> Pt/C"]
    assert contract["required_outputs"] == list(DEFAULT_OUTPUTS)
    assert call.payload["node"]["status"] == "READY"

    may_not = " ".join(call.payload["what_this_session_may_not_do"])
    assert "Review" in may_not, "a Worker is told who judges the result"
    assert "Master" in may_not, "and who may move the node"


async def test_the_status_says_what_has_happened_not_what_was_planned(
    role_environment: RoleEnvironment, project: Project, prepare: Callable[..., Prepared]
) -> None:
    """A task that has not run reports nothing in flight and nothing delivered."""
    prepared = prepare(**TERMS)
    result = await probe(
        role_environment.for_project(project, AgentRole.COMPUTE_WORKER),
        calls=(("read_execution_status", {"node_id": prepared.node_id}),),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["node_status"] == "READY"
    assert call.payload["job"] is None
    assert call.payload["execution"] is None
    assert call.payload["deviations"] == []
    assert call.payload["open_deviations"] == []
    assert call.payload["messages"] == []
    assert call.payload["required_outputs"] == list(DEFAULT_OUTPUTS)
    # Nothing has been prepared for this contract — it names no environment —
    # and the two fields are empty rather than absent, so a Worker can tell
    # "not built yet" from "this deployment does not report such a thing".
    assert call.payload["execution_requirements"] == {}
    assert call.payload["prepared_workspace"] == ""
    assert call.payload["prepared_entrypoint"] == ""
    assert call.payload["preparation"] is None


async def test_the_status_says_which_workspace_the_run_was_built_in(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """A Worker reads where its work runs, and which terms it was built from.

    Both rows are written through the real repository, because the tool reads
    rows: what is under test is that the *read* finds the preparation belonging
    to the contract version this node is bound to, and that it reports the
    workspace and the entry point rather than making a Worker derive them.
    """
    prepared = prepare(
        execution_requirements={"software": "raspa"},
        parameter_targets={"temperature_k": "298.0"},
    )
    _record(
        database,
        project,
        prepared,
        at=READ_AT,
        outcome=PreparationOutcome.PREPARED,
        workspace_path="/var/ravel/prepared/project/node/v1",
        materializer="raspa",
        execution_metadata={"entrypoint": "job.slurm", "input": "simulation.input"},
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.COMPUTE_WORKER),
        calls=(("read_execution_status", {"node_id": prepared.node_id}),),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["execution_requirements"] == {"software": "raspa"}
    assert call.payload["prepared_workspace"] == "/var/ravel/prepared/project/node/v1"
    assert call.payload["prepared_entrypoint"] == "job.slurm"
    assert call.payload["preparation"]["outcome"] == "PREPARED"
    assert call.payload["preparation"]["materializer"] == "raspa"


async def test_a_refusal_does_not_hide_the_workspace_the_run_was_built_in(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """The newest record and the newest *prepared* record are read separately.

    A run that prepared and was then prepared again by a host that could not do
    the job has a refusal as its newest preparation. A Worker asking where to
    run must be told the workspace that exists, and must still be able to read
    the refusal — so the two questions are answered from two lookups rather
    than one, which is the whole reason `prepared_for_run` exists beside
    `latest_for_run`.
    """
    prepared = prepare(execution_requirements={"software": "raspa"})
    _record(
        database,
        project,
        prepared,
        outcome=PreparationOutcome.PREPARED,
        workspace_path="/var/ravel/prepared/project/node/v1",
        materializer="raspa",
        execution_metadata={"entrypoint": "job.slurm"},
        at=READ_AT,
    )
    _record(
        database,
        project,
        prepared,
        outcome=PreparationOutcome.REFUSED,
        refusal=PreparationRefusal.ENVIRONMENT_UNAVAILABLE,
        reason="RAVEL_RASPA_DATA_DIR is unset, so no workspace can be built",
        at=READ_AT + timedelta(minutes=1),
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.COMPUTE_WORKER),
        calls=(("read_execution_status", {"node_id": prepared.node_id}),),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["prepared_workspace"] == "/var/ravel/prepared/project/node/v1"
    assert call.payload["preparation"]["outcome"] == "REFUSED"
    assert call.payload["preparation"]["refusal"] == "ENVIRONMENT_UNAVAILABLE"


# ── Asking ──────────────────────────────────────────────────────────────────


async def test_a_permitted_action_is_confirmed_and_the_work_carries_on(
    role_environment: RoleEnvironment, project: Project, database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """The whole of what a Worker does when the contract names the thing.

    A confirmation is recorded rather than a deviation: the question was
    answered by the contract, and a deviation row would say a question is
    waiting on Master when none is.
    """
    prepared = prepare(**TERMS)
    result = await probe(
        role_environment.for_project(project, AgentRole.COMPUTE_WORKER),
        calls=(
            _ask(
                prepared.node_id,
                requested_action="run_measurement",
                parameter="temperature",
                value=350,
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["permitted"] is True
    assert call.payload["deviation_id"] is None
    assert call.payload["node_status"] == "READY"
    assert "300..400" in call.payload["reason"], (
        "the reason states the window that permitted it, so a later reader can "
        f"check the answer rather than trust it; it said {call.payload['reason']!r}"
    )

    assert _deviations(database, project.project_id) == []
    (message,) = _messages(database, project.project_id, prepared.node_id)
    assert message.kind is WorkerMessageKind.CONFIRM
    assert message.approved_by_contract is True
    assert _status(database, project.project_id, prepared.node_id) is NodeStatus.READY


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ({"requested_action": "run_xrd"}, "does not list the action"),
        (
            {"requested_action": "run_measurement", "parameter": "temperature", "value": 900},
            "outside it",
        ),
        (
            {"requested_action": "run_measurement", "parameter": "pressure", "value": 1.0},
            "declares no range for pressure",
        ),
        (
            {"requested_action": "swap catalyst", "substitute": ["Pd/C", "Ir/C"]},
            "does not list the substitution",
        ),
    ],
    ids=["an action", "a value", "a parameter", "a substitution"],
)
async def test_what_the_contract_is_silent_about_stops_the_task(
    question: dict[str, Any],
    expected: str,
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """Four ways to be outside the contract, and one ending for all of them.

    The reason says which silence was found, because "not considered" and
    "considered and refused" call for different answers from Master.
    """
    prepared = prepare(**TERMS)
    result = await probe(
        role_environment.for_project(project, AgentRole.EXPERIMENTAL_WORKER),
        calls=(_ask(prepared.node_id, **question),),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["permitted"] is False
    assert call.payload["deviation_id"] is not None
    assert call.payload["task_stopped"] is True
    assert call.payload["node_status"] == "WAITING_DECISION"
    assert expected in call.payload["reason"]

    (deviation,) = _deviations(database, project.project_id)
    assert deviation.permitted is False
    assert deviation.node_id == prepared.node_id
    assert deviation.execution_contract_ref == prepared.contract.contract_id
    assert deviation.raised_by == AgentRole.EXPERIMENTAL_WORKER.value
    assert deviation.is_open

    (message,) = _messages(database, project.project_id, prepared.node_id)
    assert message.kind is WorkerMessageKind.ESCALATE
    assert "has not answered the question" in message.body

    assert _status(database, project.project_id, prepared.node_id) is (
        NodeStatus.WAITING_DECISION
    ), "a question waiting on Master must be a node Master can see waiting"
    with database.read_only() as session:
        assert RecordRepositories(session, project.project_id).decisions.all() == [], (
            "answering the deviation is Master's decision to make, not the Worker's "
            "to record"
        )


async def test_a_permitted_substitution_is_answered_by_the_pair_it_matches(
    role_environment: RoleEnvironment, project: Project, prepare: Callable[..., Prepared]
) -> None:
    """Case-sensitive and exact, so the answer names the pair that was listed."""
    prepared = prepare(**TERMS)
    result = await probe(
        role_environment.for_project(project, AgentRole.EXPERIMENTAL_WORKER),
        calls=(
            _ask(
                prepared.node_id,
                requested_action="swap catalyst",
                substitute=["Pd/C", "Pt/C"],
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["permitted"] is True
    assert "Pd/C" in call.payload["reason"] and "Pt/C" in call.payload["reason"]


async def test_a_substitution_that_is_not_a_pair_is_not_asked_at_all(
    role_environment: RoleEnvironment, project: Project, database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """One name replacing nothing is a different question, and not one the
    contract can answer."""
    prepared = prepare(**TERMS)
    result = await probe(
        role_environment.for_project(project, AgentRole.EXPERIMENTAL_WORKER),
        calls=(
            _ask(
                prepared.node_id,
                requested_action="drop catalyst",
                substitute=["Pd/C"],
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed
    assert "is not a substitution" in (call.error or "")
    assert _deviations(database, project.project_id) == []
    assert _status(database, project.project_id, prepared.node_id) is NodeStatus.READY


async def test_a_worker_does_not_claim_to_have_stopped_a_task_it_did_not(
    role_environment: RoleEnvironment, project: Project, database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """The second refusal reports the node as it is, not as the tool wishes it were.

    A node already waiting on Master cannot be moved anywhere by this tool, and
    a reply that said "stopped" a second time would be a claim about the DAG that
    the DAG does not support.
    """
    prepared = prepare(**TERMS)
    environment = role_environment.for_project(project, AgentRole.COMPUTE_WORKER)
    question = _ask(prepared.node_id, requested_action="run_xrd")

    first = await probe(environment, calls=(question,))
    second = await probe(environment, calls=(question,))

    assert first.calls[0].payload is not None
    assert first.calls[0].payload["task_stopped"] is True
    assert second.calls[0].payload is not None
    assert second.calls[0].payload["task_stopped"] is False
    assert second.calls[0].payload["node_status"] == "WAITING_DECISION"

    assert len(_deviations(database, project.project_id)) == 2, (
        "asking twice about two different things is two questions; the second is "
        "recorded even though the node had already stopped"
    )
    assert _status(database, project.project_id, prepared.node_id) is (
        NodeStatus.WAITING_DECISION
    )


# ── Speaking to a lab ───────────────────────────────────────────────────────


async def test_a_message_must_be_about_something_the_contract_names(
    role_environment: RoleEnvironment, project: Project, database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """ "INFORM from contract", made checkable.

    The alternative is a Worker negotiating terms in prose that nobody can
    audit, so the refusal names what the contract does list and points at the
    escalation that does not need to.
    """
    prepared = prepare(**TERMS)
    result = await probe(
        role_environment.for_project(project, AgentRole.EXPERIMENTAL_WORKER),
        calls=(
            (
                "send_message",
                {
                    "node_id": prepared.node_id,
                    "kind": "INFORM",
                    "body": "The operator asks whether the run can be shortened.",
                    "about": "shortening the run",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed
    assert "has to be about something the contract names" in (call.error or "")
    assert "run_measurement" in (call.error or "")
    assert "ESCALATE" in (call.error or "")
    assert _messages(database, project.project_id, prepared.node_id) == []


@pytest.mark.parametrize(
    "kind", ["CONFIRM", "INFORM", "REQUEST_MISSING_INFORMATION"]
)
async def test_a_message_about_a_named_term_is_recorded_as_checked(
    kind: str,
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """Each of the three kinds a Worker says *from* the contract.

    What is recorded is that the contract was consulted, which is what makes the
    row reviewable later: a message claiming the contract allowed something has
    to have been checked against it.
    """
    prepared = prepare(**TERMS)
    result = await probe(
        role_environment.for_project(project, AgentRole.EXPERIMENTAL_WORKER),
        calls=(
            (
                "send_message",
                {
                    "node_id": prepared.node_id,
                    "kind": kind,
                    "body": f"{kind}: the run is proceeding at 350 K.",
                    "about": "run_measurement",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["kind"] == kind
    assert call.payload["approved_by_contract"] is True

    (message,) = _messages(database, project.project_id, prepared.node_id)
    assert message.kind.value == kind


async def test_escalation_needs_nothing_named(
    role_environment: RoleEnvironment, project: Project, database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """It is how a Worker asks for authority it does not have.

    Refusing an escalation that names nothing would close the only route by
    which a contract is ever widened — which is exactly the question an operator
    asks when they ask for something the contract does not mention.
    """
    prepared = prepare(**TERMS)
    result = await probe(
        role_environment.for_project(project, AgentRole.EXPERIMENTAL_WORKER),
        calls=(
            (
                "send_message",
                {
                    "node_id": prepared.node_id,
                    "kind": "ESCALATE",
                    "body": "The operator asks whether reagent A can be replaced with B.",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["kind"] == "ESCALATE"

    (message,) = _messages(database, project.project_id, prepared.node_id)
    assert message.kind is WorkerMessageKind.ESCALATE
    assert _status(database, project.project_id, prepared.node_id) is NodeStatus.READY, (
        "an escalation is a question to a lab; it does not stop the task by itself"
    )


async def test_a_kind_outside_the_four_is_refused(
    role_environment: RoleEnvironment, project: Project, database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """The vocabulary is closed, and the refusal lists it."""
    prepared = prepare(**TERMS)
    result = await probe(
        role_environment.for_project(project, AgentRole.EXPERIMENTAL_WORKER),
        calls=(
            (
                "send_message",
                {
                    "node_id": prepared.node_id,
                    "kind": "NEGOTIATE",
                    "body": "Let us agree on different terms.",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed
    assert "CONFIRM" in (call.error or "") and "ESCALATE" in (call.error or "")
    assert _messages(database, project.project_id, prepared.node_id) == []
