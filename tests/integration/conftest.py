"""Fixtures for tests that need the real stack.

These tests run against a real PostgreSQL, a real MinIO, and — where the gate
calls for it — a real Temporal, because the properties being asserted are
properties of those systems. A test that proved "an UPDATE is rejected" against
a fake database would prove nothing about the constraint that does the
rejecting.

**The database is a dedicated one, and that is enforced.** Teardown truncates
every table, so pointing these tests at a developer's working database would
destroy real projects. `_test_settings` refuses to run if the configured
database name does not end in `_test`.

**And it is checked for staleness.** `create_all` creates missing tables and
leaves existing ones exactly as they are, so a rule added to a model after its
table was first created never reaches this database. The symptom is ugly: a
test that asserts the database refuses something instead watches it succeed,
which reads as an unguarded column rather than as an old table.
`_assert_schema_is_current` turns that into a sentence naming the table.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import CheckConstraint, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from ravel.config import Settings
from ravel.domain.contracts import (
    AcceptanceContract,
    AcceptanceCriterion,
    CriterionProvenance,
    ExecutionContract,
    ResearchContract,
)
from ravel.domain.dag import DagNode
from ravel.domain.decisions import ReviewRecord
from ravel.domain.enums import (
    AccessStatus,
    EvidenceSourceTier,
    NodeStatus,
    NodeType,
    ReviewCheckpoint,
    ReviewOutcome,
    UserRole,
)
from ravel.domain.evidence import EvidenceSource
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import requires_frozen_criteria
from ravel.execution.backends import JobRequest
from ravel.review import ReviewService
from ravel.state import guards
from ravel.state.database import Database, create_db_engine, install_utc_guard
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
    ResearchContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.research import EvidenceSourceRepository
from ravel.state.store import S3ArtifactStore
from ravel.state.tables import Base

#: Every integration test truncates on the way in, so the database name is a
#: safety interlock rather than a convention.
TEST_DATABASE_SUFFIX = "_test"


@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    """Settings pointed at the dedicated test database.

    **The name comes from the environment when it is set**, because every test
    in this suite truncates on the way in and two suites therefore cannot share
    one database: a second run — a live acceptance suite, say — would empty the
    tables under the first, and the symptom is a `NotFound` for a row the other
    run had just written. `RAVEL_TEST_DB` is how a second database is named so
    that two runs can proceed at once; the suffix check below still applies to
    whatever it says, so the interlock is not weakened by being configurable.

    Raises:
        RuntimeError: The configured database is not a test database.
    """
    # The DSN override is addressed by its alias, which is the name pydantic
    # actually accepts: an ambient RAVEL_POSTGRES_DSN must not redirect a
    # suite that truncates every table.
    settings = Settings(
        env="test",
        postgres_db=os.environ.get("RAVEL_TEST_DB", "ravel_test"),
        RAVEL_POSTGRES_DSN=None,
    )
    if not settings.postgres_db.endswith(TEST_DATABASE_SUFFIX):
        raise RuntimeError(
            f"integration tests truncate every table and refuse to run against "
            f"{settings.postgres_db!r}; the name must end in {TEST_DATABASE_SUFFIX!r}"
        )
    return settings


@pytest.fixture(scope="session")
def database(integration_settings: Settings) -> Iterator[Database]:
    """The test database, with the schema and the guards installed."""
    engine = create_db_engine(integration_settings)
    install_utc_guard(engine)
    db = Database(engine)
    try:
        db.health()
    except (OperationalError, ProgrammingError) as error:  # pragma: no cover
        pytest.skip(f"PostgreSQL is not reachable; run scripts/dev_up.sh ({error})")
    Base.metadata.create_all(engine)
    # `create_all` fires `after_create` only when it actually created tables, so
    # the guards are installed explicitly as well. `install` is idempotent, and
    # an existing schema must still be guarded.
    with engine.begin() as connection:
        guards.install(connection)
    _assert_schema_is_current(engine)
    yield db
    db.dispose()


def _assert_schema_is_current(engine: Engine) -> None:
    """Refuse to run against tables older than the models that describe them.

    Three things are compared, all by name and one of them by content: check
    constraints, so that a table predating a rule is caught rather than read as
    an unguarded one; the *literals* of those constraints, because a constraint
    that kept its name while its vocabulary changed is exactly what adding a
    value to an enum does; and columns, because an added column is the other
    half of the same problem. Worth the catalog queries once per session,
    because the failure each prevents is otherwise read as something else
    entirely — a missing guard, a column that apparently does not exist in a
    table that has had it for days, or a status the database refuses to store.

    Raises:
        RuntimeError: A constraint or column the models declare is absent from
            the database, or a check constraint's values differ from the
            model's, which means the table was created before the change and
            `create_all` did not rewrite it.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT relname, conname, pg_get_constraintdef(pg_constraint.oid) "
                "FROM pg_constraint "
                "JOIN pg_class ON pg_class.oid = pg_constraint.conrelid "
                "WHERE contype = 'c'"
            )
        ).all()
    present = {(relname, conname): definition for relname, conname, definition in rows}
    checks = _named_checks()
    stale = sorted(
        f"{table}.{name}" for table, name, _ in checks if (table, name) not in present
    )
    stale.extend(_drifted_constraints(checks, present))
    stale.extend(_missing_columns(engine))
    if stale:
        raise RuntimeError(
            "the test database is older than the models, so these rules are not "
            "in force: "
            + ", ".join(stale)
            + ". `create_all` does not alter a table that already exists; drop "
            "the tables named above (DROP TABLE ... CASCADE) and run again."
        )


