"""Contracts: the research goal, the authority envelope, and the two freezes.

The two freezes are the ones that make results meaningful. An acceptance
contract defines success *before* a computation runs, and an execution contract
defines what a Worker may do *before* it starts. Both are frozen by a method
that is idempotent and one-way — `freeze` on an already-frozen contract returns
it unchanged rather than faulting, so a retried activity is safe, and there is
no `unfreeze` for a caller to reach for.

Freezing emits no event. The vocabulary in `ProjectEventType` is closed and
none of its members means "criteria frozen"; the contract row, with its
`frozen_at`, is the record, and the node's own `NODE_STARTED` follows it. Adding
an event type to say this would be inventing vocabulary the schema did not ask
for.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ravel.domain.contracts import (
    AcceptanceContract,
    AuthorityEnvelope,
    ExecutionContract,
    ProjectSuccessContract,
    ResearchContract,
)
from ravel.state.mapping import from_row
from ravel.state.repositories.base import NotFound, ProjectScopedRepository
from ravel.state.tables import (
    AcceptanceContractRow,
    AuthorityEnvelopeRow,
    ExecutionContractRow,
    ProjectSuccessContractRow,
    ResearchContractRow,
)


class _Versioned(ProjectScopedRepository[Any]):
    """A contract family that gains a new version instead of being edited."""

    def latest(self) -> Any:
        """The highest-numbered version in this project.

        Raises:
            NotFound: No contract of this kind exists yet.
        """
        row = (
            self.session.execute(
                self._scoped().order_by(self.row_type.version.desc()).limit(1)
            )
            .scalars()
            .one_or_none()
        )
        if row is None:
            raise NotFound(f"no {self.row_type.__name__} in {self.project_id}")
        return from_row(self.record_type, row)

    def version(self, number: int) -> Any:
        """One specific version.

        Raises:
            NotFound: This project has no such version.
        """
        return self.get(version=number)

    def next_version(self) -> int:
        """The version number a new contract in this family should carry."""
        highest = self.session.execute(
            select(self.row_type.version)
            .where(self.row_type.project_id == self.project_id)
            .order_by(self.row_type.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        return int(highest or 0) + 1


class ResearchContractRepository(ProjectScopedRepository[ResearchContract]):
    """What the user asked for. One per project, immutable once written."""

    row_type = ResearchContractRow
    record_type = ResearchContract

    def current(self) -> ResearchContract:
        """The project's research contract.

        Raises:
            NotFound: The project has no research contract yet.
        """
        rows = self.all()
        if not rows:
            raise NotFound(f"project {self.project_id} has no research contract")
        if len(rows) > 1:
            raise RuntimeError(
                f"project {self.project_id} has {len(rows)} research contracts; a "
                "change to what the user wants is a new project, not a second contract"
            )
        return rows[0]


class SuccessContractRepository(_Versioned):
    """What project success and failure mean."""

    row_type = ProjectSuccessContractRow
    record_type = ProjectSuccessContract

    def add_version(self, contract: ProjectSuccessContract) -> ProjectSuccessContract:
        """Record a new version of the success definition."""
        return self.add(contract)


class AuthorityEnvelopeRepository(_Versioned):
    """What Master may decide alone."""

    row_type = AuthorityEnvelopeRow
    record_type = AuthorityEnvelope

    def add_version(self, envelope: AuthorityEnvelope) -> AuthorityEnvelope:
        """Record a new envelope. The newest governs."""
        return self.add(envelope)


class AcceptanceContractRepository(ProjectScopedRepository[AcceptanceContract]):
    """Frozen success criteria, per node."""

    row_type = AcceptanceContractRow
    record_type = AcceptanceContract

    def _order_by(self) -> Any:
        return AcceptanceContractRow.version

    def for_node(self, node_id: str, *, version: int | None = None) -> AcceptanceContract:
        """A node's acceptance contract, newest by default.

        Raises:
            NotFound: The node has no such contract.
        """
        statement = self._scoped(AcceptanceContractRow.node_id == node_id)
        if version is not None:
            statement = statement.where(AcceptanceContractRow.version == version)
        row = (
            self.session.execute(statement.order_by(AcceptanceContractRow.version.desc()))
            .scalars()
            .first()
        )
        if row is None:
            raise NotFound(f"node {node_id} has no acceptance contract")
        return from_row(AcceptanceContract, row)

    def frozen_for_node(self, node_id: str) -> AcceptanceContract | None:
        """The node's frozen contract, or `None` if none has been frozen."""
        row = (
            self.session.execute(
                self._scoped(
                    AcceptanceContractRow.node_id == node_id,
                    AcceptanceContractRow.frozen_at.is_not(None),
                ).order_by(AcceptanceContractRow.version.desc())
            )
            .scalars()
            .first()
        )
        return from_row(AcceptanceContract, row) if row is not None else None

    def freeze(self, contract_id: str) -> AcceptanceContract:
        """Freeze a contract. Idempotent.

        Raises:
            NotFound: This project has no such contract.
        """
        contract = self.get(contract_id=contract_id)
        if contract.is_frozen:
            return contract
        frozen = contract.freeze()
        row = self.session.get(AcceptanceContractRow, contract_id)
        assert row is not None
        row.frozen_at = frozen.frozen_at
        return frozen


class ExecutionContractRepository(ProjectScopedRepository[ExecutionContract]):
    """What one Worker may do, frozen before it starts."""

    row_type = ExecutionContractRow
    record_type = ExecutionContract

    def _order_by(self) -> Any:
        return ExecutionContractRow.version

    def for_node(self, node_id: str, *, version: int | None = None) -> ExecutionContract:
        """A node's execution contract, newest by default.

        Raises:
            NotFound: The node has no such contract.
        """
        statement = self._scoped(ExecutionContractRow.node_id == node_id)
        if version is not None:
            statement = statement.where(ExecutionContractRow.version == version)
        row = (
            self.session.execute(statement.order_by(ExecutionContractRow.version.desc()))
            .scalars()
            .first()
        )
        if row is None:
            raise NotFound(f"node {node_id} has no execution contract")
        return from_row(ExecutionContract, row)

    def freeze(self, contract_id: str) -> ExecutionContract:
        """Freeze a contract. Idempotent.

        Raises:
            NotFound: This project has no such contract.
        """
        contract = self.get(contract_id=contract_id)
        if contract.is_frozen:
            return contract
        frozen = contract.freeze()
        row = self.session.get(ExecutionContractRow, contract_id)
        assert row is not None
        row.frozen_at = frozen.frozen_at
        return frozen

    def permits(self, node_id: str, action: str, *, version: int | None = None) -> bool:
        """Whether the node's contract explicitly names an action as permitted.

        An action the contract does not name is forbidden, not "probably fine".
        """
        contract = self.for_node(node_id, version=version)
        if not contract.is_frozen:
            raise ValueError(
                f"node {node_id}'s execution contract is not frozen; a Worker may "
                "not act under terms that could still change"
            )
        return contract.permits(action)
