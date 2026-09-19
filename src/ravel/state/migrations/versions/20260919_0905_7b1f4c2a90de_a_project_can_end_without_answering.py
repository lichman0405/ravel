"""a project can end without answering the question

`acceptance/V0_ACCEPTANCE.md` A20 requires a project to reach one of four
outcomes: SUCCESS, FAILED, INCONCLUSIVE, or TERMINATED. Three of those the
schema already had. The fourth it did not, and the gap was not cosmetic: a
project whose budget ran out, or whose evidence never became sufficient, had
only `COMPLETED` and `FAILED` to end on, and both of them say something about
the science that is not true. `COMPLETED` says the question was answered;
`FAILED` says the answer was no. "We do not know" is neither, and it is the
most common honest ending a research project has.

`INCONCLUSIVE` is added to the vocabulary and to the two statuses a project
can reach an ending from. It is terminal, for the same reason the others are:
a project that concluded nothing does not later conclude something — that is a
new project, with its own contracts and its own record.

The constraint is replaced rather than dropped and left off. The whole point of
a closed vocabulary is that the database refuses a value nobody has thought
about, and a migration that widened the enum by removing the rule would make
the next typo a row instead of an error.

Revision ID: 7b1f4c2a90de
Revises: cae8d84d9792
Created: 2026-09-19 09:05:44.118092
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "7b1f4c2a90de"
down_revision: str | None = "cae8d84d9792"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The vocabulary before and after this revision. Spelled out rather than
#: imported from `ravel.domain.enums`, because a migration has to describe the
#: schema as it was at this revision: an enum that gains a sixth ending in a
#: year would otherwise rewrite history and make this step unreplayable.
BEFORE = "'CREATED', 'CONTRACT_DEFINED', 'EXECUTING', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED'"
AFTER = (
    "'CREATED', 'CONTRACT_DEFINED', 'EXECUTING', 'PAUSED', 'COMPLETED', 'FAILED', "
    "'INCONCLUSIVE', 'CANCELLED'"
)

#: The constraint as the database knows it. Written out in full and marked with
#: `op.f`, as the initial migration does: the name is composed from the table
#: and the constraint through `NAMING_CONVENTION`, and passing the composed
#: name back through that same convention would prefix it a second time.
CONSTRAINT = op.f("ck_projects_status_is_known")


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "projects", type_="check")
    op.create_check_constraint(CONSTRAINT, "projects", f"status IN ({AFTER})")


def downgrade() -> None:
    """Narrow the vocabulary back, refusing if any project uses the new ending.

    The reverse is not a rewrite of history and must not become one. A project
    that ended `INCONCLUSIVE` says something a rollback would have to delete,
    and there is no honest way to choose between `COMPLETED` and `FAILED` on
    its behalf — so the constraint is reinstated over the existing rows and the
    `ALTER` fails, naming the rows, if any of them hold the value. An operator
    who genuinely wants to go back has to decide what those projects concluded.
    """
    op.drop_constraint(CONSTRAINT, "projects", type_="check")
    op.create_check_constraint(CONSTRAINT, "projects", f"status IN ({BEFORE})")
