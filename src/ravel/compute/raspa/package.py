"""The immutable package a RASPA GCMC run is started from.

A prepared workspace is a directory of files; a *package* is the claim that
those files, together with the terms they encode, are one fixed thing. T10's
second acceptance condition asks for both halves of that claim:

- **the same contract produces the same package hash.** The manifest therefore
  contains no timestamp, no host path, no object-store key, and no order that
  depends on a dictionary's insertion history. Everything in it is a value the
  frozen contract, the pinned official data, or this module's version decided.
- **a version change produces a new package.** The execution contract's
  identifier and version are part of the hashed document, as is the canonical
  scientific contract. A revision that changes one temperature decimal changes
  the hash; there is no "compatible enough" comparison.

The standards a result will be judged by are fixed *here*, before any run:
`required_outputs` names the exact output path RASPA will write, `parser` pins
the result schema by content digest, `convergence` states the block count and
relative standard error the result must meet, and `acceptance` carries the
frozen Review criteria and their digest. A reviewer reading the package after a
run can tell whether the criteria were already there or were written to fit
what came back.

**Nothing is invented.** The package is built from four files whose SHA-256
values are pinned in `provenance.py`, and a fifth file generated from the
contract. If a caller supplies bytes that are not one of the pinned official
records, the build refuses: a force field that differs by one number is a
different calculation under the same name.

**The real run is T25.** This module builds and verifies input packages. It
never starts a RASPA binary, and `real_run` below says so in every manifest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ravel.compute.raspa.contract import RaspaScientificContract
from ravel.compute.raspa.provenance import official_source_document, source_record
from ravel.compute.raspa.route import (
    SUPPORT_MATRIX,
    SUPPORTED_METHOD,
    SUPPORTED_VERSION_SERIES,
)
from ravel.domain.preparation import PreparationCheck, PreparationRefusal
from ravel.preparation.workspace import (
    FileRole,
    MaterializationRefused,
    PreparationContext,
    PreparedExecution,
    PreparedFile,
    Workspace,
)

__all__ = [
    "CONVERGENCE_POLICY",
    "MATERIALIZER",
    "MATERIALIZER_VERSION",
    "PACKAGE_SCHEMA",
    "PARSER_SCHEMA_ID",
    "PARSER_SCHEMA_SHA256",
    "PARSER_SCHEMA_VERSION",
    "RaspaInputPackage",
    "build_input_package",
    "canonical_json",
    "criteria_document",
    "verify_input_package",
]

#: The materializer name the Preparation Record carries. Distinct from the
#: legacy `RaspaMaterializer`, whose files and terms this package does not
#: reproduce: a reader must be able to tell which code wrote a workspace.
MATERIALIZER = "raspa-gcmc"

#: Bumped when the files, the manifest shape, or the hashing rule changes.
MATERIALIZER_VERSION = "1"

PACKAGE_SCHEMA = "ravel.raspa.input_package/1"

PARSER_SCHEMA_ID = "https://ravel.local/schemas/premium/compute/raspa_result.schema.json"
PARSER_SCHEMA_VERSION = "1"

#: SHA-256 of `schemas/premium/compute/raspa_result.schema.json`. Pinned rather
#: than read from the repository at runtime: the compiler of an installed
#: RAVEL does not necessarily carry the schema directory, and a package whose
#: parser pin changed silently with a checkout would not be immutable. A test
#: compares this constant against the file, so the two cannot drift unnoticed.
PARSER_SCHEMA_SHA256 = "fc83e7976667cf2b21c8769552daece41354d80c034dd19a1484c746ff43d826"

#: The convergence policy fixed before the run.
#:
#: RASPA divides production cycles into five blocks and reports the standard
#: error of the block means. Five blocks is the software's own output shape;
#: a relative standard error above 5% means the state point's average is not a
#: number a scientific claim should rest on. Both values are RAVEL's, not the
#: literature's, and are marked `PROVISIONAL` so a reviewer can see that.
CONVERGENCE_POLICY: dict[str, object] = {
    "policy_version": "raspa-block-average-1",
    "min_blocks": 5,
    "max_relative_standard_error": 0.05,
    "numeric_relative_tolerance": 1e-6,
    "provenance": "PROVISIONAL",
    "provenance_ref": "T10: RAVEL-chosen threshold, not literature-derived",
    "fixed_before_run": True,
}

#: Standard stop conditions, used only when the contract states none. These
#: describe how a run ends, not what a scientific value is.
_DEFAULT_STOP_CONDITIONS: tuple[str, ...] = (
    "the RASPA process exits with a non-zero status",
    "the output file lacks the 'Simulation finished' line",
    "the reported state point does not match the frozen contract",
    "the printed loading block statistics fail the convergence policy",
)

#: The four official files this route copies, by the name each takes in the
#: workspace, mapped to the pinned source that proves its bytes.
_INPUT_FILES: tuple[tuple[str, str], ...] = (
    ("force_field_mixing_rules.def", "force-field-example-zeolites"),
    ("pseudo_atoms.def", "pseudo-atoms-example-zeolites"),
)

_MOLECULE_FILE_TEMPLATE = "{molecule}.def"

#: The official example system, and the only one this first route packages.
#: A different framework or gas needs its own provenance record, its own
#: declared license, and its own pinned digests; accepting the name here and
#: substituting bytes would make the manifest's `sources` a false statement.
_REFERENCE_FRAMEWORK = "MFI_SI.cif"
_REFERENCE_MOLECULE = "methane"
_REFERENCE_MOLECULE_LIBRARY = "ExampleDefinitions"
_REFERENCE_FORCE_FIELD = "ExampleZeolitesForceField"


def canonical_json(document: Any) -> str:
    """A stable serialisation for every digest this module computes."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _refused(refusal: PreparationRefusal, reason: str) -> MaterializationRefused:
    return MaterializationRefused(refusal, reason)


