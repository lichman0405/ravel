"""Following a RASPA result back through PostgreSQL, item by item.

A result document is a claim. The Ledger is where RAVEL keeps the records that
let someone check it, and this module is the reader that walks from the claim
to the rows:

- the **execution contract** the package was built from, still frozen, and
  still hashing to the scientific terms the package records;
- the **execution** that ran under that exact contract version and names the
  result artifact among its outputs;
- the **frozen acceptance contract** bound to the node, whose criteria hash is
  the one in the package, and which was frozen no later than the result
  existed — the check that catches criteria written to fit a result;
- every **input artifact**, by name, version, content hash, and size;
- every **source**, by URL and content hash, as a readable `EvidenceSource`;
- the **result artifact**, whose bytes hash to the canonical result document
  and whose `kind` says whether those bytes are a real computation or a
  simulation.

**Fixtures cannot pass as results.** A result whose source kind is a fixture
must be stored on a `simulated` artifact, and a result claiming `REAL_RUN` must
not be. A fixture on a `computation`-kind artifact — or a real run on a
simulated one — is refused with `FIXTURE_PRESENTED_AS_REAL`, which is the
Ledger-level expression of the rule that mock output never becomes scientific
evidence.

**No writes.** Tracing reads rows and returns a document. It does not add
evidence, amend a review, or touch a contract; those are Review's and Master's
records, and a trace that could write them would be a way to make its own
claims true.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from ravel.compute.raspa.contract import RaspaScientificContract
from ravel.compute.raspa.package import PACKAGE_SCHEMA, canonical_json
from ravel.compute.raspa.parser import EvidenceClass, RaspaResult
from ravel.compute.raspa.provenance import official_source_document
from ravel.domain.artifacts import Artifact, ArtifactVersion, is_simulated
from ravel.domain.contracts import AcceptanceContract, ExecutionContract
from ravel.preparation.workspace import MaterializationRefused
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.records import ExecutionRepository
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceSourceRepository,
)

__all__ = [
    "RaspaLedgerTrace",
    "RaspaTraceRefused",
    "TraceItem",
    "TraceRefusal",
    "trace_result",
]

TRACE_SCHEMA = "ravel.raspa.ledger_trace/1"


class TraceRefusal(StrEnum):
    """Why a result could not be traced, from the closed list."""

    PACKAGE_INVALID = "PACKAGE_INVALID"
    CONTRACT_NOT_TRACED = "CONTRACT_NOT_TRACED"
    CONTRACT_NOT_FROZEN = "CONTRACT_NOT_FROZEN"
    CONTRACT_DOCUMENT_MISMATCH = "CONTRACT_DOCUMENT_MISMATCH"
    EXECUTION_NOT_RECORDED = "EXECUTION_NOT_RECORDED"
    CRITERIA_NOT_FROZEN = "CRITERIA_NOT_FROZEN"
    CRITERIA_NOT_TRACED = "CRITERIA_NOT_TRACED"
    CRITERIA_RETROFITTED = "CRITERIA_RETROFITTED"
    INPUT_UNTRACED = "INPUT_UNTRACED"
    SOURCE_UNTRACED = "SOURCE_UNTRACED"
    RESULT_UNTRACED = "RESULT_UNTRACED"
    RESULT_HASH_MISMATCH = "RESULT_HASH_MISMATCH"
    PACKAGE_HASH_MISMATCH = "PACKAGE_HASH_MISMATCH"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    FIXTURE_PRESENTED_AS_REAL = "FIXTURE_PRESENTED_AS_REAL"


class RaspaTraceRefused(Exception):
    """A result that the Ledger does not support, and which item failed."""

    def __init__(self, code: TraceRefusal, reason: str) -> None:
        if not isinstance(code, TraceRefusal):
            raise TypeError(
                f"a trace refusal needs one of {[member.value for member in TraceRefusal]}, "
                f"got {code!r}"
            )
        if not reason.strip():
            raise ValueError("a trace refusal must say which item the Ledger did not support")
        super().__init__(reason)
        self.code = code
        self.reason = reason

    def document(self) -> dict[str, str]:
        return {"code": self.code.value, "reason": self.reason, "return_to": "MASTER"}


@dataclass(frozen=True, slots=True)
class TraceItem:
    """One item of the trace, and the row that supports it."""

    kind: str
    ref: str
    sha256: str
    detail: str

    def to_document(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "sha256": self.sha256,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RaspaLedgerTrace:
    """A fully traced result: every claim has a row."""

    project_id: str
    node_id: str
    package_sha256: str
    result_sha256: str
    result_status: str
    items: tuple[TraceItem, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": TRACE_SCHEMA,
            "project_id": self.project_id,
            "node_id": self.node_id,
            "package_sha256": self.package_sha256,
            "result_sha256": self.result_sha256,
            "result_status": self.result_status,
            "items": [item.to_document() for item in self.items],
        }


def _refuse(code: TraceRefusal, reason: str) -> RaspaTraceRefused:
    return RaspaTraceRefused(code, reason)


def _manifest_without_digest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "package_sha256"}


def _verify_manifest(manifest: dict[str, Any]) -> str:
    if manifest.get("schema") != PACKAGE_SCHEMA:
        raise _refuse(
            TraceRefusal.PACKAGE_INVALID,
            f"the manifest declares schema {manifest.get('schema')!r}, not {PACKAGE_SCHEMA!r}",
        )
    recorded = manifest.get("package_sha256")
    recomputed = canonical_json(_manifest_without_digest(manifest))
    digest = hashlib.sha256(recomputed.encode("utf-8")).hexdigest()
    if recorded != digest:
        raise _refuse(
            TraceRefusal.PACKAGE_HASH_MISMATCH,
            f"the manifest records package_sha256 {recorded}, and its document "
            f"hashes to {digest}; the package the result names was edited",
        )
    return digest


def _execution_contract(
    session: Session, project_id: str, node_id: str, manifest: dict[str, Any]
) -> tuple[ExecutionContract, RaspaScientificContract]:
    reference = manifest["execution_contract"]["contract_ref"]
    version = int(manifest["execution_contract"]["contract_version"])
    repository = ExecutionContractRepository(session, project_id)
    try:
        contract = repository.get(contract_id=reference)
    except NotFound as error:
        raise _refuse(
            TraceRefusal.CONTRACT_NOT_TRACED,
            f"the package names execution contract {reference!r}, which is not "
            f"in project {project_id}: {error}",
        ) from error
    if contract.version != version:
        raise _refuse(
            TraceRefusal.CONTRACT_NOT_TRACED,
            f"the package names contract {reference!r} version {version}, and the "
            f"row is version {contract.version}",
        )
    if contract.node_id != node_id:
        raise _refuse(
            TraceRefusal.CONTRACT_NOT_TRACED,
            f"contract {reference!r} belongs to node {contract.node_id!r}, not "
            f"the result's node {node_id!r}",
        )
    if not contract.is_frozen:
        raise _refuse(
            TraceRefusal.CONTRACT_NOT_FROZEN,
            f"contract {reference!r} version {version} is not frozen; a package "
            "built from terms that could still change has no fixed identity",
        )
    try:
        scientific = RaspaScientificContract.from_execution_contract(contract)
    except MaterializationRefused as error:
        raise _refuse(
            TraceRefusal.CONTRACT_DOCUMENT_MISMATCH,
            f"the frozen contract no longer materializes: {error.reason}",
        ) from error
    if canonical_json(scientific.to_document()) != canonical_json(
        manifest["scientific_contract"]
    ):
        raise _refuse(
            TraceRefusal.CONTRACT_DOCUMENT_MISMATCH,
            "the package's scientific terms are not those of the frozen contract "
            f"{reference!r} version {version}; the package was built from other terms",
        )
    return contract, scientific


def _execution_record(
    session: Session,
    project_id: str,
    node_id: str,
    manifest: dict[str, Any],
    artifact_id: str,
) -> str:
    reference = manifest["execution_contract"]["contract_ref"]
    version = int(manifest["execution_contract"]["contract_version"])
    executions = ExecutionRepository(session, project_id).for_node(node_id)
    matching = [
        execution
        for execution in executions
        if execution.execution_contract_ref == reference
        and execution.execution_contract_version == version
    ]
    if not matching:
        raise _refuse(
            TraceRefusal.EXECUTION_NOT_RECORDED,
            f"node {node_id!r} has no execution under contract {reference!r} "
            f"version {version}; without one the rows behind the result are incomplete",
        )
    execution = matching[-1]
    if artifact_id not in execution.output_refs:
        raise _refuse(
            TraceRefusal.RESULT_UNTRACED,
            f"execution {execution.display_id} does not name artifact "
            f"{artifact_id!r} among its outputs {list(execution.output_refs)}",
        )
    return execution.execution_id


def _acceptance(
    session: Session,
    project_id: str,
    node_id: str,
    manifest: dict[str, Any],
    result_created_at: datetime,
) -> tuple[AcceptanceContract, str]:
    reference = manifest["acceptance"]["contract_ref"]
    version = int(manifest["acceptance"]["contract_version"])
    repository = AcceptanceContractRepository(session, project_id)
    try:
        acceptance = repository.get(contract_id=reference)
    except NotFound as error:
        raise _refuse(
            TraceRefusal.CRITERIA_NOT_TRACED,
            f"the package names acceptance contract {reference!r}, which is not "
            f"in project {project_id}: {error}",
        ) from error
    if acceptance.node_id != node_id or acceptance.version != version:
        raise _refuse(
            TraceRefusal.CRITERIA_NOT_TRACED,
            f"acceptance contract {reference!r} version {version} is not the one "
            f"for node {node_id!r} version {acceptance.version}",
        )
    if not acceptance.is_frozen or acceptance.frozen_at is None:
        raise _refuse(
            TraceRefusal.CRITERIA_NOT_FROZEN,
            f"acceptance contract {reference!r} was never frozen; criteria that "
            "can still change are not a standard a result was measured against",
        )
    bound = repository.frozen_for_node(node_id)
    if bound is None or bound.contract_id != acceptance.contract_id:
        raise _refuse(
            TraceRefusal.CRITERIA_NOT_TRACED,
            f"the frozen acceptance contract bound to node {node_id!r} is "
            f"{None if bound is None else bound.contract_id!r}, not {reference!r}",
        )
    criteria = [
        {
            "criterion_id": criterion.criterion_id,
            "statement": criterion.statement,
            "metric": criterion.metric,
            "threshold": criterion.threshold,
            "provenance": criterion.provenance.value,
            "provenance_ref": criterion.provenance_ref,
            "notes": criterion.notes,
        }
        for criterion in acceptance.criteria
    ]
    digest = hashlib.sha256(canonical_json(criteria).encode("utf-8")).hexdigest()
    if digest != manifest["acceptance"]["criteria_sha256"]:
        raise _refuse(
            TraceRefusal.CRITERIA_RETROFITTED,
            f"the frozen acceptance criteria hash to {digest}, and the package "
            f"records {manifest['acceptance']['criteria_sha256']}; the standard "
            "in the package is not the one the node was frozen against",
        )
    if result_created_at is not None and acceptance.frozen_at > result_created_at:
        raise _refuse(
            TraceRefusal.CRITERIA_RETROFITTED,
            f"acceptance contract {reference!r} was frozen at "
            f"{acceptance.frozen_at.isoformat()}, after the result artifact was "
            f"created at {result_created_at.isoformat()}; criteria written after "
            "the result are criteria fitted to it",
        )
    return acceptance, digest


def _input_artifacts(
    session: Session, project_id: str, manifest: dict[str, Any]
) -> list[TraceItem]:
    repository = ArtifactRepository(session, project_id)
    items: list[TraceItem] = []
    for entry in manifest["inputs"]:
        name = str(entry["name"])
        expected = f"sha256:{entry['sha256']}"
        matched: tuple[Artifact, ArtifactVersion] | None = None
        for artifact in repository.all(name=name):
            for version in repository.versions(artifact.artifact_id):
                if version.content_hash == expected and version.size_bytes == entry["size_bytes"]:
                    matched = (artifact, version)
                    break
            if matched is not None:
                break
        if matched is None:
            raise _refuse(
                TraceRefusal.INPUT_UNTRACED,
                f"no artifact version named {name!r} hashes to {expected}; the "
                "input the run used is not in the Ledger",
            )
        artifact, version = matched
        if is_simulated(artifact.kind):
            raise _refuse(
                TraceRefusal.INPUT_UNTRACED,
                f"input {name!r} resolves to simulated artifact "
                f"{artifact.artifact_id}; a computation cannot rest on mock inputs",
            )
        items.append(
            TraceItem(
                kind="INPUT",
                ref=f"{artifact.artifact_id}@{version.version}",
                sha256=entry["sha256"],
                detail=(
                    f"{name}: {entry['source']} ({entry['license']}); "
                    f"{entry['size_bytes']} bytes"
                ),
            )
        )
    return items


def _source_rows(
    session: Session, project_id: str, manifest: dict[str, Any]
) -> list[TraceItem]:
    repository = EvidenceSourceRepository(session, project_id)
    items: list[TraceItem] = []
    for entry in manifest["sources"]:
        key = str(entry["key"])
        try:
            official = official_source_document(key)
        except KeyError as error:
            raise _refuse(
                TraceRefusal.PACKAGE_INVALID,
                f"the manifest cites source {key!r}, which is not a pinned official record",
            ) from error
        if entry["url"] != official["url"] or entry["sha256"] != official["sha256"]:
            raise _refuse(
                TraceRefusal.PACKAGE_INVALID,
                f"the manifest cites source {key!r} as {entry['url']} @ "
                f"{entry['sha256']}, not the pinned {official['url']} @ "
                f"{official['sha256']}",
            )
        expected = f"sha256:{entry['sha256']}"
        matched = [
            source
            for source in repository.all(url=official["url"])
            if source.content_hash == expected and source.was_read
        ]
        if not matched:
            raise _refuse(
                TraceRefusal.SOURCE_UNTRACED,
                f"no readable EvidenceSource at {official['url']} hashes to "
                f"{expected}; the source the package cites is not in the Ledger",
            )
        items.append(
            TraceItem(
                kind="SOURCE",
                ref=matched[-1].source_id,
                sha256=str(entry["sha256"]),
                detail=f"{key}: {official['url']} ({official['license']})",
            )
        )
    return items


def _result_artifact(
    session: Session,
    project_id: str,
    node_id: str,
    result: RaspaResult,
    artifact_id: str,
    artifact_version: int,
) -> tuple[Artifact, ArtifactVersion]:
    repository = ArtifactRepository(session, project_id)
    try:
        artifact = repository.get(artifact_id=artifact_id)
    except NotFound as error:
        raise _refuse(
            TraceRefusal.RESULT_UNTRACED,
            f"the result names artifact {artifact_id!r}, which is not in project "
            f"{project_id}: {error}",
        ) from error
    if artifact.node_id != node_id:
        raise _refuse(
            TraceRefusal.RESULT_UNTRACED,
            f"artifact {artifact_id!r} belongs to node {artifact.node_id!r}, not "
            f"the result's node {node_id!r}",
        )
    versions = [v for v in repository.versions(artifact_id) if v.version == artifact_version]
    if not versions:
        raise _refuse(
            TraceRefusal.RESULT_UNTRACED,
            f"artifact {artifact_id!r} has no version {artifact_version}",
        )
    version = versions[0]
    expected = f"sha256:{result.sha256()}"
    if version.content_hash != expected:
        raise _refuse(
            TraceRefusal.RESULT_HASH_MISMATCH,
            f"the stored result artifact hashes to {version.content_hash}, and the "
            f"canonical result document hashes to {expected}; the bytes the Ledger "
            "holds are not the result being claimed",
        )
    simulated_artifact = is_simulated(artifact.kind)
    if result.evidence_class is EvidenceClass.COMPUTATION and simulated_artifact:
        raise _refuse(
            TraceRefusal.FIXTURE_PRESENTED_AS_REAL,
            f"the result claims COMPUTATION and its artifact kind is "
            f"{artifact.kind!r}; simulated bytes cannot be a real result",
        )
    if result.evidence_class is EvidenceClass.SIMULATED and not simulated_artifact:
        raise _refuse(
            TraceRefusal.FIXTURE_PRESENTED_AS_REAL,
            f"the result is a {result.source_kind.value} fixture and its artifact "
            f"kind is {artifact.kind!r}, not 'simulated'; a fixture registered as "
            "a real result is exactly the confusion the labeling prevents",
        )
    return artifact, version


def _result_units(manifest: dict[str, Any], result: RaspaResult) -> TraceItem:
    expected_units = {
        "temperature": manifest["scientific_contract"]["units"]["temperature"],
        "pressure": manifest["scientific_contract"]["units"]["pressure"],
        "loading_primary": manifest["scientific_contract"]["units"]["loading_primary"],
        "loading_secondary": manifest["scientific_contract"]["units"]["loading_secondary"],
        "seed": manifest["scientific_contract"]["units"]["seed"],
    }
    actual = result.to_document()["units"]
    if canonical_json(actual) != canonical_json(expected_units):
        raise _refuse(
            TraceRefusal.UNIT_MISMATCH,
            f"the result states units {actual}, and the package fixed {expected_units}",
        )
    return TraceItem(
        kind="UNITS",
        ref="result.units",
        sha256="",
        detail="K, Pa, mol/kg framework, molecules/unit cell, dimensionless seed",
    )


def trace_result(
    session: Session,
    *,
    project_id: str,
    node_id: str,
    manifest: dict[str, Any],
    result: RaspaResult,
    artifact_id: str,
    artifact_version: int,
) -> RaspaLedgerTrace:
    """Trace one result to the Ledger, or refuse naming the item that failed.

    The result artifact must contain exactly `result.canonical_json()` encoded
    as UTF-8: that is what makes the document the trace holds and the bytes the
    Ledger holds the same thing rather than two accounts of it.

    Raises:
        RaspaTraceRefused: One of the items has no supporting row, or the rows
            disagree with the package or the result.
    """
    package_sha256 = _verify_manifest(manifest)
    if result.package_sha256 != package_sha256:
        raise _refuse(
            TraceRefusal.PACKAGE_HASH_MISMATCH,
            f"the result names package {result.package_sha256}, and the manifest "
            f"hashes to {package_sha256}",
        )
    contract, scientific = _execution_contract(session, project_id, node_id, manifest)
    if result.scientific_contract_sha256 != scientific.sha256():
        raise _refuse(
            TraceRefusal.CONTRACT_DOCUMENT_MISMATCH,
            f"the result names scientific contract digest "
            f"{result.scientific_contract_sha256}, and the frozen contract hashes "
            f"to {scientific.sha256()}",
        )
    if (
        result.execution_contract_ref != contract.contract_id
        or result.execution_contract_version != contract.version
    ):
        raise _refuse(
            TraceRefusal.CONTRACT_NOT_TRACED,
            "the result names execution contract "
            f"{result.execution_contract_ref!r}@v{result.execution_contract_version}, "
            f"not the package's {contract.contract_id!r}@v{contract.version}",
        )

    execution_id = _execution_record(session, project_id, node_id, manifest, artifact_id)
    artifact, version = _result_artifact(
        session, project_id, node_id, result, artifact_id, artifact_version
    )
    acceptance, criteria_sha256 = _acceptance(
        session, project_id, node_id, manifest, version.created_at
    )
    items = [
        TraceItem(
            kind="EXECUTION_CONTRACT",
            ref=f"{contract.contract_id}@v{contract.version}",
            sha256=scientific.sha256(),
            detail="frozen execution contract; scientific terms match the package",
        ),
        TraceItem(
            kind="EXECUTION",
            ref=execution_id,
            sha256="",
            detail=f"node {node_id}; output artifact {artifact_id}",
        ),
        TraceItem(
            kind="ACCEPTANCE_CONTRACT",
            ref=f"{acceptance.contract_id}@v{acceptance.version}",
            sha256=criteria_sha256,
            detail=f"frozen at {acceptance.frozen_at.isoformat() if acceptance.frozen_at else ''}",
        ),
        *_input_artifacts(session, project_id, manifest),
        *_source_rows(session, project_id, manifest),
        _result_units(manifest, result),
        TraceItem(
            kind="RESULT",
            ref=f"{artifact.artifact_id}@{version.version}",
            sha256=result.sha256(),
            detail=(
                f"status {result.status.value}; evidence {result.evidence_class.value}; "
                f"artifact kind {artifact.kind!r}"
            ),
        ),
    ]
    return RaspaLedgerTrace(
        project_id=project_id,
        node_id=node_id,
        package_sha256=package_sha256,
        result_sha256=result.sha256(),
        result_status=result.status.value,
        items=tuple(items),
    )