def _named_checks() -> list[tuple[str, str, CheckConstraint]]:
    """Every named `CHECK` the models declare, as `(table, name, constraint)`."""
    named: list[tuple[str, str, CheckConstraint]] = []
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and isinstance(constraint.name, str):
                named.append((table.name, constraint.name, constraint))
    return named


def _drifted_constraints(
    checks: list[tuple[str, str, CheckConstraint]],
    present: dict[tuple[str, str], str],
) -> list[str]:
    """Check constraints whose vocabulary the database does not match.

    Compared by the string literals each one mentions rather than by its whole
    text, because the two sides are written in different dialects: the model
    holds `status IN ('CREATED', 'EXECUTING')` and PostgreSQL returns
    `((status)::text = ANY ((ARRAY['CREATED'::character varying, ...])::text[]))`.
    Same vocabulary, two spellings.

    What this catches is a closed vocabulary that gained or lost a member —
    which is what adding a value to an enum does, and what leaves a database
    refusing a value the code believes it can store. What it does not catch is
    a changed threshold or comparison, where the literals are unchanged; that
    is a narrower miss than comparing nothing at all.
    """
    drifted = []
    for table, name, constraint in checks:
        found = present.get((table, name))
        if found is None:
            continue  # already reported as missing
        declared, actual = _literals(str(constraint.sqltext)), _literals(found)
        if declared != actual:
            drifted.append(
                f"{table}.{name} (models: {_listed(declared)}; "
                f"database: {_listed(actual)})"
            )
    return sorted(drifted)


#: A single-quoted SQL string literal, which is how a closed vocabulary is
#: written on both sides of the comparison.
_LITERAL = re.compile(r"'([^']*)'")


def _literals(definition: str) -> frozenset[str]:
    """Every string literal a constraint's definition mentions."""
    return frozenset(_LITERAL.findall(definition))


def _listed(values: frozenset[str]) -> str:
    """A literal set as readable text for an error message."""
    return ", ".join(sorted(values)) if values else "none"


def _missing_columns(engine: Engine) -> list[str]:
    """Columns the models declare and the database does not have.

    Only tables that already exist are considered: one that does not exist yet
    is about to be created, with everything on it.
    """
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    return sorted(
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        if table.name in existing
        for column in table.columns
        if column.name not in {found["name"] for found in inspector.get_columns(table.name)}
    )


@pytest.fixture
def clean(database: Database) -> None:
    """Empty every table before a test.

    TRUNCATE rather than DELETE: the append-only triggers reject DELETE on
    every table, which is the property they exist to enforce. Row triggers do
    not fire on TRUNCATE, so this is the one way state leaves the database, and
    it is available only to tests.
    """
    names = ", ".join(sorted(Base.metadata.tables))
    with database.engine.begin() as connection:
        # TRUNCATE needs the exclusive lock, so one leaked reader stops every
        # test after it. Unbounded, that surfaces as the *next* test hanging
        # until pytest-timeout kills it minutes later — a failure that names a
        # test which did nothing wrong and hides the one that did. Ten seconds
        # is longer than any honest contention here and short enough to read.
        connection.execute(text("SET LOCAL lock_timeout = '10s'"))
        try:
            connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        except OperationalError as error:
            raise RuntimeError(
                "the database is still locked by an earlier test. The usual "
                "cause is a session used after its `with ... read_only()` block "
                "closed: that checks out a connection nothing returns, leaving "
                "it idle in transaction and holding a read lock on whatever it "
                "read. It is the test before this one, not this one."
            ) from error


