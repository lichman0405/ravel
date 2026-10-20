"""Assemble a report from ledger rows, deterministically and without repair.

The builder does two jobs and refuses a third.

It **indexes** the project's rows — executions, backend jobs, artifacts and
their versions, reviews, deviations, evidence and sources — and renders counts
and an evidence index from them. Those counts are computed before any Master
commentary exists, and they are stored in the report row rather than recomputed
on read, so a later ledger change cannot alter a report that was already
written.

It **checks** every citation: the identifier must exist in this project, an
asserted hash must equal the ledger's, a source citation must name a page range
or locator, and a computation must carry execution references whose count
agrees with the ledger.

It **does not repair** anything. A claim that fails a check is not quietly
corrected and not quietly dropped: it becomes a `REPORT_CONFLICT` entry holding
the asserted value, the ledger's value, and the original record the two
disagree about. A conflicted report is still readable; it is not certified.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ravel.domain.enums import (
    ClaimClass,
    EvidenceExecutionType,
    ReportConflictCode,
    ReportDataMode,
    ReportStatus,
)
from ravel.reports.classification import (
    Classification,
    LedgerIndex,
    artifact_is_simulated,
    classify,
    is_simulated_backend,
)
from ravel.reports.models import (
    ClaimRequest,
    ReportClaim,
    ReportEvidenceRef,
    ReportRequest,
    ScientificReport,
)

#: The execution types that make a claim a computation claim, which must name
#: the records that ran rather than only the artifact that came out.
_COMPUTED = frozenset(
    {EvidenceExecutionType.COMPUTATION, EvidenceExecutionType.HUMAN_LAB}
)


@dataclass(frozen=True, slots=True)
class BuiltReport:
    """The report and its link rows, ready to be staged together."""

    report: ScientificReport
    claims: tuple[ReportClaim, ...]
    evidence_refs: tuple[ReportEvidenceRef, ...]


def build_report(
    index: LedgerIndex, project_id: str, request: ReportRequest
) -> BuiltReport:
    """Build one immutable report snapshot from a project's ledger index.

    The same request over the same ledger produces the same rendered facts and
    the same accepted claims every time. Report and conflict identifiers are
    fresh, because they name this snapshot rather than the science; they are
    not part of the deterministic rendering.
    """
    _check_request_shape(request)
    conflicts: list[dict[str, Any]] = []
    evidence_refs: list[ReportEvidenceRef] = []
    conflicted_evidence: dict[str, tuple[str, ...]] = {}
    resolved: dict[str, Classification] = {}
    requested_ids = {item.evidence_id for item in request.evidence}

    for item in request.evidence:
        classified = classify(index, item.evidence_id)
        if classified is None:
            conflicts.append(
                _conflict(
                    ReportConflictCode.FABRICATED_SOURCE,
                    f"evidence {item.evidence_id!r} does not exist in project "
                    f"{project_id}; no ledger row was corrected or invented",
                    evidence_id=item.evidence_id,
                    asserted={"evidence_id": item.evidence_id},
                )
            )
            conflicted_evidence[item.evidence_id] = (
                ReportConflictCode.FABRICATED_SOURCE.value,
            )
            continue

        codes: list[str] = []
        if item.content_hash is not None and item.content_hash != classified.content_hash:
            codes.append(ReportConflictCode.HASH_MISMATCH.value)
            conflicts.append(
                _conflict(
                    ReportConflictCode.HASH_MISMATCH,
                    f"evidence {item.evidence_id!r} was asserted with hash "
                    f"{item.content_hash!r}, but the ledger holds "
                    f"{classified.content_hash!r}",
                    evidence_id=item.evidence_id,
                    asserted={"content_hash": item.content_hash},
                    original_record=classified.original_record,
                    actual={"content_hash": classified.content_hash},
                )
            )
        if (
            classified.execution_type is EvidenceExecutionType.RESEARCH_SOURCE
            and not item.locator.strip()
        ):
            codes.append(ReportConflictCode.MISSING_LOCATOR.value)
            conflicts.append(
                _conflict(
                    ReportConflictCode.MISSING_LOCATOR,
                    f"evidence {item.evidence_id!r} is a research source but the "
                    "report names no page range or locator",
                    evidence_id=item.evidence_id,
                    asserted={"locator": item.locator},
                    original_record=classified.original_record,
                )
            )

        resolved[item.evidence_id] = classified
        if codes:
            conflicted_evidence[item.evidence_id] = tuple(codes)
        evidence_refs.append(
            ReportEvidenceRef(
                report_id="",  # filled with the report id below
                evidence_id=item.evidence_id,
                position=len(evidence_refs) + 1,
                execution_type=classified.execution_type,
                locator=item.locator.strip(),
                content_hash=classified.content_hash,
                original_record=classified.original_record,
            )
        )

    claims: list[ReportClaim] = []
    for claim_request in request.claims:
        claim_id = str(uuid4())
        claim_conflicts, rejected = _claim_conflicts(
            index=index,
            claim_request=claim_request,
            claim_id=claim_id,
            requested_ids=requested_ids,
            resolved=resolved,
            conflicted_evidence=conflicted_evidence,
        )
        conflicts.extend(claim_conflicts)
        if rejected:
            continue
        claims.append(
            ReportClaim(
                report_id="",  # filled with the report id below
                claim_id=claim_id,
                position=len(claims) + 1,
                claim_type=claim_request.claim_type,
                statement=claim_request.statement.strip(),
                evidence_ids=tuple(dict.fromkeys(claim_request.evidence_ids)),
                execution_refs=tuple(dict.fromkeys(claim_request.execution_refs)),
                execution_node_id=claim_request.execution_node_id,
                execution_count=claim_request.execution_count,
            )
        )

    report_id = _new_report_id()
    evidence_rows = tuple(
        _with_report_id(ref, report_id) for ref in evidence_refs
    )
    claim_rows = tuple(_with_report_id(claim, report_id) for claim in claims)
    reason = (
        f"{len(conflicts)} claim(s) or reference(s) disagreed with the ledger"
        if conflicts
        else ""
    )
    report_status = ReportStatus.REPORT_CONFLICT if conflicts else ReportStatus.CERTIFIED
    report = ScientificReport(
        report_id=report_id,
        project_id=project_id,
        title=request.title,
        data_mode=_data_mode(index, evidence_rows),
        status=report_status,
        limitations=request.limitations,
        rendered=render_facts(
            index,
            project_id=project_id,
            evidence_refs=evidence_rows,
            conflicts=conflicts,
            status=report_status,
            reason=reason,
        ),
        conflicts=tuple(conflicts),
    )
    return BuiltReport(report=report, claims=claim_rows, evidence_refs=evidence_rows)


def render_facts(
    index: LedgerIndex,
    *,
    project_id: str,
    evidence_refs: tuple[ReportEvidenceRef, ...],
    conflicts: list[dict[str, Any]],
    status: ReportStatus,
    reason: str,
) -> dict[str, Any]:
    """The deterministic part of a report: counts, state and evidence index.

    Nothing in this object is read from a caller's prose. Every value comes
    from a ledger row, and every map is keyed in sorted order so two reports
    over one ledger compare equal rather than merely looking equal.
    """
    execution_backends = Counter(
        execution.backend for execution in index.executions.values()
    )
    execution_status = Counter(
        execution.termination_status for execution in index.executions.values()
    )
    simulated_executions = sum(
        1
        for execution in index.executions.values()
        if is_simulated_backend(execution.backend)
    )
    job_states = Counter(job.state for job in index.jobs.values())
    job_backends = Counter(job.backend for job in index.jobs.values())
    simulated_jobs = sum(
        1 for job in index.jobs.values() if is_simulated_backend(job.backend)
    )
    review_outcomes = Counter(review.outcome for review in index.reviews)
    review_checkpoints = Counter(review.checkpoint for review in index.reviews)
    artifacts = []
    for artifact in sorted(
        index.artifacts.values(), key=lambda row: row.artifact_id
    ):
        version = index.latest_version(artifact.artifact_id)
        artifacts.append(
            {
                "artifact_id": artifact.artifact_id,
                "name": artifact.name,
                "kind": artifact.kind,
                "provenance": artifact.provenance,
                "created_by": artifact.created_by,
                "node_id": artifact.node_id,
                "latest_version": version.version if version is not None else None,
                "content_hash": (
                    version.content_hash if version is not None else None
                ),
                "simulated": artifact_is_simulated(index, artifact),
            }
        )
    return {
        "project_id": project_id,
        "status": status.value,
        "status_reason": reason,
        "executions": {
            "total": len(index.executions),
            "real": len(index.executions) - simulated_executions,
            "simulated": simulated_executions,
            "by_status": dict(sorted(execution_status.items())),
            "by_backend": dict(sorted(execution_backends.items())),
        },
        "backend_jobs": {
            "total": len(index.jobs),
            "real": len(index.jobs) - simulated_jobs,
            "simulated": simulated_jobs,
            "by_state": dict(sorted(job_states.items())),
            "by_backend": dict(sorted(job_backends.items())),
        },
        "artifacts": {
            "total": len(artifacts),
            "simulated": sum(1 for artifact in artifacts if artifact["simulated"]),
            "real": sum(1 for artifact in artifacts if not artifact["simulated"]),
            "index": artifacts,
        },
        "reviews": {
            "total": len(index.reviews),
            "by_outcome": dict(sorted(review_outcomes.items())),
            "by_checkpoint": dict(sorted(review_checkpoints.items())),
            "index": [
                {
                    "review_id": review.review_id,
                    "node_id": review.node_id,
                    "checkpoint": review.checkpoint,
                    "outcome": review.outcome,
                    "frozen_criteria_ref": review.frozen_criteria_ref,
                    "frozen_criteria_version": review.frozen_criteria_version,
                    "artifact_refs": list(review.artifact_refs),
                    "evidence_refs": list(review.evidence_refs),
                }
                for review in index.reviews
            ],
        },
        "deviations": {
            "total": len(index.deviations),
            "open": sum(
                1
                for deviation in index.deviations
                if deviation.resolved_by_decision_ref is None
            ),
        },
        "evidence_index": [
            {
                "evidence_id": ref.evidence_id,
                "execution_type": ref.execution_type.value,
                "locator": ref.locator,
                "content_hash": ref.content_hash,
                "original_record": ref.original_record,
            }
            for ref in sorted(evidence_refs, key=lambda row: row.position)
        ],
        "conflict_codes": sorted(
            {str(conflict["code"]) for conflict in conflicts}
        ),
        "ledger_digest": _ledger_digest(index),
    }


def _claim_conflicts(
    *,
    index: LedgerIndex,
    claim_request: ClaimRequest,
    claim_id: str,
    requested_ids: set[str],
    resolved: dict[str, Classification],
    conflicted_evidence: dict[str, tuple[str, ...]],
) -> tuple[list[dict[str, Any]], bool]:
    """The conflicts this claim raises, and whether it must be rejected.

    A claim is rejected when it raises any conflict, and also when it cites
    evidence whose own citation failed — in that second case the evidence-level
    conflict already names the failure, so no duplicate claim-level conflict is
    invented, but the claim still may not be persisted.
    """
    conflicts: list[dict[str, Any]] = []
    cited = tuple(dict.fromkeys(claim_request.evidence_ids))
    for evidence_id in cited:
        if evidence_id not in requested_ids:
            conflicts.append(
                _conflict(
                    ReportConflictCode.FABRICATED_SOURCE,
                    f"claim cites evidence {evidence_id!r}, which is not in the "
                    "report's evidence index; a claim cannot cite a row the "
                    "report did not read",
                    claim_id=claim_id,
                    evidence_id=evidence_id,
                    asserted={"evidence_ids": list(cited)},
                )
            )
            continue
        codes = conflicted_evidence.get(evidence_id, ())
        if evidence_id not in resolved:
            if not codes:
                conflicts.append(
                    _conflict(
                        ReportConflictCode.FABRICATED_SOURCE,
                        f"claim cites evidence {evidence_id!r}, which did not "
                        "resolve to a ledger row",
                        claim_id=claim_id,
                        evidence_id=evidence_id,
                    )
                )
            # The evidence-level conflict already names the row; do not repeat
            # the same source failure once per claim.
            continue
        for code in codes:
            conflicts.append(
                _conflict(
                    ReportConflictCode(code),
                    f"claim rests on evidence {evidence_id!r}, whose citation "
                    f"failed verification with {code}",
                    claim_id=claim_id,
                    evidence_id=evidence_id,
                    asserted={"evidence_ids": list(cited)},
                    original_record=resolved[evidence_id].original_record,
                )
            )

    types = {
        resolved[evidence_id].execution_type
        for evidence_id in cited
        if evidence_id in resolved
    }
    if (
        claim_request.claim_type is ClaimClass.FACT
        and EvidenceExecutionType.SIMULATED in types
    ):
        conflicts.append(
            _conflict(
                ReportConflictCode.SIMULATED_AS_FACT,
                "a FACT claim rests on simulated evidence; simulated output is "
                "never a fact, and the report does not relabel it",
                claim_id=claim_id,
                asserted={
                    "claim_type": claim_request.claim_type.value,
                    "statement": claim_request.statement,
                    "evidence_ids": list(cited),
                },
                original_record={
                    evidence_id: resolved[evidence_id].original_record
                    for evidence_id in cited
                    if evidence_id in resolved
                    and resolved[evidence_id].execution_type
                    is EvidenceExecutionType.SIMULATED
                },
            )
        )

    conflicts.extend(
        _execution_conflicts(index, claim_request, claim_id=claim_id, types=types)
    )
    cited_conflicted = any(
        evidence_id in conflicted_evidence for evidence_id in cited
    )
    return conflicts, bool(conflicts) or cited_conflicted


def _execution_conflicts(
    index: LedgerIndex,
    claim_request: ClaimRequest,
    *,
    claim_id: str,
    types: set[EvidenceExecutionType],
) -> list[dict[str, Any]]:
    """Checks a computation claim's execution references and count."""
    conflicts: list[dict[str, Any]] = []
    computed_claim = bool(types & _COMPUTED)
    execution_refs = tuple(dict.fromkeys(claim_request.execution_refs))
    needs_execution_refs = (
        computed_claim
        or bool(execution_refs)
        or claim_request.execution_count is not None
        or claim_request.execution_node_id is not None
    )
    missing_refs = (
        computed_claim
        or (
            claim_request.execution_count is not None
            and claim_request.execution_node_id is None
        )
    ) and not execution_refs
    if missing_refs:
        conflicts.append(
            _conflict(
                ReportConflictCode.MISSING_EXECUTION_REF,
                "a report claim that asserts executions must carry execution "
                "records; an artifact or a count alone does not name the run",
                claim_id=claim_id,
                asserted={
                    "statement": claim_request.statement,
                    "execution_refs": [],
                    "execution_count": claim_request.execution_count,
                    "evidence_ids": list(claim_request.evidence_ids),
                },
            )
        )
    if not needs_execution_refs:
        return conflicts

    originals: list[dict[str, Any]] = []
    known: list[str] = []
    for execution_id in execution_refs:
        execution = index.executions.get(execution_id)
        if execution is None:
            conflicts.append(
                _conflict(
                    ReportConflictCode.FABRICATED_EXECUTION,
                    f"claim cites execution {execution_id!r}, which does not "
                    "exist in this project",
                    claim_id=claim_id,
                    asserted={"execution_id": execution_id},
                )
            )
            continue
        known.append(execution_id)
        originals.append(
            {
                "execution_id": execution.execution_id,
                "node_id": execution.node_id,
                "backend": execution.backend,
                "termination_status": execution.termination_status,
            }
        )

    node_id = claim_request.execution_node_id
    if node_id is not None and known:
        wrong_node = [
            execution_id
            for execution_id in known
            if index.executions[execution_id].node_id != node_id
        ]
        if wrong_node:
            conflicts.append(
                _conflict(
                    ReportConflictCode.WRONG_EXECUTION_COUNT,
                    f"claim names node {node_id!r} but cites executions of other "
                    f"nodes: {sorted(wrong_node)}",
                    claim_id=claim_id,
                    asserted={
                        "execution_node_id": node_id,
                        "execution_refs": known,
                    },
                    original_record={"executions": originals},
                )
            )

    if claim_request.execution_count is not None:
        if node_id is not None and not known:
            actual = len(index.executions_for_node(node_id))
            ledger_ids = [
                execution.execution_id
                for execution in index.executions_for_node(node_id)
            ]
        else:
            actual = len(known)
            ledger_ids = list(known)
        if claim_request.execution_count != actual:
            conflicts.append(
                _conflict(
                    ReportConflictCode.WRONG_EXECUTION_COUNT,
                    f"claim asserts {claim_request.execution_count} execution(s), "
                    f"but the ledger records {actual} for "
                    f"{node_id or 'the cited references'}",
                    claim_id=claim_id,
                    asserted={
                        "execution_count": claim_request.execution_count,
                        "execution_refs": known,
                        "execution_node_id": node_id,
                    },
                    original_record={
                        "execution_count": actual,
                        "execution_ids": ledger_ids,
                        "executions": originals,
                    },
                    actual={"execution_count": actual},
                )
            )
    return conflicts