@dataclass(frozen=True, slots=True)
class RaspaInputPackage:
    """A built package: a directory, its manifest, and its identity."""

    root: Path
    manifest: dict[str, Any]
    package_sha256: str
    files: tuple[PreparedFile, ...]
    checks: tuple[PreparationCheck, ...]

    @property
    def output_filename(self) -> str:
        """The exact path RASPA is expected to write, relative to `root`."""
        return str(self.manifest["output_filename"])

    @property
    def required_outputs(self) -> tuple[str, ...]:
        return tuple(str(entry) for entry in self.manifest["required_outputs"])

    def execution(self) -> PreparedExecution:
        """The package as the runtime's `PreparedExecution`."""
        return PreparedExecution(
            workspace_path=str(self.root),
            materializer=MATERIALIZER,
            materializer_version=MATERIALIZER_VERSION,
            files=self.files,
            manifest=self.manifest,
            checks=self.checks,
            required_outputs=self.required_outputs,
            execution_metadata={
                "software": "raspa",
                "software_version_series": SUPPORTED_VERSION_SERIES,
                "input": "simulation.input",
                "output": self.output_filename,
                "package_sha256": self.package_sha256,
            },
        )


def criteria_document(context: PreparationContext) -> tuple[list[dict[str, object]], str, str]:
    """The frozen Review criteria, their digest, and the acceptance contract ref.

    Raises:
        MaterializationRefused: The context carries no frozen acceptance terms.
            A package whose result is to be reviewed has to say what it will be
            reviewed against *before* the run; accepting the package without
            them would leave the criteria to be supplied later, which is the
            retrofitting T10-AC3 forbids.
    """
    acceptance = context.acceptance
    if acceptance is None or not acceptance.criteria:
        raise _refused(
            PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
            "the node has no frozen acceptance criteria, and a RASPA package is "
            "written with the Review criteria and convergence policy it will be "
            "judged against already fixed; criteria supplied after a result are "
            "criteria fitted to it",
        )
    documents: list[dict[str, object]] = []
    for criterion in acceptance.criteria:
        documents.append(
            {
                "criterion_id": criterion.criterion_id,
                "statement": criterion.statement,
                "metric": criterion.metric,
                "threshold": criterion.threshold,
                "provenance": criterion.provenance.value,
                "provenance_ref": criterion.provenance_ref,
                "notes": criterion.notes,
            }
        )
    digest = _sha256(canonical_json(documents).encode("utf-8"))
    return documents, digest, acceptance.contract_id


