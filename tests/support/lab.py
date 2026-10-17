"""A laboratory run that has really been prepared, for the suites that need one.

P11-06 added a backend whose work is handed to a *person*, and the two suites
that test it from opposite ends — the backend's own port under
`tests/integration/backends/`, and the Gateway routes the person uses under
`tests/integration/gateway/` — both need the same thing to exist first: a frozen
contract, a package materialized on this machine, and a handover row.

A module rather than a conftest, for the reason `tests/support/slurm.py` is one:
two suites need it and they sit in different directories, and a conftest is
loaded for its own directory only.

**Nothing here is a shortcut around production.** The package is built by
`begin_node_run` and `prepare_execution` — the two activities a run uses — and
the `JobRequest` is built by the function the workflow builds it with. What the
helpers save is the repetition, not the path: a handover written against a
directory a test had made up would be a claim about the fixture, and the whole
point of this phase is that a person is handed a package RAVEL can account for.

The terms below are the *contract's*, not the test's: `LabMaterializer` refuses
to build a package from a contract that states no procedure, no samples, no
conditions and no outputs, because filling any of those in would be making a
scientific decision nobody delegated to software. That refusal is why a
laboratory fixture cannot be built by writing a directory by hand.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tests.integration.conftest import Prepared
from tests.integration.temporal.conftest import await_state

from ravel.backends.lab import HumanLabBackend
from ravel.config import Settings
from ravel.domain.artifacts import Artifact, ArtifactVersion
from ravel.domain.enums import NodeStatus, NodeType, UserRole
from ravel.domain.execution import DeviationRecord
from ravel.domain.lab import LabHandover
from ravel.execution.backends import BackendRegistry, JobRequest
from ravel.execution.temporal.activities import NodeRunActivities, _request
from ravel.execution.temporal.contracts import PreparationReport, RunInput, RunPlan
from ravel.gateway.auth.passwords import hash_password
from ravel.preparation import LabMaterializer, MaterializerRegistry
from ravel.state.database import Database
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.lab import LabHandoverRepository, record_upload
from ravel.state.repositories.preparations import PreparationRepository
from ravel.state.repositories.records import RecordRepositories
from ravel.state.store import S3ArtifactStore

if TYPE_CHECKING:  # imported for its type only, so support does not pull the suite in
    from tests.e2e.conftest import Headless

#: The role whose contract this run executes. A string because that is what
#: `RunInput.actor_id` carries everywhere else.
ACTOR = "experimental-worker"

#: The person at the bench. A *user*, not an agent: every upload is attributed
#: to them, and the suites assert that rather than a role name.
UPLOADER = "lab-user-ada"

#: What the bench owes, in the contract's order. Two, so that a delivery of one
#: is visibly short rather than ambiguous.
OUTPUTS = ("experiment_log", "raw_data")

PROCEDURE = "Equilibrate each sample for 30 minutes, then record the conductivity."
SAMPLES = ("batch-17 powder", "batch-18 powder")
CONDITIONS = {"temperature_c": "25", "hold_minutes": "30"}
LIMITS = {"bench_hours": "6", "instrument": "TGA-2"}

#: What makes a contract one that has to be prepared: `requires_preparation` is
#: about `execution_requirements`, and `lab` is the kind `LabMaterializer` is
#: registered under.
REQUIREMENTS = {"lab": "bench-chemistry"}

LOG_BYTES = b"sample,conductivity\nbatch-17,1.4\nbatch-18,1.9\n"
RAW_BYTES = b"t_minutes,reading\n0,1.38\n30,1.41\n"


def terms(**overrides: object) -> dict[str, object]:
    """The keyword arguments that build a runnable laboratory contract.

    Passed to a suite's own `prepare` fixture, so that each suite decides which
    transaction and which project the node is built in.
    """
    return {
        "node_type": NodeType.EXPERIMENT,
        "required_outputs": OUTPUTS,
        "procedure": PROCEDURE,
        "inputs": SAMPLES,
        "parameter_targets": CONDITIONS,
        "resource_limits": LIMITS,
        "execution_requirements": REQUIREMENTS,
        **overrides,
    }


@dataclass(frozen=True)
class Handed:
    """A laboratory run that has been prepared, handed over, and is waiting."""

    prepared: Prepared
    report: PreparationReport
    plan: RunPlan
    request: JobRequest
    handover: LabHandover
    ref: str

    @property
    def node_id(self) -> str:
        return self.prepared.node_id

    @property
    def workspace(self) -> Path:
        """The package the bench was given, on this machine."""
        return Path(self.report.workspace_path)

    def read_back(self, database: Database) -> LabHandover:
        """The handover as it stands now, read from the database.

        Read rather than held, because what the suites assert is about what was
        *recorded*: a backend that returned the right thing and wrote nothing
        would pass a test that only looked at the return value.
        """
        with database.read_only() as session:
            return LabHandoverRepository(session, self.prepared.project_id).get(
                handover_id=self.handover.handover_id
            )


def prepare_for_the_bench(
    headless: Headless, prepare: Callable[..., Prepared]
) -> Prepared:
    """A contract this run will send to a bench, and the bench to send it to.

    The backend is registered on the harness's own registry, so the workflow
    resolves it exactly as a deployment's would — the registry is what a worker
    process builds from its settings, and a case that resolved the backend some
    other way would be testing a channel no deployment has. The contract is
    built from the terms above, which are the *contract's* terms rather than the
    test's: a materializer refuses a contract that states no procedure, no
    samples, no conditions and no outputs, because filling any of those in would
    be making a scientific decision nobody delegated to software.
    """
    headless.registry.register(
        NodeType.EXPERIMENT, HumanLabBackend(database=headless.database)
    )
    return prepare(**terms())


async def start_and_wait(headless: Headless, prepared: Prepared) -> None:
    """Begin the run and wait until the bench has the work."""
    await headless.client.start_node_run(
        project_id=prepared.project_id,
        node_id=prepared.node_id,
        actor_id=ACTOR,
        execution_contract_version=prepared.contract.version,
    )
    await await_state(
        lambda: headless.status_of(prepared.node) is NodeStatus.WAITING_EXTERNAL
    )


def a_bench_user(
    database: Database, project_id: str, *, username: str, password: str
) -> str:
    """Give a prepared project somebody with an account to work at its bench.

    A project built by `prepare` has one member — its owner — and the console
    signs in the way a person does, with a username and a password. So this is
    two rows: the account, with a real hash, and the `LAB_USER` membership the
    project's owner confers. The owner is the granter rather than nobody,
    because a first membership is the only one nobody confers and this is not
    one.

    Returns:
        The account's identifier, which is what an upload's `created_by` will
        say — the route attributes a file to whoever holds the token, and the
        token names a user rather than a name.
    """
    with database.transaction() as session:
        users = UserRepository(session)
        bench = users.create(username=username, password_hash=hash_password(password))
        owner = users.by_username("owner")
        assert owner is not None, "the prepared project has no owner to grant the role"
        MembershipRepository(session, project_id).grant(
            user_id=bench.user_id, role=UserRole.LAB_USER, granted_by=owner.user_id
        )
    return bench.user_id


def registry_for(bench: HumanLabBackend) -> BackendRegistry:
    """A registry holding one backend, which is all a single run needs."""
    registry = BackendRegistry()
    registry.register(NodeType.EXPERIMENT, bench)
    return registry


def lab_materializers() -> MaterializerRegistry:
    """A deployment that can assemble a bench package, and only that."""
    registry = MaterializerRegistry()
    registry.register(LabMaterializer())
    return registry


async def hand_over(
    *,
    database: Database,
    settings: Settings,
    store: S3ArtifactStore,
    bench: HumanLabBackend,
    prepared: Prepared,
) -> Handed:
    """Prepare the package, begin the run, and give the work to the bench.

    The order is the run's own, and every step is the production one.

    Raises:
        AssertionError: The package could not be built. That is this helper's
            setup failing rather than a condition under test, so it is asserted
            with the materializer's own refusal in the message.
    """
    activities = NodeRunActivities(
        settings=settings,
        database=database,
        registry=registry_for(bench),
        materializers=lab_materializers(),
        store=store,
    )
    plan = await activities.begin_node_run(
        RunInput(
            project_id=prepared.project_id,
            node_id=prepared.node_id,
            attempt=1,
            execution_contract_version=prepared.contract.version,
            actor_id=ACTOR,
        )
    )
    report = await activities.prepare_execution(plan)
    assert report.outcome.value == "PREPARED", (
        f"the package could not be built: {report.refusal} — {report.reason}"
    )

    with database.read_only() as session:
        package = PreparationRepository(session, prepared.project_id).prepared_for_run(
            prepared.node_id, prepared.contract.version
        )
    assert package is not None

    # The request the workflow makes, made by the workflow's own function: a
    # hand-built one could differ from production in exactly the field a
    # backend is being tested on.
    request = _request(plan, attempt=1, prepared=package)
    handle = bench.submit(request)
    with database.read_only() as session:
        handover = LabHandoverRepository(session, prepared.project_id).for_attempt(
            prepared.node_id, 1, prepared.contract.version
        )
    assert handover is not None, "the submission recorded no handover"
    return Handed(
        prepared=prepared,
        report=report,
        plan=plan,
        request=request,
        handover=handover,
        ref=handle.backend_job_ref,
    )


def upload(
    database: Database,
    store: S3ArtifactStore,
    handover: LabHandover,
    output: str,
    body: bytes,
    *,
    by: str = UPLOADER,
    filename: str = "",
    media_type: str = "",
) -> tuple[Artifact, ArtifactVersion]:
    """File bytes as the artifact that answers one output.

    The same function the Gateway's upload route calls, so what a suite asserts
    about an upload's columns is asserted about the ones production writes. The
    route's own refusals — an unowed name, an oversized body — are its tests'
    subject, not this helper's.
    """
    with database.transaction() as session:
        return record_upload(
            session,
            store,
            handover,
            output=output,
            chunks=[body],
            uploaded_by=by,
            filename=filename or output,
            media_type=media_type,
        )


def raise_a_deviation(
    database: Database, prepared: Prepared, *, node_id: str | None = None
) -> DeviationRecord:
    """A lab user's report, recorded the way the route records one.

    Written through the repository the route writes through and against the
    node's frozen contract, so a suite that needs a report to exist before it
    drives the delivery that carries it does not have to go through HTTP to get
    one — the route's own tests are where the route is exercised.

    `prepared` rather than the `Handed` a backend suite usually has, because the
    deviation is raised before there is anything to hand over: the sentence is
    what the bench says instead of doing the work. `node_id` defaults to the
    node the contract is for, and is set only to test a report about *another*
    node.
    """
    deviation = DeviationRecord(
        project_id=prepared.project_id,
        node_id=node_id or prepared.node_id,
        execution_contract_ref=prepared.contract.contract_id,
        requested_action="run_at_pressure",
        description="The furnace would not hold 900C, so the run was done at 850C.",
        raised_by=UPLOADER,
    )
    with database.transaction() as session:
        return RecordRepositories(session, prepared.project_id).deviations.raise_(
            deviation
        )


__all__ = [
    "ACTOR",
    "CONDITIONS",
    "LIMITS",
    "LOG_BYTES",
    "OUTPUTS",
    "PROCEDURE",
    "RAW_BYTES",
    "REQUIREMENTS",
    "SAMPLES",
    "UPLOADER",
    "Handed",
    "a_bench_user",
    "hand_over",
    "lab_materializers",
    "prepare_for_the_bench",
    "raise_a_deviation",
    "registry_for",
    "start_and_wait",
    "terms",
    "upload",
]
