"""an attempt belongs to a contract version

`backend_jobs` was keyed `(project_id, node_id, attempt)`, which reads as
"the second time this node's work was handed to a backend". The Execution
Record reads the same word differently and more narrowly: it holds the attempts
of *one run*, and it refuses any list that is not numbered from one, because
"attempt 3 of 2" is not something a reader can interpret.

Both readings were fine until a node could run twice. Answering a Worker's
escalation with a revised contract is an instruction to run the node again, and
that run's first attempt is the first attempt of the work the new contract
describes — but under the old key it would have been numbered after the
previous run's, and the record it produced would have been refused by the
domain model. The failure was not silent, which is how it was found; it was
also not a fault in the record's rule, which is the older and more principled
of the two.

So the version joins the key. An attempt number now means the same thing in the
job row, in the Execution Record, and in the workflow: the nth try at the work
described by one version of one node's contract.

Revision ID: 2b8e4f7c1a35
Revises: 7c1d9b40e6a2
Created: 2026-09-19 11:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "2b8e4f7c1a35"
down_revision: str | None = "7c1d9b40e6a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "backend_jobs"


def _constraint() -> str:
    """The constraint as the database knows it.

    `one_job_per_attempt` is the name the table declares, and `op.f` marks it
    as already final so `NAMING_CONVENTION` is not applied to it a second time.
    A function rather than a module constant because `op` is a proxy — calling
    it while the revisions are being loaded, before any migration is running,
    raises instead of returning a name, and it would take `alembic history`
    down with it.
    """
    return op.f("one_job_per_attempt")


def upgrade() -> None:
    """Widen the key so a revised contract's run counts its own attempts."""
    op.drop_constraint(_constraint(), TABLE, type_="unique")
    op.create_unique_constraint(
        _constraint(),
        TABLE,
        ["project_id", "node_id", "execution_contract_version", "attempt"],
    )


def downgrade() -> None:
    """Put the node-wide key back.

    This fails on a database where a node has run under two versions, and it
    should: the rows it would have to reconcile are two attempts numbered one,
    and collapsing them would mean deciding which of the two runs was not work.
    An operator rolling back past this revision has to say what happens to
    those rows rather than be told afterwards.
    """
    op.drop_constraint(_constraint(), TABLE, type_="unique")
    op.create_unique_constraint(
        _constraint(), TABLE, ["project_id", "node_id", "attempt"]
    )
