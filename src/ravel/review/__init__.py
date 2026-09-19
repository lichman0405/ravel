"""The Review gate: what a verdict is measured against, and what it does.

`docs/02_AGENT_MODEL.md` gives Review three powers and three prohibitions. It
may accept, diagnose, and advise; it may not mutate the DAG, change an
Execution Contract, or alter acceptance criteria. Both halves of that are in
this package: `checkpoints` decides what a node owes and whether it has been
cleared, and `service` writes the record and applies the one part of a verdict
that is Review's to apply.

What is *not* here is a reviewer. Review is one of the five agent roles and its
judgement is a model's; this package is the part that has to be mechanical —
which criteria were frozen, whether all of them were answered, where a node
goes when the answer is no.
"""

from ravel.review.checkpoints import (
    PRE_RUN_NODE_TYPES,
    Clearance,
    NotClearedError,
    latest_at,
    pre_run_clearance,
    required_checkpoints,
)
from ravel.review.service import ReviewError, ReviewService, SubmittedReview

__all__ = [
    "PRE_RUN_NODE_TYPES",
    "Clearance",
    "NotClearedError",
    "ReviewError",
    "ReviewService",
    "SubmittedReview",
    "latest_at",
    "pre_run_clearance",
    "required_checkpoints",
]