@pytest.fixture
def artifact_store(integration_settings: Settings) -> S3ArtifactStore:
    """The real object store, with its bucket present."""
    store = S3ArtifactStore(integration_settings)
    try:
        store.ensure_bucket()
    except Exception as error:  # pragma: no cover - depends on the stack
        pytest.skip(f"MinIO is not reachable; run scripts/dev_up.sh ({error})")
    return store


@pytest.fixture
def project(database: Database, clean: None) -> Project:
    """A project in CREATED status, with an owner who may direct it."""
    with database.transaction() as session:
        owner = UserRepository(session).create(username="owner", password_hash="x")
        project = ProjectRegistry(session).create(
            title="Catalyst screen", objective="Find a better dopant.", created_by=owner.user_id
        )
        MembershipRepository(session, project.project_id).grant(
            user_id=owner.user_id, role=UserRole.PROJECT_OWNER
        )
        return project


@pytest.fixture
def other_project(database: Database, project: Project) -> Project:
    """A second project, for testing that scopes do not leak into each other."""
    with database.transaction() as session:
        return ProjectRegistry(session).create(
            title="Unrelated", objective="Something else entirely.", created_by="someone"
        )


#: The outputs a contract requires when a caller does not name its own. Two of
#: them, so that a run delivering one short is visibly incomplete rather than
#: ambiguous.
DEFAULT_OUTPUTS = ("conductivity.csv", "notes.json")


@dataclass(frozen=True)
class Prepared:
    """A node that can be run, and the contracts it runs under."""

    node: DagNode
    #: The node's acceptance criteria, or `None` for a node type that has none.
    #: `None` rather than a contract nothing bound, because the lookup the
    #: system does is by node and would have found it either way.
    acceptance: AcceptanceContract | None
    contract: ExecutionContract

    @property
    def node_id(self) -> str:
        """The node's identifier, so a test does not have to reach through."""
        return self.node.node_id

    @property
    def project_id(self) -> str:
        return self.contract.project_id

    def request(self, *, attempt: int = 1) -> JobRequest:
        """The work a Worker would hand a backend for this node."""
        return JobRequest(
            project_id=self.contract.project_id,
            node_id=self.node.node_id,
            attempt=attempt,
            execution_contract_ref=self.contract.contract_id,
            execution_contract_version=self.contract.version,
            objective=self.contract.objective,
            required_outputs=self.contract.required_outputs,
            is_retry=attempt > 1,
        )


