"""Master's read surface over what the Research seat produced.

`research.py` is the seat's own surface: it searches, opens, reads, and writes
into the Evidence Ledger. This module is the other end of the same rows, and it
exists because of a gap in the chain the design already claims to implement:

```text
Master → Research Agent → Evidence / ResearchRecord → Master → Decision
```

The last arrow had no tool behind it. Master could read the whole project state
and could not read one research task's result, so the only ways to act on what a
task found were to have remembered the turn it was produced in — which a
replacement session does not have — or to plan the next node as though the
evidence were not there.

**Nothing here writes.** The Ledger and the records assembled from it are
Research's authorship, and what keeps that true is that no Master-facing tool
writes either one (`registry.WRITE_AUTHORSHIP`). A read surface that could also
register a claim would make the role that judges the evidence the role that
supplies it, which is exactly what the two seats are separated to prevent.

**What is read is the row, not a recollection of it.** Every field here comes
out of PostgreSQL in this call, so a Master that has read a result is holding
what the Research seat actually recorded rather than a summary of an earlier
conversation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from ravel.domain.dag import DagNode
from ravel.domain.decisions import ReviewRecord
from ravel.domain.enums import NodeType
from ravel.domain.state_machines import HANDED_OVER_NODE_STATUSES
from ravel.mcp.context import ToolContext, as_json, require_master
from ravel.mcp.tools.research import assessment_of
from ravel.research import sufficiency
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import RecordRepositories
from ravel.state.repositories.research import (
    EvidenceRepository,
    EvidenceSourceRepository,
    conflicts_for,
    task_ledger,
)


def _research_node(session: Session, project_id: str, node_id: str) -> DagNode:
    """The RESEARCH node a readback is about.

    Refused rather than accepted for another node type, for the reason the
    Research seat's own reader refuses one: what this returns is that node's
    ledger, and a COMPUTATION node has no ledger to return. Saying so is more
    use than an empty answer that reads like "the research found nothing".

    Raises:
        ValueError: The node is not a RESEARCH node.
    """
    node = DagRepository(session, project_id).node(node_id)
    if node.node_type is not NodeType.RESEARCH:
        raise ValueError(
            f"{node.display_id} is a {node.node_type.value} node, which has no "
            "research result: the Evidence Ledger belongs to the research tasks "
            "whose questions it answers. `list_research_results` names the nodes "
            "that do have one."
        )
    return node


def research_summary(
    session: Session,
    project_id: str,
    node: DagNode,
    *,
    review: ReviewRecord | None = None,
) -> dict[str, Any]:
    """One research task's outcome, as a summary rather than as its contents.

    The counts are here and the claims are not, because this is the shape the
    two reads that need it are read in: what Master needs first is which tasks
    have produced something, how strong it came back, and what verdict judged
    it. The claims themselves are `read_research_result`'s answer, and pushing
    them into a listing — or worse, into the project state every turn begins
    with — would put a project's whole evidence base through the model's
    context to answer a question about which node to read.

    Flat rather than nested, and named for what a reader is looking for, so
    that `read_project_state`'s `completed_research` and the listing behind
    `list_research_results` are one shape rather than two that can disagree
    about what a result looks like.

    `review` is the verdict to report, or `None` for a task nobody has judged.
    It is passed in because both callers already hold the reviews they want
    reported: the project state reads every review once for its `stopped` list,
    and a summary that queried them again would be the second read this
    function exists to avoid.
    """
    ledger = task_ledger(session, project_id, node.node_id)
    latest = ledger.records[-1] if ledger.records else None
    return {
        "node_id": node.node_id,
        "display_id": node.display_id,
        "objective": node.objective,
        "node_status": node.status.value,
        "roadmap_phase": node.roadmap_phase,
        # Whether the task has been handed over to the final checkpoint, which
        # is a different question from whether a record exists: a record is
        # written by the seat, and the hand-over is what asks Review to judge
        # it. A task that submitted nothing and was cancelled is handed over
        # neither way.
        "handed_over": node.status in HANDED_OVER_NODE_STATUSES,
        "research_record_ref": latest.research_id if latest is not None else None,
        "completion_status": (
            latest.completion_status.value if latest is not None else None
        ),
        "sufficiency": latest.sufficiency.sufficiency.value if latest is not None else None,
        "question": latest.question if latest is not None else None,
        "report_chars": len(latest.report) if latest is not None else 0,
        "claims": len(ledger.claims),
        "sources": len(ledger.sources),
        "conflicts": len(ledger.conflicts),
        # The verdict, when Review has given one. It is what a result is read
        # *through* rather than instead of: a result that PASSED met the terms
        # the task ran under, which says nothing about whether it supports the
        # plan Master is about to make, and a PARTIAL says the evidence did not
        # carry what the task was asked for.
        "review_checkpoint": review.checkpoint.value if review is not None else None,
        "review_outcome": review.outcome.value if review is not None else None,
        "review_diagnosis": review.diagnosis if review is not None else None,
    }


def completed_research(
    session: Session,
    project_id: str,
    nodes: Iterable[DagNode],
    *,
    reviews: Mapping[str, ReviewRecord],
) -> list[dict[str, Any]]:
    """The research tasks that have handed a result over, in the DAG's order.

    This is what `read_project_state` reports as `completed_research`, and the
    filter is the point of it: the state read is the one every turn begins
    with, so what it carries has to be the answer to "is there something to
    read" rather than every research task's row. A task still being worked on
    is not here — it is one entry of a status count in the DAG summary until it
    hands over, and `list_research_results` names it for a Master that wants to
    know what is under way.

    Handed over rather than "has a record", because the record is what the
    hand-over writes: the status is the fact the rest of the system acts on,
    and a listing that derived its membership from the fields it reports would
    be unable to report a task whose record is missing.
    """
    return [
        research_summary(session, project_id, node, review=reviews.get(node.node_id))
        for node in nodes
        if node.node_type is NodeType.RESEARCH and node.status in HANDED_OVER_NODE_STATUSES
    ]


def list_research_results(context: ToolContext) -> Any:
    """Which research tasks have produced something, and how strong it is."""

    async def list_research_results() -> dict[str, Any]:
        """List every research task in this project and what it delivered.

        Each entry names the node — pass its `node_id` to
        `read_research_result` — its status, whether a record was submitted,
        the completion status and sufficiency RAVEL judged it against, how many
        claims, sources and conflicts are in its ledger, and the verdict Review
        has given it if any.

        `research_record_ref: null` on a node that is RUNNING means the task is
        still being worked on. The same null on a node that has ended means it
        was ended without one — by a cancellation, or by a decision of yours —
        and there is nothing to read.

        Read this before planning work that depends on what research found, and
        then read the result itself: the counts say how much there is, not what
        it says.
        """
        require_master(context, "list_research_results")
        with context.read() as session:
            nodes = [
                node
                for node in DagRepository(session, context.project_id).nodes()
                if node.node_type is NodeType.RESEARCH
            ]
            reviews = RecordRepositories(session, context.project_id).reviews
            results = [
                research_summary(
                    session,
                    context.project_id,
                    node,
                    review=reviews.latest_for_node(node.node_id),
                )
                for node in nodes
            ]

        return {
            "project_id": context.project_id,
            "results": results,
            "with_a_record": len(
                [one for one in results if one["research_record_ref"] is not None]
            ),
            "note": (
                None
                if results
                else (
                    "this project has no research task. If the plan needs evidence "
                    "rather than an assumption, the node that produces it is a "
                    "RESEARCH node, and it is planned like any other work."
                )
            ),
        }

    return list_research_results


def read_research_result(context: ToolContext) -> Any:
    """One research task's result in full, with everything it rests on."""

    async def read_research_result(node_id: str) -> dict[str, Any]:
        """Read what one research task found, and what supports it.

        `result` is the structured Research Record as the seat submitted it —
        its facts, inferences and hypotheses, the completion status RAVEL
        judged it against, the sufficiency assessment with its measurements and
        gaps, and the unknowns the task could not settle. When no record has
        been submitted, `result` is null and `sufficiency` is measured live
        from the ledger, so a task still under way is readable rather than
        blank.

        `claims` are the Evidence rows the record was assembled from, each with
        its class, tier, confidence, conditions and the ids of the sources it
        rests on. `sources` are those sources' ledger rows: where each came
        from, when it was retrieved, the hash of the bytes, and whether RAVEL
        obtained the content or only the metadata. `conflicts` are the
        disagreements the seat recorded, with whatever resolution a decision
        gave them.

        **This is provenance, not a licence.** What is here is what Research
        read and wrote down. Nothing in it may be replaced by your own
        recollection, and a claim you need that is not here is a research task
        rather than a line to add. `reviews` is the verdicts given about this
        node, so a result can be read together with the judgement of it.
        """
        require_master(context, "read_research_result")
        with context.read() as session:
            node = _research_node(session, context.project_id, node_id)
            ledger = task_ledger(session, context.project_id, node.node_id)
            reviews = RecordRepositories(session, context.project_id).reviews.for_node(
                node.node_id
            )
            # Measured now rather than read from the record: a task that has
            # not submitted one still has evidence, and Master deciding whether
            # to wait for more should see what is there today.
            live = sufficiency.assess(
                claims=list(ledger.claims),
                sources=list(ledger.sources),
                conflicts=list(ledger.conflicts),
            )

        latest = ledger.records[-1] if ledger.records else None
        return {
            "node": {
                "node_id": node.node_id,
                "display_id": node.display_id,
                "objective": node.objective,
                "status": node.status.value,
                "roadmap_phase": node.roadmap_phase,
                "execution_contract_ref": node.execution_contract_ref,
                "started_at": node.started_at.isoformat() if node.started_at else None,
                "completed_at": (
                    node.completed_at.isoformat() if node.completed_at else None
                ),
            },
            "result": as_json(latest) if latest is not None else None,
            "records_submitted": len(ledger.records),
            "claims": as_json(ledger.claims),
            "sources": as_json(ledger.sources),
            "conflicts": as_json(ledger.conflicts),
            "sufficiency": assessment_of(live),
            "reviews": [
                {
                    "review_id": review.review_id,
                    "display_id": review.display_id,
                    "checkpoint": review.checkpoint.value,
                    "outcome": review.outcome.value,
                    "frozen_criteria_ref": review.frozen_criteria_ref,
                    "frozen_criteria_version": review.frozen_criteria_version,
                    "diagnosis": review.diagnosis,
                    "recommendations": list(review.recommendations),
                    "created_at": review.created_at.isoformat(),
                }
                for review in reviews
            ],
            "has_result": latest is not None,
        }

    return read_research_result


