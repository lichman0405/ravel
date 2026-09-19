"""a node can wait on master before it runs

`dag_nodes` requires a `started_at` for every status that implies work has
begun, and `WAITING_DECISION` was on that list. It was on the list because it
had only ever been reachable one way: a Worker mid-run asks something only
Master can answer, so a node waiting on a decision had always started.

A refused pre-flight review is the second way in. Review reads a COMPUTATION
or EXPERIMENT node's frozen criteria before it runs, and a refusal parks the
node at `WAITING_DECISION` — that is the status for "blocked on a Master
decision", and the alternative was leaving it at READY where the clearance
gate would hold it invisibly. Such a node has not started and has no start
moment to record, and the constraint left two ways out, both worse than
correcting it:

- write a `started_at` the node did not have, which puts a falsehood in the
  authoritative record of when work began;
- move it to `BLOCKED` instead, which already means an unmet dependency and
  would trade one conflation for another.

What the constraint is for survives intact: a node that is `RUNNING`, is
waiting on something external, is under review, or has a result, still cannot
exist without a start time. What goes is the accidental part — that being
parked on a decision could only happen after starting.

`WAITING_DECISION` is the only value removed, and the only one that has a
route into it from a status with no start time.

Revision ID: 9e4c7b12af03
Revises: 3d9a6f01b7c2
Created: 2026-09-19 09:50:41.226883
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "9e4c7b12af03"
down_revision: str | None = "3d9a6f01b7c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The constraint as the database knows it. Written out in full and marked with
#: `op.f`, as the initial migration does: the name is composed from the table
#: and the constraint through `NAMING_CONVENTION`, and passing the composed
#: name back through that same convention would prefix it a second time.
CONSTRAINT = op.f("ck_dag_nodes_started_nodes_have_a_start_time")

#: Statuses that mean work began. `WAITING_DECISION` is not among them.
STARTED = "'RUNNING', 'WAITING_EXTERNAL', 'REVIEWING', 'PASSED', 'FAILED', 'PARTIAL'"

#: The list this replaces, which included `WAITING_DECISION`.
BEFORE = f"{STARTED}, 'WAITING_DECISION'"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "dag_nodes", type_="check")
    op.create_check_constraint(
        CONSTRAINT,
        "dag_nodes",
        f"status NOT IN ({STARTED}) OR started_at IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "dag_nodes", type_="check")
    # Reinstating the narrower rule over the existing rows, so a node parked
    # before it ran fails the migration instead of being given a start time it
    # never had. Failing is the point: the choice between those two belongs to
    # whoever is rolling back, not to this script.
    op.create_check_constraint(
        CONSTRAINT,
        "dag_nodes",
        f"status NOT IN ({BEFORE}) OR started_at IS NOT NULL",
    )