def build_prepared(
    session: Session,
    *,
    project_id: str,
    node_type: NodeType = NodeType.COMPUTATION,
    required_outputs: tuple[str, ...] = DEFAULT_OUTPUTS,
    allowed_actions: tuple[str, ...] = ("run_measurement",),
    allowed_ranges: dict[str, str] | None = None,
    allowed_substitutions: tuple[str, ...] = (),
    allowed_retries: int = 0,
    criteria: tuple[str, ...] = ("Conductivity rises by at least 15%.",),
    with_acceptance: bool = True,
    freeze_acceptance: bool = True,
    cleared: bool = True,
    parameter_targets: dict[str, str] | None = None,
    resource_limits: dict[str, str] | None = None,
    inputs: tuple[str, ...] = (),
    execution_requirements: dict[str, str] | None = None,
    procedure: str = "",
) -> Prepared:
    """Build a READY node with frozen contracts, the way production does.

    Assembled through the same repositories production uses, because the
    preconditions that let a node run are part of what is being tested: a
    fixture that wrote rows directly could hand a Worker a contract that was
    never frozen, and the run would then be exercising a state the system
    cannot reach.

    A function rather than only a fixture, because *which transaction* this
    runs in is not a detail. Most packages here take a session per operation
    and the fixture below is right for them; Master holds one session for the
    whole of a decision, so its tests have to build their nodes inside it. Two
    open transactions writing events for one project serialise on the event
    counter — which does not fail, it waits, and a test that waits reads as a
    hang rather than as a mistake about transactions.

    `with_acceptance=False` writes no acceptance criteria at all, which is what
    a RESEARCH node has. `freeze_acceptance=False` writes them and leaves them
    unfrozen, which is a state production can reach for a node whose criteria
    were revised and not yet re-frozen — and which the two are kept apart for:
    the system's lookup is by node, so a contract that exists is found whether
    or not the node's binding names it.

    `cleared=False` leaves a COMPUTATION or EXPERIMENT node without the
    pre-flight review it needs to enter RUNNING, which is the state a node is
    in while it waits to be reviewed. The default is the cleared one because
    "ready to run" is what almost every caller means by preparing a node, and
    the gate that refuses an uncleared one is asserted in
    `tests/integration/review/test_pre_run_gate.py` rather than here.
    """
    dag = DagRepository(session, project_id)
    node = dag.add_node(
        DagNode.create(
            project_id=project_id,
            node_type=node_type,
            objective="Measure conductivity across the dopant series.",
            created_by="master",
        ),
        role=AgentRole.MASTER,
        decision_ref="dec-1",
    )
    acceptance: AcceptanceContract | None = None
    if with_acceptance:
        acceptance = AcceptanceContract(
            project_id=project_id,
            node_id=node.node_id,
            criteria=tuple(
                AcceptanceCriterion(
                    statement=statement,
                    provenance=CriterionProvenance.USER_REQUIREMENT,
                )
                for statement in criteria
            ),
        )
        acceptance_repository = AcceptanceContractRepository(session, project_id)
        acceptance_repository.add(acceptance)
        if freeze_acceptance:
            acceptance_repository.freeze(acceptance.contract_id)
        dag.bind_acceptance_contract(node.node_id, acceptance.contract_id)

    contract = ExecutionContract(
        project_id=project_id,
        node_id=node.node_id,
        objective="Measure the conductivity of each sample.",
        # Empty by default, which is what a contract that names an environment
        # but no method is: `requires_preparation` is about
        # `execution_requirements`, so a caller that states one has to state
        # this too or the materializer refuses — see `LabMaterializer`.
        procedure=procedure,
        allowed_actions=allowed_actions,
        allowed_ranges=allowed_ranges or {},
        allowed_substitutions=allowed_substitutions,
        required_outputs=required_outputs,
        allowed_retries=allowed_retries,
        parameter_targets=dict(parameter_targets or {}),
        resource_limits=dict(resource_limits or {}),
        inputs=inputs,
        execution_requirements=dict(execution_requirements or {}),
    )
    contracts = ExecutionContractRepository(session, project_id)
    contracts.add(contract)
    contracts.freeze(contract.contract_id)
    dag.bind_execution_contract(node.node_id, contract.contract_id)

    ready = dag.transition_node(node.node_id, NodeStatus.READY, actor_id="scheduler")
    if cleared and requires_frozen_criteria(node_type):
        _clear_for_running(
            session,
            project_id=project_id,
            node=ready,
            acceptance=acceptance,
            contract=contract,
        )
    return Prepared(node=ready, acceptance=acceptance, contract=contract)


@pytest.fixture
def a_node(project: Project) -> Callable[..., DagNode]:
    """Build a node in this project, the way production builds one.

    Uncommitted: it returns the record a caller would hand to a mutation. That
    is what makes it usable for a *replacement* — the node Master decides to
    commit instead — as well as for a node a test is about to commit itself.

    Shared, because building a node is not specific to the package that plans
    them: the DAG tests plan with it, and Master's tests use it to describe
    terms written for a different node.
    """

    def build(
        node_type: NodeType = NodeType.RESEARCH,
        *,
        objective: str = "Survey the literature on the dopant series.",
        **overrides: object,
    ) -> DagNode:
        node = DagNode.create(
            project_id=project.project_id,
            node_type=node_type,
            objective=objective,
            created_by="master",
        )
        return node.model_copy(update=overrides) if overrides else node

    return build


