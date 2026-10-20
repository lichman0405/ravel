"""Reading a RASPA output file into one of three honest outcomes.

A parser that returns a number whenever it can find one is how a failed run
becomes a result. RASPA prints a long report; a crashed run can still contain
the words `Average loading` from a partial block, and a run that never
converged still prints a mean. So this module separates three outcomes before
it returns any value:

- `SOFTWARE_FAILURE` — the process failed, its version is not the pinned
  series, the completion line is absent, it reported warnings, the state point
  in the output is not the one the contract fixed, or the printed unit
  conversions do not agree with the printed loading. No scientific result
  exists, even if some numbers were parseable.
- `NON_CONVERGED` — the run completed and the numbers are internally
  consistent, but there are too few block means or the relative standard error
  exceeds the threshold fixed in the input package before the run.
- `NORMAL` — the run completed and met the fixed policy.

**Fixtures are labelled.** The parser takes a `SourceKind` with no default.
`REAL_RUN` is the only kind that yields evidence class `COMPUTATION`; the
official output the RASPA authors committed and any synthetic failure fixture
yield `SIMULATED`. A test cannot accidentally register a fixture as a real
result by forgetting an argument: there is no default to forget.

**Units are values, not assumptions.** The parser reads RASPA's own conversion
factors and checks the reported `mol/kg` and `mg/g` against them. A result
document states `K`, `Pa`, `mol/kg framework`, and `molecules/unit cell`
explicitly, because a number whose unit lives only in a column heading is a
number a later reader will guess at.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from ravel.compute.raspa.contract import RaspaScientificContract
from ravel.compute.raspa.package import CONVERGENCE_POLICY
from ravel.compute.raspa.provenance import SourceKind
from ravel.compute.raspa.route import ROUTE_ID, SUPPORTED_METHOD, SUPPORTED_VERSION_SERIES

__all__ = [
    "Estimate",
    "EvidenceClass",
    "RaspaResult",
    "ResultCheck",
    "ResultStatus",
    "parse_raspa_output",
]

RESULT_SCHEMA = "ravel.raspa.result/1"

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"

_VERSION = re.compile(r"^RASPA\s+(\d+)\.(\d+)(?:\.(\d+))?", re.MULTILINE)
_SEED = re.compile(r"^Random number seed:\s*(\d+)", re.MULTILINE)
_TEMPERATURE = re.compile(r"^\s*External temperature:\s*(" + _NUMBER + r")\s*\[K\]", re.MULTILINE)
_PRESSURE = re.compile(r"^\s*Partial pressure:\s*(" + _NUMBER + r")\s*\[Pa\]", re.MULTILINE)
_CYCLES = re.compile(r"^Number of cycles:\s*(\d+)", re.MULTILINE)
_INITIALIZATION = re.compile(
    r"^Number of initializing cycles:\s*(\d+)", re.MULTILINE
)
_FINISHED = re.compile(r"^Simulation finished,\s*(\d+) warnings?\s*$", re.MULTILINE)
_BLOCK = re.compile(r"^\s*Block\[\s*(\d+)\]\s+(" + _NUMBER + r")", re.MULTILINE)
_LOADING = re.compile(
    r"^\s*Average loading absolute \[molecules/unit cell\]\s+("
    + _NUMBER
    + r")\s*\+/-\s*("
    + _NUMBER
    + r")",
    re.MULTILINE,
)
_MOL_PER_KG = re.compile(
    r"^\s*Average loading absolute \[mol/kg framework\]\s+("
    + _NUMBER
    + r")\s*\+/-\s*("
    + _NUMBER
    + r")",
    re.MULTILINE,
)
_MG_PER_G = re.compile(
    r"^\s*Average loading absolute \[milligram/gram framework\]\s+("
    + _NUMBER
    + r")\s*\+/-\s*("
    + _NUMBER
    + r")",
    re.MULTILINE,
)
_CONVERSION_MOL_KG = re.compile(
    r"Conversion factor molecules/unit cell -> mol/kg:\s+(" + _NUMBER + r")"
)
_CONVERSION_MG_G = re.compile(
    r"Conversion factor molecules/unit cell -> mg/g:\s+(" + _NUMBER + r")"
)


class ResultStatus(StrEnum):
    """The three outcomes a parsed output can have."""

    NORMAL = "NORMAL"
    NON_CONVERGED = "NON_CONVERGED"
    SOFTWARE_FAILURE = "SOFTWARE_FAILURE"


class EvidenceClass(StrEnum):
    """What the parsed bytes are evidence *of*.

    `COMPUTATION` is a real execution. `SIMULATED` is a fixture: the official
    reference output verifies the parser, and a synthetic file exercises a
    failure branch. Neither is a result RAVEL produced.
    """

    COMPUTATION = "COMPUTATION"
    SIMULATED = "SIMULATED"


@dataclass(frozen=True, slots=True)
class Estimate:
    """One reported average, with the standard error RASPA printed."""

    mean: float
    standard_error: float
    relative_standard_error: float | None

    @classmethod
    def from_values(cls, mean: float, standard_error: float) -> Estimate:
        relative = None if mean == 0 else abs(standard_error / mean)
        return cls(mean=mean, standard_error=standard_error, relative_standard_error=relative)

    def to_document(self) -> dict[str, float | None]:
        return {
            "mean": self.mean,
            "standard_error": self.standard_error,
            "relative_standard_error": self.relative_standard_error,
        }


@dataclass(frozen=True, slots=True)
class ResultCheck:
    """One parser or policy check, and what it found."""

    name: str
    passed: bool
    detail: str

    def to_document(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class RaspaResult:
    """One parsed output file, and the standard it was measured against."""

    package_sha256: str
    scientific_contract_sha256: str
    execution_contract_ref: str
    execution_contract_version: int
    status: ResultStatus
    reason: str
    source_kind: SourceKind
    evidence_class: EvidenceClass
    software_version: str | None
    software_series: str | None
    temperature_k: float | None
    pressure_pa: float | None
    seed: int | None
    cycles: int | None
    initialization_cycles: int | None
    blocks: tuple[float, ...]
    molecules_per_unit_cell: Estimate | None
    mol_per_kg: Estimate | None
    mg_per_g: Estimate | None
    conversion_mol_per_kg: float | None
    conversion_mg_per_g: float | None
    warnings: int | None
    output_sha256: str
    output_size_bytes: int
    checks: tuple[ResultCheck, ...] = field(default_factory=tuple)

    @property
    def relative_standard_error(self) -> float | None:
        if self.molecules_per_unit_cell is None:
            return None
        return self.molecules_per_unit_cell.relative_standard_error

    def to_document(self) -> dict[str, Any]:
        """The result as the parser schema describes it."""
        return {
            "schema": RESULT_SCHEMA,
            "route_id": ROUTE_ID,
            "method": SUPPORTED_METHOD,
            "software": {
                "name": "RASPA",
                "version": self.software_version,
                "version_series": self.software_series,
            },
            "package_sha256": self.package_sha256,
            "scientific_contract_sha256": self.scientific_contract_sha256,
            "execution_contract": {
                "contract_ref": self.execution_contract_ref,
                "contract_version": self.execution_contract_version,
            },
            "status": self.status.value,
            "reason": self.reason,
            "source": {
                "kind": self.source_kind.value,
                "evidence_class": self.evidence_class.value,
                "evidence_reason": _evidence_reason(self.source_kind),
            },
            "state_point": {
                "temperature_k": self.temperature_k,
                "pressure_pa": self.pressure_pa,
                "seed": self.seed,
            },
            "sampling": {
                "cycles": self.cycles,
                "initialization_cycles": self.initialization_cycles,
            },
            "blocks": {"count": len(self.blocks), "values": list(self.blocks)},
            "loading": {
                "molecules_per_unit_cell": (
                    self.molecules_per_unit_cell.to_document()
                    if self.molecules_per_unit_cell is not None
                    else None
                ),
                "mol_per_kg": (
                    self.mol_per_kg.to_document() if self.mol_per_kg is not None else None
                ),
                "mg_per_g": self.mg_per_g.to_document() if self.mg_per_g is not None else None,
            },
            "conversions": {
                "mol_per_kg_per_molecule_per_unit_cell": self.conversion_mol_per_kg,
                "mg_per_g_per_molecule_per_unit_cell": self.conversion_mg_per_g,
            },
            "units": {
                "temperature": "K",
                "pressure": "Pa",
                "loading_primary": "mol/kg framework",
                "loading_secondary": "molecules/unit cell",
                "seed": "dimensionless",
            },
            "convergence": {
                "policy_version": CONVERGENCE_POLICY["policy_version"],
                "min_blocks": CONVERGENCE_POLICY["min_blocks"],
                "max_relative_standard_error": CONVERGENCE_POLICY[
                    "max_relative_standard_error"
                ],
                "numeric_relative_tolerance": CONVERGENCE_POLICY[
                    "numeric_relative_tolerance"
                ],
                "converged": self.status is ResultStatus.NORMAL,
                "checks": [check.to_document() for check in self.checks],
            },
            "warnings": self.warnings,
            "output": {
                "sha256": self.output_sha256,
                "size_bytes": self.output_size_bytes,
            },
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_document(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _evidence_reason(kind: SourceKind) -> str:
    if kind is SourceKind.REAL_RUN:
        return "the output was written by a RASPA process this execution started"
    if kind is SourceKind.REFERENCE_FIXTURE:
        return (
            "the output is the official reference output committed by the RASPA "
            "authors; it verifies the parser and is not a RAVEL execution"
        )
    return (
        "the output is a synthetic fixture shaped like RASPA output; it exercises "
        "a parser branch and is not a RAVEL execution"
    )


def _text(output: bytes | str) -> tuple[str, str, int, bool]:
    if isinstance(output, str):
        data = output.encode("utf-8")
        text = output
        valid = True
    else:
        data = bytes(output)
        text = data.decode("utf-8", errors="replace")
        valid = "\ufffd" not in text
    return text, hashlib.sha256(data).hexdigest(), len(data), valid


def _float(match: re.Match[str] | None, group: int = 1) -> float | None:
    if match is None:
        return None
    return float(match.group(group))


def _close(left: float | None, right: float | None, tolerance: float) -> bool:
    if left is None or right is None:
        return False
    scale = max(abs(left), abs(right), 1e-12)
    return abs(left - right) <= tolerance * scale


def _absolute_blocks(text: str) -> tuple[tuple[float, ...], bool]:
    """The block means for absolute loading, and whether they are numbered in order."""
    loading = _LOADING.search(text)
    if loading is None:
        return (), False
    section_start = text.rfind("Number of molecules:", 0, loading.start())
    if section_start < 0:
        return (), False
    section = text[section_start : loading.start()]
    pairs = [(int(match.group(1)), float(match.group(2))) for match in _BLOCK.finditer(section)]
    ordered = [index for index, _ in pairs] == list(range(len(pairs)))
    return tuple(value for _, value in pairs), ordered


def _estimate(match: re.Match[str] | None) -> Estimate | None:
    if match is None:
        return None
    return Estimate.from_values(float(match.group(1)), float(match.group(2)))


def parse_raspa_output(
    output: bytes | str,
    *,
    exit_code: int,
    contract: RaspaScientificContract,
    package_sha256: str,
    provenance: SourceKind,
) -> RaspaResult:
    """Parse one RASPA output file and classify it.

    `exit_code` and `provenance` have no defaults: the first is the process's
    outcome and the second is where the bytes came from, and a parser that
    assumed either would be able to turn a successful-looking fixture into a
    result.

    Raises:
        TypeError: `provenance` is not a `SourceKind`. A string that happens to
            spell one is not the same claim, and this is the one argument that
            decides whether a result is real.
    """
    if not isinstance(provenance, SourceKind):
        raise TypeError(
            f"provenance must be a SourceKind, got {provenance!r}; the difference "
            "between a real run and a fixture is not a string a caller can type"
        )

    text, output_sha256, output_size, valid_text = _text(output)
    policy = CONVERGENCE_POLICY
    tolerance = cast(float, policy["numeric_relative_tolerance"])
    max_relative = cast(float, policy["max_relative_standard_error"])
    min_blocks = cast(int, policy["min_blocks"])

    failures: list[str] = []
    checks: list[ResultCheck] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(ResultCheck(name=name, passed=passed, detail=detail))

    if not valid_text:
        failures.append("the output file is not UTF-8 text")
    check("output_is_text", valid_text, "decoded as UTF-8" if valid_text else "not UTF-8")

    version_match = _VERSION.search(text)
    version = version_match.group(0).split()[-1] if version_match is not None else None
    series = (
        f"{version_match.group(1)}.{version_match.group(2)}"
        if version_match is not None
        else None
    )
    version_ok = series == SUPPORTED_VERSION_SERIES
    if not version_ok:
        failures.append(
            f"the output does not name RASPA {SUPPORTED_VERSION_SERIES}.x "
            f"(found {version!r})"
        )
    check("software_version_supported", version_ok, version or "no version line")

    temperature = _float(_TEMPERATURE.search(text))
    pressure = _float(_PRESSURE.search(text))
    seed_match = _SEED.search(text)
    seed = int(seed_match.group(1)) if seed_match is not None else None
    state_ok = (
        _close(temperature, contract.temperature_k, tolerance)
        and _close(pressure, contract.pressure_pa, tolerance)
        and seed == contract.seed
    )
    if not state_ok:
        failures.append(
            "the output state point "
            f"(T={temperature!r} K, P={pressure!r} Pa, seed={seed!r}) is not the "
            f"contract's (T={contract.temperature_k!r} K, "
            f"P={contract.pressure_pa!r} Pa, seed={contract.seed!r})"
        )
    check(
        "state_point_matches_contract",
        state_ok,
        f"T {temperature!r} K, P {pressure!r} Pa, seed {seed!r}",
    )

    finished = _FINISHED.search(text)
    warnings = int(finished.group(1)) if finished is not None else None
    completed = finished is not None and warnings == 0
    if finished is None:
        failures.append("the output has no 'Simulation finished' line")
    elif warnings:
        failures.append(f"RASPA reported {warnings} warnings")
    check(
        "run_completed_without_warnings",
        completed,
        "completed with 0 warnings" if completed else f"warnings {warnings!r}",
    )

    cycles_match = _CYCLES.search(text)
    initial_match = _INITIALIZATION.search(text)
    cycles = int(cycles_match.group(1)) if cycles_match is not None else None
    initialization = int(initial_match.group(1)) if initial_match is not None else None

    blocks, blocks_ordered = _absolute_blocks(text)
    loading = _estimate(_LOADING.search(text))
    mol_per_kg = _estimate(_MOL_PER_KG.search(text))
    mg_per_g = _estimate(_MG_PER_G.search(text))
    conversion_mol = _float(_CONVERSION_MOL_KG.search(text))
    conversion_mg = _float(_CONVERSION_MG_G.search(text))

    parsed = all(
        value is not None
        for value in (loading, mol_per_kg, mg_per_g, conversion_mol, conversion_mg)
    )
    if not parsed:
        failures.append("the output does not contain the absolute-loading block statistics")
    check(
        "loading_statistics_parsed",
        parsed,
        "absolute loading, mol/kg, mg/g and both conversion factors"
        if parsed
        else "a loading statistic or conversion factor is absent",
    )

    if not blocks_ordered:
        failures.append("the absolute-loading block means are not consecutively numbered")
    check(
        "block_means_numbered",
        blocks_ordered,
        f"{len(blocks)} block means" if blocks_ordered else "missing or out of order",
    )

    conversions_ok = False
    if (
        loading is not None
        and mol_per_kg is not None
        and mg_per_g is not None
        and conversion_mol is not None
        and conversion_mg is not None
    ):
        conversions_ok = _close(
            mol_per_kg.mean, loading.mean * conversion_mol, tolerance
        ) and _close(mg_per_g.mean, loading.mean * conversion_mg, tolerance)
        if not conversions_ok:
            failures.append(
                "the printed loading does not agree with RASPA's own unit "
                "conversion factors, so the units cannot be trusted"
            )
    check(
        "unit_conversions_consistent",
        conversions_ok,
        f"mol/kg factor {conversion_mol!r}, mg/g factor {conversion_mg!r}",
    )

    if failures:
        status = ResultStatus.SOFTWARE_FAILURE
        reason = "; ".join(failures)
    elif len(blocks) < min_blocks:
        status = ResultStatus.NON_CONVERGED
        reason = (
            f"the run reported {len(blocks)} block means and the fixed policy "
            f"requires at least {min_blocks}"
        )
    elif (
        loading is not None
        and loading.relative_standard_error is not None
        and loading.relative_standard_error > max_relative
    ):
        status = ResultStatus.NON_CONVERGED
        reason = (
            "the relative standard error of the absolute loading is "
            f"{loading.relative_standard_error:.6g}, above the fixed "
            f"{max_relative:g}"
        )
    else:
        status = ResultStatus.NORMAL
        reason = (
            "RASPA completed without warnings at the frozen state point, and the "
            "absolute-loading block statistics meet the fixed convergence policy"
        )
    check(
        "convergence_policy_met",
        status is ResultStatus.NORMAL,
        reason if status is ResultStatus.NORMAL else f"{status.value}: {reason}",
    )

    if exit_code != 0:
        status = ResultStatus.SOFTWARE_FAILURE
        reason = f"RASPA exited {exit_code}; " + reason

    evidence = (
        EvidenceClass.COMPUTATION
        if provenance is SourceKind.REAL_RUN
        else EvidenceClass.SIMULATED
    )
    return RaspaResult(
        package_sha256=package_sha256,
        scientific_contract_sha256=contract.sha256(),
        execution_contract_ref=contract.execution_contract_ref,
        execution_contract_version=contract.execution_contract_version,
        status=status,
        reason=reason,
        source_kind=provenance,
        evidence_class=evidence,
        software_version=version,
        software_series=series,
        temperature_k=temperature,
        pressure_pa=pressure,
        seed=seed,
        cycles=cycles,
        initialization_cycles=initialization,
        blocks=blocks,
        molecules_per_unit_cell=loading,
        mol_per_kg=mol_per_kg,
        mg_per_g=mg_per_g,
        conversion_mol_per_kg=conversion_mol,
        conversion_mg_per_g=conversion_mg,
        warnings=warnings,
        output_sha256=output_sha256,
        output_size_bytes=output_size,
        checks=tuple(checks),
    )