def _pinned_system(contract: RaspaScientificContract) -> None:
    """Refuse a system this route has no pinned, licensed reference data for.

    Raises:
        MaterializationRefused: The contract names a framework, molecule,
            molecule library, or force field other than the bundled official
            example.
    """
    mismatches: list[str] = []
    if contract.framework != _REFERENCE_FRAMEWORK:
        mismatches.append(f"framework {contract.framework!r}")
    if contract.molecule != _REFERENCE_MOLECULE:
        mismatches.append(f"molecule {contract.molecule!r}")
    if contract.molecule_definition != _REFERENCE_MOLECULE_LIBRARY:
        mismatches.append(f"molecule_definition {contract.molecule_definition!r}")
    if contract.force_field != _REFERENCE_FORCE_FIELD:
        mismatches.append(f"force_field {contract.force_field!r}")
    if mismatches:
        expected = (
            f"framework {_REFERENCE_FRAMEWORK!r}, molecule {_REFERENCE_MOLECULE!r}, "
            f"molecule_definition {_REFERENCE_MOLECULE_LIBRARY!r}, force_field "
            f"{_REFERENCE_FORCE_FIELD!r}"
        )
        raise _refused(
            PreparationRefusal.UNSUPPORTED_ENVIRONMENT,
            "this first scientific route packages exactly one redistributable "
            f"reference system, and the contract names {', '.join(mismatches)}; "
            f"the pinned system is {expected}. Another structure or gas needs its "
            "own provenance record, license, and digests rather than a substitution "
            "under the same package name.",
        )


def _supplied_inputs(
    context: PreparationContext, contract: RaspaScientificContract
) -> list[tuple[str, bytes, str]]:
    """The four pinned input files, as the caller resolved them.

    Returns:
        `(workspace name, bytes, source key)` in a stable order.

    Raises:
        MaterializationRefused: A required name is absent from the contract's
            inputs, is not resolvable in the context, or resolves to bytes that
            are not the pinned official record.
    """
    framework = contract.framework
    molecule_file = _MOLECULE_FILE_TEMPLATE.format(molecule=contract.molecule)
    wanted: list[tuple[str, str]] = [
        (framework, "framework-mfi-si"),
        *_INPUT_FILES,
        (molecule_file, "molecule-methane"),
    ]
    missing_from_contract = [
        name for name, _ in wanted if name not in context.contract.inputs
    ]
    if missing_from_contract:
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            "the contract does not name "
            + ", ".join(missing_from_contract)
            + " in its inputs; a package is built from files the contract says "
            "it is built from, and an input the contract never named is one no "
            "reviewer approved",
        )
    resolved: list[tuple[str, bytes, str]] = []
    for name, source_key in wanted:
        supplied = context.input_named(name)
        if supplied is None:
            available = [entry.name for entry in context.inputs]
            raise _refused(
                PreparationRefusal.INCONSISTENT_CONTRACT,
                f"the contract names {name!r} as an input and this project holds "
                f"no file by that name; the inputs that were resolved are "
                f"{available or 'none'}",
            )
        record = source_record(source_key)
        digest = _sha256(supplied.data)
        if digest != record.sha256:
            raise _refused(
                PreparationRefusal.INCONSISTENT_CONTRACT,
                f"the bytes supplied as {name!r} hash to {digest}, not the pinned "
                f"official {source_key} digest {record.sha256}; this route copies "
                "only the official MIT-licensed reference data its manifest cites",
            )
        resolved.append((name, supplied.data, source_key))
    return resolved


def _input_entry(name: str, data: bytes, source_key: str) -> dict[str, object]:
    record = source_record(source_key)
    return {
        "name": name,
        "role": FileRole.INPUT.value,
        "sha256": _sha256(data),
        "size_bytes": len(data),
        "source_key": record.key,
        "source": record.url,
        "license": record.license,
    }