def _data_mode(
    index: LedgerIndex, evidence_refs: tuple[ReportEvidenceRef, ...]
) -> ReportDataMode:
    """REAL, SIMULATED, or MIXED, from provenance and never from a caller."""
    modes: set[ReportDataMode] = set()
    for execution in index.executions.values():
        modes.add(
            ReportDataMode.SIMULATED
            if is_simulated_backend(execution.backend)
            else ReportDataMode.REAL
        )
    for job in index.jobs.values():
        modes.add(
            ReportDataMode.SIMULATED
            if is_simulated_backend(job.backend)
            else ReportDataMode.REAL
        )
    for artifact in index.artifacts.values():
        modes.add(
            ReportDataMode.SIMULATED
            if artifact_is_simulated(index, artifact)
            else ReportDataMode.REAL
        )
    for ref in evidence_refs:
        modes.add(
            ReportDataMode.SIMULATED
            if ref.execution_type is EvidenceExecutionType.SIMULATED
            else ReportDataMode.REAL
        )
    if not modes or modes == {ReportDataMode.REAL}:
        return ReportDataMode.REAL
    if modes == {ReportDataMode.SIMULATED}:
        return ReportDataMode.SIMULATED
    return ReportDataMode.MIXED


def _ledger_digest(index: LedgerIndex) -> str:
    """A digest of exactly the ledger rows this report indexed.

    Two reports with the same digest were assembled from the same facts. It is
    not an authorization token and no read path trusts it; it is a cheap way
    for a reader to notice that two snapshots differ in more than their ids.
    """
    def canonical(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            rows,
            key=lambda row: json.dumps(row, sort_keys=True, default=str),
        )

    facts = {
        "evidence": canonical(
            [
                {
                    "evidence_id": row.evidence_id,
                    "claim_class": row.claim_class,
                    "source_refs": list(row.source_refs),
                    "content_hash": row.content_hash,
                }
                for row in index.evidence.values()
            ]
        ),
        "sources": canonical(
            [
                {
                    "source_id": row.source_id,
                    "url": row.url,
                    "access_status": row.access_status,
                    "content_hash": row.content_hash,
                    "artifact_ref": row.artifact_ref,
                }
                for row in index.sources.values()
            ]
        ),
        "artifacts": canonical(
            [
                {
                    "artifact_id": row.artifact_id,
                    "kind": row.kind,
                    "provenance": row.provenance,
                    "created_by": row.created_by,
                    "versions": [
                        {
                            "version": version.version,
                            "content_hash": version.content_hash,
                        }
                        for version in index.versions.get(row.artifact_id, ())
                    ],
                }
                for row in index.artifacts.values()
            ]
        ),
        "executions": canonical(
            [
                {
                    "execution_id": row.execution_id,
                    "node_id": row.node_id,
                    "backend": row.backend,
                    "backend_job_ref": row.backend_job_ref,
                    "termination_status": row.termination_status,
                }
                for row in index.executions.values()
            ]
        ),
        "jobs": canonical(
            [
                {
                    "job_id": row.job_id,
                    "backend": row.backend,
                    "state": row.state,
                    "attempt": row.attempt,
                }
                for row in index.jobs.values()
            ]
        ),
        "reviews": canonical(
            [
                {
                    "review_id": row.review_id,
                    "node_id": row.node_id,
                    "checkpoint": row.checkpoint,
                    "outcome": row.outcome,
                    "frozen_criteria_ref": row.frozen_criteria_ref,
                    "frozen_criteria_version": row.frozen_criteria_version,
                }
                for row in index.reviews
            ]
        ),
        "deviations": canonical(
            [
                {
                    "deviation_id": row.deviation_id,
                    "node_id": row.node_id,
                    "resolved_by_decision_ref": row.resolved_by_decision_ref,
                }
                for row in index.deviations
            ]
        ),
    }
    encoded = json.dumps(
        facts, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _check_request_shape(request: ReportRequest) -> None:
    """Refuse an ambiguous request before any row is staged.

    Duplicate identifiers make "the same ledger, the same report" false, so
    they are a caller error rather than a report conflict. A missing statement
    or limitation cannot be rendered at all.
    """
    if not request.title.strip():
        raise ValueError("a report needs a title")
    if not request.claims:
        raise ValueError("a report needs at least one claim; an empty report certifies nothing")
    seen_evidence: set[str] = set()
    for item in request.evidence:
        if item.evidence_id in seen_evidence:
            raise ValueError(f"evidence {item.evidence_id!r} is listed more than once")
        seen_evidence.add(item.evidence_id)
    for claim in request.claims:
        if not claim.statement.strip():
            raise ValueError("every claim needs a non-empty statement")
        if len(set(claim.evidence_ids)) != len(claim.evidence_ids):
            raise ValueError(
                f"claim {claim.statement!r} cites the same evidence more than once"
            )
        if len(set(claim.execution_refs)) != len(claim.execution_refs):
            raise ValueError(
                f"claim {claim.statement!r} cites the same execution more than once"
            )
        if claim.execution_count is not None and claim.execution_count < 0:
            raise ValueError(
                f"claim {claim.statement!r} asserts a negative execution count"
            )
    for limitation in request.limitations:
        if not limitation.strip():
            raise ValueError("a limitation cannot be an empty string")


def _new_report_id() -> str:
    return str(uuid4())


def _with_report_id(row: Any, report_id: str) -> Any:
    """Return a link record with the report id filled in.

    The id is allocated after validation, because a conflicted report needs a
    report row just as a certified one does. Constructing the links with a
    placeholder and replacing it keeps the report identity a single allocation
    rather than two generators that could disagree.
    """
    return row.model_copy(update={"report_id": report_id})


def _conflict(
    code: ReportConflictCode,
    message: str,
    *,
    claim_id: str | None = None,
    evidence_id: str | None = None,
    asserted: dict[str, Any] | None = None,
    original_record: dict[str, Any] | None = None,
    actual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One conflict, naming what was asserted and what the ledger holds."""
    return {
        "conflict_id": str(uuid4()),
        "code": code.value,
        "message": message,
        "claim_id": claim_id,
        "evidence_id": evidence_id,
        "asserted": asserted or {},
        "actual": actual or {},
        "original_record": original_record,
    }


__all__ = ["BuiltReport", "build_report", "render_facts"]