def read_evidence(context: ToolContext) -> Any:
    """One claim in the ledger, and the sources it rests on."""

    async def read_evidence(evidence_id: str) -> dict[str, Any]:
        """Read one Evidence row and the sources that support it.

        Use it when a claim inside a result is the one a decision turns on and
        its wording matters. `claim` is the row as recorded — the statement,
        its class, the tier and access status RAVEL read off its sources, the
        conditions it holds under, and what it conflicts with. `sources` are
        those sources' rows, named by the same ids the claim carries, and
        `conflicts` are the recorded disagreements this claim is part of.

        `evidence_id` is the id from a claim in `read_research_result`. It is
        not a display id: the ledger's identifiers are what every other record
        refers to a claim by.
        """
        require_master(context, "read_evidence")
        with context.read() as session:
            claim = EvidenceRepository(session, context.project_id).get(
                evidence_id=evidence_id
            )
            sources = EvidenceSourceRepository(session, context.project_id)
            supported = [sources.get(source_id=ref) for ref in claim.source_refs]
            conflicts = conflicts_for(session, context.project_id, [claim])

        return {
            "claim": as_json(claim),
            "sources": as_json(supported),
            "conflicts": as_json(conflicts),
        }

    return read_evidence


def read_source_metadata(context: ToolContext) -> Any:
    """What RAVEL recorded about one source, without its text."""

    async def read_source_metadata(source_id: str) -> dict[str, Any]:
        """Read one source's ledger row: what it is and how it was reached.

        The row carries the requested and final URL, the title and publisher,
        the DOI, the media type, the time it was retrieved, the hash of the
        bytes RAVEL read, the tier RAVEL assigned it, and whether the content
        itself was obtained or only its metadata. `has_snapshot` says whether a
        stored copy exists, which is the difference between a source that can
        be read again and a citation of something nobody can.

        **No text is returned.** What a source says is read by the seat whose
        task the source answers, and a passage worth quoting is worth a
        research task that quotes it in the ledger. This answers whether the
        source behind a claim is the kind of thing it is presented as.
        """
        require_master(context, "read_source_metadata")
        with context.read() as session:
            source = EvidenceSourceRepository(session, context.project_id).get(
                source_id=source_id
            )
            citing = EvidenceRepository(session, context.project_id).supported_by(
                source_id
            )

        return {
            "source": as_json(source),
            "cited_by": [claim.evidence_id for claim in citing],
            "was_read": source.was_read,
            "has_snapshot": source.snapshot_ref is not None,
        }

    return read_source_metadata


IMPLEMENTATIONS: dict[str, Any] = {
    "list_research_results": list_research_results,
    "read_research_result": read_research_result,
    "read_evidence": read_evidence,
    "read_source_metadata": read_source_metadata,
}