def _package_document(
    context: PreparationContext,
    *,
    contract: RaspaScientificContract,
    inputs: list[tuple[str, bytes, str]],
    input_entries: list[dict[str, object]],
    simulation_input: bytes,
    acceptance_documents: list[dict[str, object]],
    acceptance_sha256: str,
    acceptance_ref: str,
    acceptance_version: int,
) -> dict[str, Any]:
    """The manifest, before its own digest is added.

    The order of every list is fixed by this function rather than by a set or a
    directory walk, so two builds of the same contract serialise byte for byte
    identically.
    """
    generated = [*(_generated_file(name, data) for name, data, _ in inputs)]
    generated.append(_generated_file("simulation.input", simulation_input))
    sources = [
        official_source_document(key)
        for key in (
            "raspa2-license",
            "official-simulation-input",
            "framework-mfi-si",
            "force-field-example-zeolites",
            "pseudo-atoms-example-zeolites",
            "molecule-methane",
        )
    ]
    stop_conditions = list(context.contract.stop_conditions) or list(_DEFAULT_STOP_CONDITIONS)
    output_filename = contract.output_filename()
    return {
        "schema": PACKAGE_SCHEMA,
        "route": SUPPORT_MATRIX.to_document(),
        "project_id": context.project_id,
        "node_id": context.node_id,
        "node_display_id": context.node_display_id,
        "objective": context.contract.objective,
        "execution_contract": {
            "contract_ref": context.contract.contract_id,
            "contract_version": context.contract.version,
        },
        "materializer": MATERIALIZER,
        "materializer_version": MATERIALIZER_VERSION,
        "method": SUPPORTED_METHOD,
        "scientific_contract": contract.to_document(),
        "software": {
            "name": "RASPA",
            "version_series": SUPPORTED_VERSION_SERIES,
            "environment": "probe_required",
        },
        "required_outputs": [output_filename],
        "output_filename": output_filename,
        "stop_conditions": stop_conditions,
        "inputs": input_entries,
        "sources": sources,
        "acceptance": {
            "contract_ref": acceptance_ref,
            "contract_version": acceptance_version,
            "criteria": acceptance_documents,
            "criteria_sha256": acceptance_sha256,
        },
        "parser": {
            "schema_id": PARSER_SCHEMA_ID,
            "schema_version": PARSER_SCHEMA_VERSION,
            "schema_sha256": PARSER_SCHEMA_SHA256,
            "result_statuses": ["NORMAL", "NON_CONVERGED", "SOFTWARE_FAILURE"],
            "evidence_classes": ["COMPUTATION", "SIMULATED"],
        },
        "convergence": dict(CONVERGENCE_POLICY),
        "evidence_scope": "IMPLEMENTATION_ONLY",
        "real_run": {
            "task": SUPPORT_MATRIX.real_run_task,
            "status": SUPPORT_MATRIX.real_run_status,
            "blocker": SUPPORT_MATRIX.real_run_blocker,
            "unlock": SUPPORT_MATRIX.real_run_unlock,
        },
        "generated_files": generated,
    }


def _generated_file(name: str, data: bytes) -> dict[str, object]:
    return {
        "name": name,
        "role": FileRole.INPUT.value,
        "sha256": _sha256(data),
        "size_bytes": len(data),
    }


