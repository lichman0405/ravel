"""Where RAVEL's recoveries from dead runs are kept.

One repository, one table, and two things it does that the base class does not.

**`record` is an insert that conflicts rather than a check followed by an
insert.** The reconciler can be running in one supervisor and a second
supervisor can be started before the first is stopped — the deployment runs one,
but nothing in the database says so — and two scans that both find the same
stranded node must produce one reconciliation, not two. The uniqueness is the
`one_reconciliation_per_run` constraint's, decided by PostgreSQL, for the same
reason `BackendJobRepository.start` decides its own that way.

**A second record for one run is returned rather than raised on.** That is the
opposite of what `BackendJobRepository.start` does with a conflicting insert,
and the difference is what the two calls mean. Starting a job twice with
different work is a contradiction somebody has to look at; finding one stranded
run twice is the *expected* result of scanning on a timer, and the correct
answer is the record the first scan wrote.

**No event is emitted, and the omission is deliberate.** The project event
vocabulary is closed — `schemas/project_events.yaml` names its members, and
`ProjectEventType` is a transcription of that file — so inventing
`EXECUTION_RECONCILED` here would put a value in the stream that the schema
does not define and the TUI cannot be expected to render. What the stream does
show is the node's own move (`NODE_WAITING`, emitted by the transition that
made it), and what the reconciler saw is in this table, which is where a reader
who wants the reason should look. `WorkerMessageRepository` records its rows
under the same rule and for the same reason.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from ravel.domain.reconciliation import RunReconciliation
from ravel.state.mapping import to_row_data
from ravel.state.repositories.base import ProjectScopedRepository
from ravel.state.tables import RunReconciliationRow


class RunReconciliationRepository(ProjectScopedRepository[RunReconciliation]):
    """Every run this project lost, and what RAVEL did about each."""

    row_type = RunReconciliationRow
    record_type = RunReconciliation

    def _order_by(self) -> Any:
        """When RAVEL found it.

        The base class sorts by `created_at`, which is the right column and
        still has to be named: this record has no other timestamp, and the
        ordering is the order the losses were discovered rather than the order
        the runs were started.
        """
        return self.row_type.created_at

    def for_run(
        self, node_id: str, execution_contract_version: int
    ) -> RunReconciliation | None:
        """The record for one run of one node, if it has already been found."""
        return self._one(
            node_id=node_id, execution_contract_version=execution_contract_version
        )

    def for_node(self, node_id: str) -> list[RunReconciliation]:
        """Every run of one node that was found dead, oldest first."""
        return self.all(node_id=node_id)

    def record(self, reconciliation: RunReconciliation) -> RunReconciliation:
        """Write the record, or return the one a previous scan wrote.

        Raises:
            ProjectScopeError: The record belongs to a different project. The
                check is explicit because this method writes its own INSERT
                rather than going through `add`, so the guard every other write
                passes is not on this path by default.
        """
        self._authorize(reconciliation)
        statement = (
            pg_insert(RunReconciliationRow)
            .values(**to_row_data(reconciliation, RunReconciliationRow))
            .on_conflict_do_nothing(
                index_elements=["project_id", "node_id", "execution_contract_version"]
            )
        )
        self.session.execute(statement)
        stored = self.for_run(
            reconciliation.node_id, reconciliation.execution_contract_version
        )
        assert stored is not None  # the insert cannot vanish
        return stored
