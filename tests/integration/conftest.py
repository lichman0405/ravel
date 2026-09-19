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

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import CheckConstraint, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

from ravel.config import Settings
from ravel.domain.contracts import (
    AcceptanceContract,
    AcceptanceCriterion,
    CriterionProvenance,
    ExecutionContract,
)
from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, NodeType, UserRole
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.execution.backends import JobRequest
from ravel.state import guards
from ravel.state.database import Database, create_db_engine, install_utc_guard
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.store import S3ArtifactStore
from ravel.state.tables import Base

#: Every integration test truncates on the way in, so the database name is a
#: safety interlock rather than a convention.
TEST_DATABASE_SUFFIX = "_test"


@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    """Settings pointed at the dedicated test database.

    Raises:
        RuntimeError: The configured database is not a test database.
    """
    # The DSN override is addressed by its alias, which is the name pydantic
    # actually accepts: an ambient RAVEL_POSTGRES_DSN must not redirect a
    # suite that truncates every table.
    settings = Settings(env="test", postgres_db="ravel_test", RAVEL_POSTGRES_DSN=None)
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
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


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


@pytest.fixture
def prepare(database: Database, project) -> Callable[..., Prepared]:
    """Build a READY node with frozen contracts, the way production does.

    Assembled through the same repositories production uses, because the
    preconditions that let a node run are part of what is being tested: a
    fixture that wrote rows directly could hand a Worker a contract that was
    never frozen, and the run would then be exercising a state the system
    cannot reach.

    Shared by three gates — the mock backends, the review gate, and the
    headless loop — because all three start from a node that is ready to run,
    and three copies of "how a runnable node is built" would be three places
    for that to drift.
    """

    def build(
        *,
        node_type: NodeType = NodeType.COMPUTATION,
        required_outputs: tuple[str, ...] = DEFAULT_OUTPUTS,
        allowed_actions: tuple[str, ...] = ("run_measurement",),
        allowed_ranges: dict[str, str] | None = None,
        allowed_substitutions: tuple[str, ...] = (),
        allowed_retries: int = 0,
        criteria: tuple[str, ...] = ("Conductivity rises by at least 15%.",),
        with_acceptance: bool = True,
        freeze_acceptance: bool = True,
    ) -> Prepared:
        """Build one.

        `with_acceptance=False` writes no acceptance criteria at all, which is
        what a RESEARCH node has. `freeze_acceptance=False` writes them and
        leaves them unfrozen, which is a state production can reach for a node
        whose criteria were revised and not yet re-frozen — and which the two
        are kept apart for: the system's lookup is by node, so a contract that
        exists is found whether or not the node's binding names it.
        """
        with database.transaction() as session:
            dag = DagRepository(session, project.project_id)
            node = dag.add_node(
                DagNode.create(
                    project_id=project.project_id,
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
                    project_id=project.project_id,
                    node_id=node.node_id,
                    criteria=tuple(
                        AcceptanceCriterion(
                            statement=statement,
                            provenance=CriterionProvenance.USER_REQUIREMENT,
                        )
                        for statement in criteria
                    ),
                )
                acceptance_repository = AcceptanceContractRepository(
                    session, project.project_id
                )
                acceptance_repository.add(acceptance)
                if freeze_acceptance:
                    acceptance_repository.freeze(acceptance.contract_id)
                dag.bind_acceptance_contract(node.node_id, acceptance.contract_id)

            contract = ExecutionContract(
                project_id=project.project_id,
                node_id=node.node_id,
                objective="Measure the conductivity of each sample.",
                allowed_actions=allowed_actions,
                allowed_ranges=allowed_ranges or {},
                allowed_substitutions=allowed_substitutions,
                required_outputs=required_outputs,
                allowed_retries=allowed_retries,
            )
            contracts = ExecutionContractRepository(session, project.project_id)
            contracts.add(contract)
            contracts.freeze(contract.contract_id)
            dag.bind_execution_contract(node.node_id, contract.contract_id)

            return Prepared(
                node=dag.transition_node(
                    node.node_id, NodeStatus.READY, actor_id="scheduler"
                ),
                acceptance=acceptance,
                contract=contract,
            )

    return build