@pytest.fixture
def prepare(database: Database, project: Project) -> Callable[..., Prepared]:
    """A runnable node, built and committed in a transaction of its own.

    Shared by three gates — the mock backends, the review gate, and the
    headless loop — because all three start from a node that is ready to run,
    and three copies of "how a runnable node is built" would be three places
    for that to drift.
    """

    def build(**options: object) -> Prepared:
        with database.transaction() as session:
            return build_prepared(
                session,
                project_id=project.project_id,
                **options,  # type: ignore[arg-type]
            )

    return build


#: When the sources these tests write were read. Fixed rather than `utcnow()`,
#: so that a claim's `retrieved_at` is compared against a value the test chose
#: instead of against whatever the clock said while it was running.
READ_AT = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def a_source(
    database: Database,
    project_id: str,
    *,
    url: str,
    tier: EvidenceSourceTier | None,
    access_status: AccessStatus = AccessStatus.OK,
) -> EvidenceSource:
    """One source row, written through the repository the gateway writes through.

    A source that was read carries a hash and the moment it was read; one RAVEL
    could not open carries neither, which is the distinction the ledger is built
    on and the one the domain type refuses to let anybody blur.

    Shared by the two suites that need a ledger with something in it — the
    Research one, where a claim is rated by its sources, and the Review one,
    where a record has to have been assembled from some. Offline either way:
    opening a URL is the live suite's job, and a rule that only holds when the
    network is up is not a rule.
    """
    read = access_status is AccessStatus.OK
    source = EvidenceSource(
        project_id=project_id,
        url=url,
        title=url.rstrip("/").rsplit("/", 1)[-1],
        access_status=access_status,
        retrieved_at=READ_AT if read else None,
        content_hash=f"sha256:{'ab' * 32}" if read else None,
        media_type="text/html" if read else None,
        tier=tier,
    )
    with database.transaction() as session:
        return EvidenceSourceRepository(session, project_id).record(source)


@pytest.fixture
def research_task(
    database: Database, project: Project, prepare: Callable[..., Prepared]
) -> Prepared:
    """A RESEARCH node under the research contract that says what was asked.

    Shared by the two suites that serve a research task through its tool server
    — the integration one and the live one — because the state a session is
    pointed at is the same state in both, and two definitions of it would be
    two places for "what the user asked for" to drift.

    A RESEARCH node has no acceptance criteria: that is what `with_acceptance`
    is off for. What it runs under is the Execution Contract, which `prepare`
    freezes.

    Written through `commit` rather than `add`, so the fixture goes through the
    door Master's tool goes through. This row used to have no writer in the
    product at all — only fixtures — which is how a live Research seat came to
    be refused the terms of its own task; a fixture that could still write one
    by a path nothing else uses would be keeping that gap open.
    """
    with database.transaction() as session:
        ResearchContractRepository(session, project.project_id).commit(
            ResearchContract(
                project_id=project.project_id,
                original_user_goal="Find a dopant that survives 500 hours under load.",
                scientific_problem="Which dopant keeps conductivity above the threshold?",
                research_hypotheses=("Niobium doping raises stability.",),
                target_metrics=("conductivity gain >= 15%",),
                acceptance_strategy="Measure the series and compare against the baseline.",
                known_constraints=("Bench time is limited.",),
                prohibited_actions=("No testing on live reactors.",),
            ),
            role=AgentRole.MASTER,
        )
    return prepare(node_type=NodeType.RESEARCH, with_acceptance=False)


def _clear_for_running(
    session: Session,
    *,
    project_id: str,
    node: DagNode,
    acceptance: AcceptanceContract | None,
    contract: ExecutionContract,
) -> None:
    """Submit the pre-flight PASS a node needs before it may run.

    Through `ReviewService`, not by writing a row: the gate reads the review
    the service accepted, and a fixture that wrote a PASS directly could hand a
    node a clearance the service would have refused — for instance one naming
    criteria that were never frozen. The definition of done it names is the
    same one the service will look up, which is the acceptance contract when
    there is one and the Execution Contract otherwise.
    """
    measured = acceptance if acceptance is not None else contract
    ReviewService(session, project_id).submit(
        ReviewRecord(
            project_id=project_id,
            node_id=node.node_id,
            checkpoint=ReviewCheckpoint.PRE_RUN,
            frozen_criteria_ref=measured.contract_id,
            frozen_criteria_version=measured.version,
            outcome=ReviewOutcome.PASS,
            diagnosis="The plan states what will be measured and how it will be run.",
        ),
        role=AgentRole.REVIEW,
    )