def _require_empty(root: Path) -> None:
    """Refuse to write a package over a directory that already holds anything.

    Raises:
        MaterializationRefused: The workspace root exists and is not empty, or
            cannot be created.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _refused(
            PreparationRefusal.ENVIRONMENT_UNAVAILABLE,
            f"the package workspace {root} cannot be created: {error}",
        ) from error
    if any(root.iterdir()):
        raise _refused(
            PreparationRefusal.ENVIRONMENT_UNAVAILABLE,
            f"the package workspace {root} is not empty; an input package is "
            "immutable, so a second preparation writes a new workspace rather "
            "than replacing files under the first one",
        )


def build_input_package(context: PreparationContext) -> RaspaInputPackage:
    """Write one immutable RASPA GCMC input package.

    Raises:
        MaterializationRefused: The contract is missing a term, names a system
            this route does not package, has no frozen acceptance criteria, or
            the resolved inputs are not the pinned official bytes.
    """
    contract = RaspaScientificContract.from_execution_contract(context.contract)
    _pinned_system(contract)
    acceptance_documents, acceptance_sha256, acceptance_ref = criteria_document(context)
    acceptance_version = context.acceptance.version if context.acceptance else 0
    inputs = _supplied_inputs(context, contract)
    simulation_input = contract.simulation_input().encode("utf-8")
    output_filename = contract.output_filename()

    if output_filename not in context.contract.required_outputs:
        declared = list(context.contract.required_outputs)
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract requires outputs {declared or 'none'}, and this route "
            f"produces exactly {output_filename!r} for the stated system; a run "
            "cannot deliver a name the software does not write",
        )
    unexpected = [name for name in context.contract.required_outputs if name != output_filename]
    if unexpected:
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract requires {', '.join(unexpected)}, which this RASPA "
            f"route does not produce; its one required output is {output_filename!r}",
        )

    _require_empty(context.workspace_root)
    workspace = Workspace(context.workspace_root)
    for name, data, _ in inputs:
        workspace.write_bytes(name, data, role=FileRole.INPUT)
    workspace.write_bytes("simulation.input", simulation_input, role=FileRole.INPUT)

    input_entries = [_input_entry(name, data, key) for name, data, key in inputs]
    manifest = _package_document(
        context,
        contract=contract,
        inputs=inputs,
        input_entries=input_entries,
        simulation_input=simulation_input,
        acceptance_documents=acceptance_documents,
        acceptance_sha256=acceptance_sha256,
        acceptance_ref=acceptance_ref,
        acceptance_version=acceptance_version,
    )
    package_sha256 = _sha256(canonical_json(manifest).encode("utf-8"))
    manifest["package_sha256"] = package_sha256
    workspace.write_json("calculation_manifest.json", manifest, role=FileRole.MANIFEST)

    checks = (
        PreparationCheck(
            name="scientific_terms_canonical",
            passed=True,
            detail=(
                f"method {contract.method}; {contract.temperature_k:g} K; "
                f"{contract.pressure_pa:g} Pa; seed {contract.seed}"
            ),
        ),
        PreparationCheck(
            name="official_inputs_pinned",
            passed=True,
            detail=", ".join(f"{name} {_sha256(data)}" for name, data, _ in inputs),
        ),
        PreparationCheck(
            name="package_digest_computed",
            passed=True,
            detail=f"package_sha256 {package_sha256}",
        ),
        PreparationCheck(
            name="canonical_fixtures_only",
            passed=True,
            detail=(
                "this task copies official reference data and parses the official "
                "reference output as a fixture; it does not execute RASPA"
            ),
        ),
    )
    return RaspaInputPackage(
        root=Path(context.workspace_root),
        manifest=manifest,
        package_sha256=package_sha256,
        files=workspace.files,
        checks=checks,
    )


def verify_input_package(root: Path, *, strict: bool = False) -> RaspaInputPackage:
    """Re-read a written package and prove it is still the package it claims.

    Raises:
        MaterializationRefused: The manifest is absent or not a package
            manifest, its recorded digest does not match the document, or a
            listed file is missing or has different bytes.
    """
    base = Path(root)
    manifest_path = base / "calculation_manifest.json"
    if not manifest_path.is_file():
        raise _refused(
            PreparationRefusal.ENVIRONMENT_UNAVAILABLE,
            f"no calculation_manifest.json is present under {base}; a directory "
            "without a manifest is not an input package",
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != PACKAGE_SCHEMA:
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the manifest under {base} declares schema {manifest.get('schema')!r}, "
            f"not {PACKAGE_SCHEMA!r}",
        )
    recorded = manifest.pop("package_sha256", None)
    recomputed = _sha256(canonical_json(manifest).encode("utf-8"))
    if recorded != recomputed:
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the manifest under {base} records package_sha256 {recorded}, and its "
            f"document hashes to {recomputed}; the manifest was edited after it "
            "was built",
        )
    manifest["package_sha256"] = recorded

    files: list[PreparedFile] = []
    for entry in manifest["generated_files"]:
        name = str(entry["name"])
        path = base / name
        if not path.is_file():
            raise _refused(
                PreparationRefusal.ENVIRONMENT_UNAVAILABLE,
                f"the package under {base} is missing {name}",
            )
        data = path.read_bytes()
        digest = _sha256(data)
        if digest != entry["sha256"] or len(data) != entry["size_bytes"]:
            raise _refused(
                PreparationRefusal.INCONSISTENT_CONTRACT,
                f"the file {name} under {base} hashes to {digest} and is "
                f"{len(data)} bytes, not the recorded {entry['sha256']} / "
                f"{entry['size_bytes']}; the package is not the one its manifest "
                "describes",
            )
        files.append(
            PreparedFile(
                name=name,
                role=FileRole(str(entry["role"])),
                sha256=digest,
                size_bytes=len(data),
            )
        )
    files.append(
        PreparedFile(
            name="calculation_manifest.json",
            role=FileRole.MANIFEST,
            sha256=_sha256(manifest_path.read_bytes()),
            size_bytes=manifest_path.stat().st_size,
        )
    )
    if strict:
        listed = {entry.name for entry in files}
        extras = sorted(path.name for path in base.iterdir() if path.name not in listed)
        if extras:
            raise _refused(
                PreparationRefusal.INCONSISTENT_CONTRACT,
                f"the package under {base} contains files its manifest does not "
                f"describe: {', '.join(extras)}",
            )
    return RaspaInputPackage(
        root=base,
        manifest=manifest,
        package_sha256=str(recorded),
        files=tuple(files),
        checks=(
            PreparationCheck(
                name="package_digest_verified",
                passed=True,
                detail=f"package_sha256 {recorded}",
            ),
        ),
    )
