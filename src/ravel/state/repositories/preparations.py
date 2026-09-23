"""Where RAVEL's prepared workspaces are recorded.

One repository, one table, and one thing it does that the base class does not:
**the newest preparation for a run is what a reader wants**, and there may be
more than one. The execution loop prepares before it starts a job, an attempt
that refused may be followed by one that prepared, and a host that changed
under a contract produces a second manifest for the same terms. None of those
is a duplicate to be collapsed — they are what happened, in order — so nothing
here deduplicates and nothing here updates: a preparation is written once and
read back as history.

The two reads are therefore both about *finding* rather than about enforcing. A
Worker asks what was built for the run it is about to start and gets the newest;
Master asks what RAVEL tried for a node and gets all of it, which is what makes
a node that refused twice and then succeeded legible as exactly that.
"""

from __future__ import annotations

from typing import Any

from ravel.domain.preparation import PreparationOutcome, PreparationRecord
from ravel.state.repositories.base import ProjectScopedRepository
from ravel.state.tables import PreparationRow


class PreparationRepository(ProjectScopedRepository[PreparationRecord]):
    """Every workspace this project's contracts were materialized into."""

    row_type = PreparationRow
    record_type = PreparationRecord

    def _order_by(self) -> Any:
        """When RAVEL built it.

        The base class sorts by `created_at`, which is the right column and
        still has to be named: this record has no other timestamp, and the
        orderings that matter below are variations on it.
        """
        return self.row_type.created_at

    def for_node(self, node_id: str) -> list[PreparationRecord]:
        """Everything RAVEL prepared for one node, oldest first."""
        return self.all(node_id=node_id)

    def for_run(
        self, node_id: str, execution_contract_version: int
    ) -> list[PreparationRecord]:
        """Everything RAVEL prepared for one run of one node, oldest first."""
        return self.all(
            node_id=node_id, execution_contract_version=execution_contract_version
        )

    def latest_for_run(
        self, node_id: str, execution_contract_version: int
    ) -> PreparationRecord | None:
        """The last thing RAVEL prepared for one run, or `None` if nothing was.

        The newest rather than the first, because a retried preparation is the
        one whose workspace exists: an attempt that refused wrote nothing, and
        the manifest a Worker needs is the one that succeeded.
        """
        records = self.for_run(node_id, execution_contract_version)
        return records[-1] if records else None

    def stopping_refusal(self, node_id: str) -> PreparationRecord | None:
        """The refusal that is stopping this node now, or `None` if none is.

        Across contract versions, unlike the two reads above, because the caller
        is answering "why is this node stopped" for a node whose run it did not
        watch: a node whose terms were revised and whose new run then refused
        has its answer under the new version, and a read that asked for a
        version the caller had to guess at would report the wrong run.

        **The newest record has to be the refusal.** A refusal that was later
        followed by a preparation is history — Master revised the terms, the
        next run built its workspace and went on — and reporting it would answer
        "why has this stopped" with the reason something else stopped earlier.
        That is the same mistake as reporting a `PASS` as a node's verdict, and
        it is reachable the same way: a node that refused at version one and was
        parked at version two by a lost run has both records, and only the
        second is about where it is now.
        """
        records = self.for_node(node_id)
        if records and records[-1].outcome is PreparationOutcome.REFUSED:
            return records[-1]
        return None

    def prepared_for_run(
        self, node_id: str, execution_contract_version: int
    ) -> PreparationRecord | None:
        """The newest *prepared* workspace for a run, ignoring refusals.

        Separate from `latest_for_run` for the case the difference is about: a
        run that prepared and was then prepared again by a host that could not
        do it would have a refusal as its newest record, and a Worker asking
        where to run must not be sent to a refusal.
        """
        for record in reversed(self.for_run(node_id, execution_contract_version)):
            if record.outcome is PreparationOutcome.PREPARED:
                return record
        return None

    def record(self, preparation: PreparationRecord) -> PreparationRecord:
        """Write a preparation.

        Raises:
            ProjectScopeError: The record belongs to a different project.
        """
        return self.add(preparation)
