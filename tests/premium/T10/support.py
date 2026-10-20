"""Shared, explicitly labelled fixtures for the T10 suite.

Everything the parser reads here is either the official output the RASPA
authors committed (`REFERENCE_FIXTURE`) or text this module generated to
exercise a branch (`TEST_FIXTURE`). No helper in this file can produce a
`REAL_RUN` claim: `parse_raspa_output` requires the source kind, and the
helpers here pass a fixture kind by name.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO

from tests.integration.conftest import READ_AT

from ravel.compute.raspa.contract import RaspaScientificContract
from ravel.compute.raspa.package import build_input_package
from ravel.compute.raspa.parser import RaspaResult, parse_raspa_output
from ravel.compute.raspa.provenance import SourceKind, read_bundled, source_record
from ravel.domain.artifacts import Artifact, ArtifactVersion
from ravel.domain.contracts import AcceptanceCriterion, CriterionProvenance, ExecutionContract
from ravel.domain.enums import (
    AccessStatus,
    CompletenessVerdict,
    EvidenceSourceTier,
    TerminationStatus,
)
from ravel.domain.evidence import EvidenceSource
from ravel.domain.execution import CompletenessCheck, ExecutionRecord
from ravel.preparation.workspace import AcceptanceTerms, PreparationContext, PreparedInput
from ravel.state.database import Database
from ravel.state.repositories.records import ExecutionRepository
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceSourceRepository,
)
from ravel.state.store import ArtifactStoreError, StoredObject, hash_chunks

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = ROOT / "schemas" / "premium" / "compute"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
REFERENCE_OUTPUT = FIXTURE_DIR / "official_output_100000.data"

PROJECT_ID = "project-t10"
NODE_ID = "node-t10"
CONTRACT_ID = "contract-t10"
ACCEPTANCE_ID = "acceptance-t10"

TERMS: dict[str, str] = {
    "method": "GCMC",
    "temperature": "300",
    "temperature_unit": "K",
    "pressure": "100000",
    "pressure_unit": "Pa",
    "cycles": "25000",
    "initialization_cycles": "5000",
    "print_every": "1000",
    "unit_cells": "2 2 2",
    "translation_probability": "0.5",
    "reinsertion_probability": "0.5",
    "swap_probability": "1.0",
    "seed": "1621335350",
    "framework": "MFI_SI.cif",
    "molecule": "methane",
    "molecule_definition": "ExampleDefinitions",
    "force_field": "ExampleZeolitesForceField",
    "helium_void_fraction": "0.29",
    "remove_atom_number_code_from_label": "yes",
}

INPUT_NAMES: tuple[str, ...] = (
    "MFI_SI.cif",
    "force_field_mixing_rules.def",
    "pseudo_atoms.def",
    "methane.def",
)


class MemoryStore:
    """An in-memory `ArtifactStore`.

    The one doubled boundary in this suite: T10 is about whether the Ledger's
    metadata lines up, and no property being tested needs MinIO's network.
    Artifact rows, evidence rows, executions and their guards are real
    PostgreSQL rows.
    """

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, chunks: Iterable[bytes], *, media_type: str = "") -> StoredObject:
        if key in self.objects:
            raise ArtifactStoreError(f"{key} already exists; artifacts are immutable")
        data = b"".join(chunks)
        digest, size = hash_chunks([data])
        self.objects[key] = (data, digest)
        return StoredObject(key=key, content_hash=digest, size_bytes=size)

    def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ArtifactStoreError(f"{key} is absent")
        return self.objects[key][0]

    def open(self, key: str) -> BinaryIO:
        return BytesIO(self.get(key))

    def exists(self, key: str) -> bool:
        return key in self.objects

    def verify(self, key: str, expected_hash: str) -> bool:
        return key in self.objects and self.objects[key][1] == expected_hash

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def official_inputs() -> tuple[PreparedInput, ...]:
    """The four pinned official files, as a caller would resolve them."""
    files = (
        ("MFI_SI.cif", "framework-mfi-si"),
        ("force_field_mixing_rules.def", "force-field-example-zeolites"),
        ("pseudo_atoms.def", "pseudo-atoms-example-zeolites"),
        ("methane.def", "molecule-methane"),
    )
    return tuple(
        PreparedInput(name=name, data=read_bundled(key), source=source_record(key).url)
        for name, key in files
    )


def an_execution_contract(
    terms: dict[str, str] | None = None,
    *,
    contract_id: str = CONTRACT_ID,
    version: int = 1,
    required_outputs: tuple[str, ...] | None = None,
    inputs: tuple[str, ...] = INPUT_NAMES,
    requirements: dict[str, str] | None = None,
) -> ExecutionContract:
    """A frozen-shaped execution contract carrying the canonical terms."""
    targets = dict(TERMS if terms is None else terms)
    contract = ExecutionContract(
        contract_id=contract_id,
        project_id=PROJECT_ID,
        node_id=NODE_ID,
        version=version,
        objective="Compute the absolute methane loading of MFI at the fixed state point.",
        procedure="GCMC in RASPA 2.0.x from the pinned official reference data.",
        inputs=inputs,
        parameter_targets=targets,
        required_outputs=required_outputs or (),
        execution_requirements=(
            {"software": "raspa"} if requirements is None else requirements
        ),
    )
    return contract


def a_scientific_contract(
    terms: dict[str, str] | None = None, *, version: int = 1, contract_id: str = CONTRACT_ID
) -> RaspaScientificContract:
    return RaspaScientificContract.from_execution_contract(
        an_execution_contract(terms, version=version, contract_id=contract_id)
    )


def expected_output(terms: dict[str, str] | None = None) -> str:
    return a_scientific_contract(terms).output_filename()


def default_criteria() -> tuple[AcceptanceCriterion, ...]:
    return (
        AcceptanceCriterion(
            criterion_id="crit-loading-units",
            statement=(
                "The result reports absolute loading in mol/kg framework and "
                "molecules per unit cell at the contract's state point."
            ),
            metric="loading",
            threshold="both units reported",
            provenance=CriterionProvenance.USER_REQUIREMENT,
        ),
        AcceptanceCriterion(
            criterion_id="crit-convergence",
            statement=(
                "The absolute-loading average meets the convergence policy fixed "
                "in the package before the run."
            ),
            metric="relative_standard_error",
            threshold="<= 0.05",
            provenance=CriterionProvenance.PROVISIONAL,
            notes="RAVEL's provisional policy, not a literature threshold.",
        ),
    )


def an_acceptance_terms(
    *, version: int = 1, contract_id: str = ACCEPTANCE_ID
) -> AcceptanceTerms:
    return AcceptanceTerms(
        contract_id=contract_id, version=version, criteria=default_criteria()
    )


def a_context(
    root: Path,
    *,
    terms: dict[str, str] | None = None,
    contract: ExecutionContract | None = None,
    inputs: tuple[PreparedInput, ...] | None = None,
    acceptance: AcceptanceTerms | None = None,
) -> PreparationContext:
    """A package-building context: frozen terms, resolved official inputs, criteria."""
    built = contract or an_execution_contract(terms)
    scientific = RaspaScientificContract.from_execution_contract(built)
    if not built.required_outputs:
        built = built.model_copy(update={"required_outputs": (scientific.output_filename(),)})
    return PreparationContext(
        project_id=built.project_id,
        node_id=built.node_id,
        node_display_id="N10",
        contract=built,
        workspace_root=Path(root),
        inputs=official_inputs() if inputs is None else inputs,
        acceptance=an_acceptance_terms() if acceptance is None else acceptance,
    )


def build_package(root: Path, **options: Any):
    """Build a package from defaults, or from the options a test names."""
    return build_input_package(a_context(root, **options))


def reference_output_bytes() -> bytes:
    return REFERENCE_OUTPUT.read_bytes()


def reference_result(
    package_sha256: str,
    *,
    contract: RaspaScientificContract | None = None,
    provenance: SourceKind = SourceKind.REFERENCE_FIXTURE,
    exit_code: int = 0,
    output: bytes | None = None,
) -> RaspaResult:
    """Parse the official output as a clearly labelled reference fixture."""
    return parse_raspa_output(
        reference_output_bytes() if output is None else output,
        exit_code=exit_code,
        contract=contract or a_scientific_contract(),
        package_sha256=package_sha256,
        provenance=provenance,
    )


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def synthetic_output(
    *,
    blocks: tuple[float, ...] = (2.0, 3.0, 3.0, 4.0, 3.0),
    mean: float = 3.0,
    standard_error: float = 0.002,
    version: str = "RASPA 2.0.45",
    seed: int = 1621335350,
    temperature: float = 300.0,
    pressure: float = 100000.0,
    finished: bool = True,
    warnings: int = 0,
    conversion_mol_kg: float = 0.1733675050,
    conversion_mg_g: float = 2.7812412648,
    printed_mol_kg: float | None = None,
    printed_mg_g: float | None = None,
) -> bytes:
    """A minimal output shaped like RASPA 2.0, explicitly a TEST_FIXTURE.

    This is not a scientific result and is never compared to one. It exists so
    the parser's failure and convergence branches can be exercised without
    pretending an official file failed.

    `printed_mol_kg` and `printed_mg_g` default to the values the printed
    conversion factors imply; a caller that overrides one is writing an
    internally inconsistent file, which is the unit-failure branch.
    """
    mol_kg = mean * conversion_mol_kg if printed_mol_kg is None else printed_mol_kg
    mg_g = mean * conversion_mg_g if printed_mg_g is None else printed_mg_g
    lines = [
        "Compiler and run-time data",
        "===========================================================================",
        version,
        "",
        "Simulation",
        "===========================================================================",
        f"Random number seed: {seed}",
        "Number of cycles: 25000",
        "Number of initializing cycles: 5000",
        f"External temperature: {temperature:g} [K]",
        f"Partial pressure:    {pressure:.14f} [Pa]",
        "",
        "\tConversion factor molecules/unit cell -> mol/kg:       "
        f"{conversion_mol_kg:.10f} [-]",
        "\tConversion factor molecules/unit cell -> mg/g:       "
        f"{conversion_mg_g:.10f} [-]",
        "",
        "Number of molecules:",
        "====================",
        "",
        "Component 0 [methane]",
        "-------------------------------------------------------------",
    ]
    for index, value in enumerate(blocks):
        lines.append(f"\tBlock[{index:2d}] {value:.8f}           [-]")
    lines += [
        "\t------------------------------------------------------------------------------",
        "\tAverage loading absolute                             "
        f"{mean * 23:.10f} +/-       0.0000000000 [-]",
        "\tAverage loading absolute [molecules/unit cell]        "
        f"{mean:.10f} +/-       {standard_error:.10f} [-]",
        "\tAverage loading absolute [mol/kg framework]                  "
        f"{mol_kg:.10f} +/-       {standard_error * conversion_mol_kg:.10f} [-]",
        "\tAverage loading absolute [milligram/gram framework]          "
        f"{mg_g:.10f} +/-       {standard_error * conversion_mg_g:.10f} [-]",
        "",
    ]
    if finished:
        lines.append(f"Simulation finished,  {warnings} warnings")
    return ("\n".join(lines) + "\n").encode("utf-8")


# ── A small JSON Schema reader ─────────────────────────────────────────────
#
# `jsonschema` is not a project dependency, and pulling it in for four schemas
# would be a dependency added for the tests alone. This validator implements
# exactly the keywords the T10 schemas use: type, const, enum, pattern,
# required, properties, additionalProperties, items, minItems/maxItems,
# minimum/maximum/exclusiveMinimum, and local `$defs` references. A missing
# keyword is a test-writing mistake, not something this silently ignores:
# `validate` reports an unsupported keyword as an error.


def schema_document(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def schema_sha256(name: str) -> str:
    return hashlib.sha256((SCHEMA_DIR / name).read_bytes()).hexdigest()


_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, int | float) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}

_SUPPORTED = {
    "$schema",
    "$id",
    "$defs",
    "$ref",
    "title",
    "description",
    "type",
    "const",
    "enum",
    "pattern",
    "minLength",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
}


def validate(document: Any, schema: dict[str, Any]) -> list[str]:
    """Every way `document` violates `schema`, as readable sentences."""

    errors: list[str] = []

    def resolve(spec: dict[str, Any]) -> dict[str, Any]:
        seen = 0
        while "$ref" in spec:
            reference = str(spec["$ref"])
            if not reference.startswith("#/$defs/"):
                errors.append(f"unsupported $ref {reference!r}")
                return {}
            spec = schema["$defs"][reference.rsplit("/", 1)[-1]]
            seen += 1
            if seen > 20:
                errors.append("$ref chain is cyclic")
                return {}
        return spec

    def check(value: Any, spec: dict[str, Any], path: str) -> None:
        unsupported = set(spec) - _SUPPORTED
        if unsupported:
            errors.append(f"{path}: validator does not implement {sorted(unsupported)}")
            return
        spec = resolve(spec)
        if not spec:
            return
        if "const" in spec and value != spec["const"]:
            errors.append(f"{path}: {value!r} is not {spec['const']!r}")
        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"{path}: {value!r} is not one of {spec['enum']}")
        if "type" in spec:
            declared = spec["type"]
            kinds = declared if isinstance(declared, list) else [declared]
            if not any(_TYPE_CHECKS[kind](value) for kind in kinds):
                errors.append(f"{path}: {value!r} is not of type {kinds}")
                return
        if (
            isinstance(value, str)
            and "minLength" in spec
            and len(value) < int(spec["minLength"])
        ):
            errors.append(f"{path}: {value!r} is shorter than {spec['minLength']}")
        if (
            isinstance(value, str)
            and "pattern" in spec
            and re.fullmatch(str(spec["pattern"]), value) is None
        ):
            errors.append(f"{path}: {value!r} does not match {spec['pattern']!r}")
        if isinstance(value, int | float) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                errors.append(f"{path}: {value} is below {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                errors.append(f"{path}: {value} is above {spec['maximum']}")
            if "exclusiveMinimum" in spec and value <= spec["exclusiveMinimum"]:
                errors.append(f"{path}: {value} is not above {spec['exclusiveMinimum']}")
        if isinstance(value, dict):
            for name in spec.get("required", []):
                if name not in value:
                    errors.append(f"{path}: missing required property {name!r}")
            properties = spec.get("properties", {})
            if spec.get("additionalProperties") is False:
                for name in value:
                    if name not in properties:
                        errors.append(f"{path}: unexpected property {name!r}")
            for name, child in value.items():
                if name in properties:
                    check(child, properties[name], f"{path}.{name}")
        if isinstance(value, list):
            if "minItems" in spec and len(value) < spec["minItems"]:
                errors.append(f"{path}: {len(value)} items, minimum {spec['minItems']}")
            if "maxItems" in spec and len(value) > spec["maxItems"]:
                errors.append(f"{path}: {len(value)} items, maximum {spec['maxItems']}")
            item_spec = spec.get("items")
            if isinstance(item_spec, dict):
                for index, item in enumerate(value):
                    check(item, item_spec, f"{path}[{index}]")

    check(document, schema, "$")
    return errors


def assert_valid(document: Any, schema_name: str) -> None:
    errors = validate(document, schema_document(schema_name))
    assert not errors, "; ".join(errors)


# ── Real PostgreSQL rows for the AC3 trace ─────────────────────────────────


def record_input_artifacts(
    database: Database,
    *,
    project_id: str,
    node_id: str,
    manifest: dict[str, Any],
    package_root: Path,
) -> dict[str, tuple[Artifact, ArtifactVersion]]:
    store = MemoryStore()
    recorded: dict[str, tuple[Artifact, ArtifactVersion]] = {}
    with database.transaction() as session:
        repository = ArtifactRepository(session, project_id, store)
        for entry in manifest["inputs"]:
            name = str(entry["name"])
            artifact, version = repository.register(
                name=name,
                chunks=[(package_root / name).read_bytes()],
                created_by="compute-worker",
                kind="input",
                provenance=str(entry["source"]),
                node_id=node_id,
                media_type="text/plain",
            )
            recorded[name] = (artifact, version)
    return recorded


def record_sources(database: Database, *, project_id: str, manifest: dict[str, Any]) -> None:
    with database.transaction() as session:
        repository = EvidenceSourceRepository(session, project_id)
        for entry in manifest["sources"]:
            repository.record(
                EvidenceSource(
                    project_id=project_id,
                    url=str(entry["url"]),
                    title=str(entry["title"]),
                    access_status=AccessStatus.OK,
                    retrieved_at=READ_AT,
                    content_hash=f"sha256:{entry['sha256']}",
                    media_type="text/plain",
                    tier=EvidenceSourceTier.A,
                )
            )


def record_result_artifact(
    database: Database,
    *,
    project_id: str,
    node_id: str,
    result: RaspaResult,
    kind: str = "simulated",
    name: str = "raspa-result.json",
) -> tuple[Artifact, ArtifactVersion]:
    store = MemoryStore()
    with database.transaction() as session:
        repository = ArtifactRepository(session, project_id, store)
        return repository.register(
            name=name,
            chunks=[result.canonical_json().encode("utf-8")],
            created_by="compute-worker",
            kind=kind,
            provenance=(
                "fixture:official-reference-output"
                if kind == "simulated"
                else "raspa:gcmc"
            ),
            node_id=node_id,
            media_type="application/json",
        )


def record_execution(
    database: Database,
    *,
    project_id: str,
    node_id: str,
    contract_ref: str,
    contract_version: int,
    output_refs: tuple[str, ...],
) -> ExecutionRecord:
    execution = ExecutionRecord(
        project_id=project_id,
        node_id=node_id,
        execution_contract_ref=contract_ref,
        execution_contract_version=contract_version,
        backend="slurm",
        output_refs=output_refs,
        completeness=CompletenessCheck(verdict=CompletenessVerdict.COMPLETE),
        termination_status=TerminationStatus.COMPLETED,
        executed_by="slurm",
        started_at=READ_AT,
    )
    with database.transaction() as session:
        return ExecutionRepository(session, project_id).record(execution, actor_id="slurm")


@dataclass(frozen=True, slots=True)
class TraceFixture:
    """Everything AC3 needs: a real node, a real package, and real ledger rows."""

    prepared: Any
    package: Any
    result: RaspaResult
    artifact: Artifact
    version: ArtifactVersion


def a_traced_fixture(
    database: Database,
    prepared: Any,
    package: Any,
    *,
    result: RaspaResult | None = None,
    result_kind: str = "simulated",
    record_execution_record: bool = True,
    record_all_inputs: bool = True,
    record_all_sources: bool = True,
    output_refs: tuple[str, ...] | None = None,
) -> TraceFixture:
    """Build the Ledger rows a trace reads, with named omissions for refusals."""
    scientific = RaspaScientificContract.from_execution_contract(prepared.contract)
    parsed = result or reference_result(package.package_sha256, contract=scientific)
    inputs = {
        str(entry["name"]): entry for entry in package.manifest["inputs"]
    }
    if not record_all_inputs:
        first = next(iter(inputs))
        inputs.pop(first)
    partial_manifest = dict(package.manifest)
    partial_manifest["inputs"] = list(inputs.values())
    record_input_artifacts(
        database,
        project_id=prepared.project_id,
        node_id=prepared.node_id,
        manifest=partial_manifest,
        package_root=package.root,
    )
    sources_manifest = dict(package.manifest)
    if not record_all_sources:
        sources_manifest["sources"] = package.manifest["sources"][1:]
    record_sources(database, project_id=prepared.project_id, manifest=sources_manifest)
    artifact, version = record_result_artifact(
        database,
        project_id=prepared.project_id,
        node_id=prepared.node_id,
        result=parsed,
        kind=result_kind,
    )
    if record_execution_record:
        record_execution(
            database,
            project_id=prepared.project_id,
            node_id=prepared.node_id,
            contract_ref=prepared.contract.contract_id,
            contract_version=prepared.contract.version,
            output_refs=output_refs if output_refs is not None else (artifact.artifact_id,),
        )
    return TraceFixture(
        prepared=prepared,
        package=package,
        result=parsed,
        artifact=artifact,
        version=version,
    )
