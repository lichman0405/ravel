"""The frozen scientific terms a RASPA GCMC package is written from.

A `parameter_targets` dictionary is enough to *carry* a contract, but it is not
enough to build a calculation from unless every value that matters is present
and readable. This module is the boundary between the two: it reads the
contract, refuses anything the route cannot execute, canonicalises the units,
and returns an immutable record whose serialisation is the basis of the package
hash.

**Nothing is defaulted.** RASPA has defaults for nearly every keyword, and a
default RAVEL did not write down is a value the contract's author never chose
while the result depends on it. Temperature, pressure, and their units, the
force field, the structure, the molecule and its definition library, the
sampling parameters, and the random seed are all required. A contract missing
one is refused with `MISSING_SCIENTIFIC_PARAMETER` and the refusal names it.

**The unit is a term, not a suffix.** `temperature = 300` is not a scientific
value until the contract says whether it is kelvin, celsius, or something else.
The same pressure written in bar and in pascal is the same state point only
after conversion, and the conversion is arithmetic on a stated unit rather
than a guess. The canonical record stores K and Pa; the source spelling is
kept beside them so the manifest can show what the contract actually said.

**Method is a closed set of one.** Only GCMC. The contract validator does not
try to infer an ensemble from other keywords; a contract that names another
method is refused with `UNSUPPORTED_ENVIRONMENT`, because this route is not
that calculation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from ravel.compute.raspa.route import (
    ROUTE_ID,
    SUPPORTED_METHOD,
    SUPPORTED_VERSION_SERIES,
    canonical_method,
)
from ravel.domain.contracts import ExecutionContract
from ravel.domain.preparation import PreparationRefusal
from ravel.preparation.workspace import MaterializationRefused

__all__ = [
    "REQUIRED_TERMS",
    "RaspaScientificContract",
]

#: Every contract term this route cannot be built without. The order is the
#: order a refusal names them in, and it is the list the missing-term test
#: parametrises over.
REQUIRED_TERMS: tuple[str, ...] = (
    "method",
    "temperature",
    "temperature_unit",
    "pressure",
    "pressure_unit",
    "cycles",
    "initialization_cycles",
    "print_every",
    "unit_cells",
    "translation_probability",
    "reinsertion_probability",
    "swap_probability",
    "seed",
    "framework",
    "molecule",
    "molecule_definition",
    "force_field",
)

#: A name that is a single path component and cannot inject a newline, a
#: separator, or a shell metacharacter into a generated input file. Molecule
#: and force-field names are model-supplied values, and a model-supplied value
#: decides *what* is simulated, never where a file is read from.
_BARE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")

_FRAMEWORK_SUFFIXES = frozenset({".cif", ".cssr", ".pdb", ".xyz", ".vasp", ".xsf"})


def _refused(refusal: PreparationRefusal, reason: str) -> MaterializationRefused:
    return MaterializationRefused(refusal, reason)


def _missing(terms: list[str]) -> MaterializationRefused:
    return _refused(
        PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
        "the contract's parameter_targets does not state "
        + ", ".join(terms)
        + "; a RASPA GCMC package cannot be written without it, and this route "
        "supplies no scientific value of its own. A missing value is a research "
        "or contract decision for Master, not a default for the materializer.",
    )


def _text(terms: dict[str, str], name: str) -> str:
    return terms[name].strip()


def _number(
    term: str,
    text: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
    maximum: float | None = None,
) -> float:
    try:
        value = float(text)
    except ValueError:
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, which is not a number; a "
            "value is only a term if the software can be given it",
        ) from None
    if not math.isfinite(value):
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, which is not a finite "
            "number; a simulation cannot be run at an undefined state point",
        )
    if positive and value <= 0:
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, and this route has no "
            "meaning at or below zero",
        )
    if non_negative and value < 0:
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, and a negative value is "
            "not a state this software can be asked for",
        )
    if maximum is not None and value > maximum:
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, above the maximum "
            f"{maximum!r} this route accepts",
        )
    return value


def _integer(term: str, text: str, *, minimum: int) -> int:
    value = _number(term, text, positive=minimum > 0, non_negative=minimum == 0)
    if value != int(value):
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, which is not a whole "
            "number; the software counts these in whole units",
        )
    whole = int(value)
    if whole < minimum:
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, below the minimum "
            f"{minimum}; this route cannot run with it",
        )
    return whole


def _bare_name(term: str, value: str) -> str:
    if not _BARE_NAME.fullmatch(value):
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {value!r}, which is not a single "
            "name; names select a definition inside the package and never a "
            "path outside it. Separators, whitespace, and control characters "
            "are refused.",
        )
    return value


def _unit(term: str, text: str, accepted: dict[str, str]) -> str:
    value = text.strip().lower()
    canonical = accepted.get(value)
    if canonical is None:
        options = ", ".join(sorted(set(accepted.values())))
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, which this route does "
            f"not accept; the accepted units are {options}",
        )
    return canonical


def _unit_cells(term: str, text: str) -> tuple[int, int, int]:
    parts = text.split()
    if len(parts) != 3:
        raise _refused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, and a simulation box is "
            "three counts; write it as '<a> <b> <c>', for example '2 2 2'",
        )
    counts = tuple(_integer(term, part, minimum=1) for part in parts)
    return counts  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class RaspaScientificContract:
    """The canonical scientific terms, immutable and serialisable.

    Values are canonical (K and Pa). ``pressure_source_*`` keeps what the
    contract wrote, so the manifest can show a conversion rather than only its
    result. The record has no timestamp: two preparations of the same frozen
    contract must serialise identically.
    """

    execution_contract_ref: str
    execution_contract_version: int
    method: str
    temperature_k: float
    pressure_pa: float
    pressure_source_value: float
    pressure_source_unit: str
    cycles: int
    initialization_cycles: int
    print_every: int
    unit_cells: tuple[int, int, int]
    translation_probability: float
    reinsertion_probability: float
    swap_probability: float
    seed: int
    framework: str
    molecule: str
    molecule_definition: str
    force_field: str
    helium_void_fraction: float | None = None
    cutoff_angstrom: float | None = None
    remove_atom_number_code_from_label: str | None = None

    @classmethod
    def from_execution_contract(cls, contract: ExecutionContract) -> RaspaScientificContract:
        """Read and validate the route's terms from a frozen execution contract.

        Raises:
            MaterializationRefused: A required term is absent, a stated value
                is unusable, or the contract names a method or environment this
                route does not implement.
        """
        targets = contract.parameter_targets
        absent = [term for term in REQUIRED_TERMS if not targets.get(term, "").strip()]
        if absent:
            raise _missing(absent)

        requirements = contract.execution_requirements
        software = requirements.get("software", "").strip()
        if software and software.lower() != "raspa":
            raise _refused(
                PreparationRefusal.UNSUPPORTED_ENVIRONMENT,
                f"the contract requires software {software!r}, and this package "
                "is the RASPA route; a materializer for another package is not "
                "this one under a different name",
            )

        method = canonical_method(_text(targets, "method"))
        if method != SUPPORTED_METHOD:
            raise _refused(
                PreparationRefusal.UNSUPPORTED_ENVIRONMENT,
                f"the contract names method {_text(targets, 'method')!r}; this "
                f"route supports only {SUPPORTED_METHOD} gas adsorption in RASPA "
                f"{SUPPORTED_VERSION_SERIES}.x. Conductivity, DFT, molecular "
                "dynamics, Gibbs-ensemble, and other calculation types are not "
                "alternative spellings of this one and are not prepared here.",
            )

        _unit(
            "temperature_unit",
            _text(targets, "temperature_unit"),
            {"k": "K", "kelvin": "K", "kelvins": "K"},
        )
        pressure_unit = _unit(
            "pressure_unit",
            _text(targets, "pressure_unit"),
            {
                "bar": "bar",
                "bars": "bar",
                "pa": "Pa",
                "pascal": "Pa",
                "pascals": "Pa",
            },
        )
        temperature = _number("temperature", _text(targets, "temperature"), positive=True)
        pressure = _number("pressure", _text(targets, "pressure"), positive=True)
        pressure_pa = pressure * 1e5 if pressure_unit == "bar" else pressure

        cycles = _integer("cycles", _text(targets, "cycles"), minimum=1)
        initialization = _integer(
            "initialization_cycles", _text(targets, "initialization_cycles"), minimum=0
        )
        print_every = _integer("print_every", _text(targets, "print_every"), minimum=1)
        if print_every > cycles:
            raise _refused(
                PreparationRefusal.INCONSISTENT_CONTRACT,
                f"the contract states print_every={print_every} with only "
                f"{cycles} production cycles; the run would finish before its "
                "first production block and no converged average could exist",
            )

        cells = _unit_cells("unit_cells", _text(targets, "unit_cells"))
        translation = _number(
            "translation_probability",
            _text(targets, "translation_probability"),
            non_negative=True,
            maximum=1.0,
        )
        reinsertion = _number(
            "reinsertion_probability",
            _text(targets, "reinsertion_probability"),
            non_negative=True,
            maximum=1.0,
        )
        swap = _number(
            "swap_probability",
            _text(targets, "swap_probability"),
            non_negative=True,
            maximum=1.0,
        )
        if swap <= 0:
            raise _refused(
                PreparationRefusal.INCONSISTENT_CONTRACT,
                "the contract states swap_probability=0. With no insertion or "
                "deletion move this is not a grand-canonical adsorption run, "
                "and its loading would not be an adsorption result",
            )

        seed = _integer("seed", _text(targets, "seed"), minimum=1)
        if seed > 2**64 - 1:
            raise _refused(
                PreparationRefusal.INCONSISTENT_CONTRACT,
                f"the contract states seed={seed}, outside the unsigned 64-bit "
                "range RASPA accepts; the software would truncate it and the run "
                "would not use the seed the contract recorded",
            )

        framework = _text(targets, "framework")
        suffix = Path(framework).suffix.lower()
        if suffix not in _FRAMEWORK_SUFFIXES:
            raise _refused(
                PreparationRefusal.INCONSISTENT_CONTRACT,
                f"the contract names framework {framework!r}, which is not a "
                "structure file this route reads; the formats are "
                f"{', '.join(sorted(_FRAMEWORK_SUFFIXES))}",
            )
        _bare_name("framework", framework)
        molecule = _bare_name("molecule", _text(targets, "molecule"))
        molecule_definition = _bare_name(
            "molecule_definition", _text(targets, "molecule_definition")
        )
        force_field = _bare_name("force_field", _text(targets, "force_field"))

        helium = None
        if targets.get("helium_void_fraction", "").strip():
            helium = _number(
                "helium_void_fraction",
                _text(targets, "helium_void_fraction"),
                non_negative=True,
                maximum=1.0,
            )
        cutoff = None
        if targets.get("cutoff_angstrom", "").strip():
            cutoff = _number("cutoff_angstrom", _text(targets, "cutoff_angstrom"), positive=True)
        label = None
        if targets.get("remove_atom_number_code_from_label", "").strip():
            label = _unit(
                "remove_atom_number_code_from_label",
                _text(targets, "remove_atom_number_code_from_label"),
                {"yes": "yes", "no": "no"},
            )

        return cls(
            execution_contract_ref=contract.contract_id,
            execution_contract_version=contract.version,
            method=method,
            temperature_k=temperature,
            pressure_pa=pressure_pa,
            pressure_source_value=pressure,
            # The canonical unit is Pa; the source unit is what the contract
            # said. `temperature_unit` is accepted only as K, so it has no
            # separate source field to keep.
            pressure_source_unit="Pa" if pressure_unit == "Pa" else "bar",
            cycles=cycles,
            initialization_cycles=initialization,
            print_every=print_every,
            unit_cells=cells,
            translation_probability=translation,
            reinsertion_probability=reinsertion,
            swap_probability=swap,
            seed=seed,
            framework=framework,
            molecule=molecule,
            molecule_definition=molecule_definition,
            force_field=force_field,
            helium_void_fraction=helium,
            cutoff_angstrom=cutoff,
            remove_atom_number_code_from_label=label,
        )

    # ── Canonical forms ────────────────────────────────────────────────────

    @property
    def framework_name(self) -> str:
        """The framework name RASPA writes into `FrameworkName`."""
        return Path(self.framework).stem

    def temperature_document(self) -> dict[str, object]:
        return {"value": self.temperature_k, "unit": "K"}

    def pressure_document(self) -> dict[str, object]:
        return {
            "value": self.pressure_pa,
            "unit": "Pa",
            "source_value": self.pressure_source_value,
            "source_unit": self.pressure_source_unit,
        }

    def sampling_document(self) -> dict[str, object]:
        return {
            "cycles": self.cycles,
            "initialization_cycles": self.initialization_cycles,
            "print_every": self.print_every,
            "unit_cells": list(self.unit_cells),
            "translation_probability": self.translation_probability,
            "reinsertion_probability": self.reinsertion_probability,
            "swap_probability": self.swap_probability,
            "seed": self.seed,
        }

    def system_document(self) -> dict[str, object]:
        return {
            "framework": self.framework,
            "molecule": self.molecule,
            "molecule_definition": self.molecule_definition,
            "force_field": self.force_field,
            "helium_void_fraction": self.helium_void_fraction,
            "cutoff_angstrom": self.cutoff_angstrom,
            "remove_atom_number_code_from_label": self.remove_atom_number_code_from_label,
        }

    def to_document(self) -> dict[str, object]:
        """The canonical contract document, with units stated explicitly."""
        return {
            "route_id": ROUTE_ID,
            "software": {"name": "RASPA", "version_series": SUPPORTED_VERSION_SERIES},
            "execution_contract": {
                "contract_ref": self.execution_contract_ref,
                "contract_version": self.execution_contract_version,
            },
            "method": self.method,
            "temperature": self.temperature_document(),
            "pressure": self.pressure_document(),
            "sampling": self.sampling_document(),
            "system": self.system_document(),
            "units": {
                "temperature": "K",
                "pressure": "Pa",
                "pressure_source": self.pressure_source_unit,
                "loading_primary": "mol/kg framework",
                "loading_secondary": "molecules/unit cell",
                "seed": "dimensionless",
                "unit_cells": "count",
                "probability": "dimensionless",
            },
        }

    def canonical_json(self) -> str:
        """A stable serialisation: sorted keys, no whitespace, UTF-8 text."""
        return json.dumps(
            self.to_document(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def sha256(self) -> str:
        """The digest of the canonical document."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def simulation_input(self) -> str:
        """The exact RASPA input file, deterministically from the terms.

        The keywords that describe the *form* of a GCMC run rather than its
        content are fixed here (`SimulationType MonteCarlo`,
        `MoleculeDefinition` from the contract's library, `CreateNumberOfMolecules
        0`). Every scientific value comes from the frozen contract.
        """
        lines = [
            "# Written by RAVEL T10 from a frozen execution contract.",
            "# Scientific values come from the contract; the input syntax is RAVEL's.",
            "SimulationType                MonteCarlo",
            f"RandomSeed                    {self.seed}",
            f"NumberOfCycles                {self.cycles}",
            f"NumberOfInitializationCycles  {self.initialization_cycles}",
            f"PrintEvery                    {self.print_every}",
        ]
        if self.remove_atom_number_code_from_label is not None:
            lines.append(
                "RemoveAtomNumberCodeFromLabel "
                f"{self.remove_atom_number_code_from_label}"
            )
        lines += [
            f"Forcefield                    {self.force_field}",
            "",
            "Framework 0",
            f"FrameworkName                 {self.framework_name}",
            f"UnitCells                     {' '.join(str(cell) for cell in self.unit_cells)}",
        ]
        if self.helium_void_fraction is not None:
            lines.append(f"HeliumVoidFraction            {self.helium_void_fraction:.10g}")
        lines.append(f"ExternalTemperature           {self.temperature_k:.10g}")
        lines.append(f"ExternalPressure              {self.pressure_pa:.10g}")
        if self.cutoff_angstrom is not None:
            lines.append(f"CutOff                        {self.cutoff_angstrom:.10g}")
        lines += [
            "",
            f"Component 0 MoleculeName      {self.molecule}",
            f"MoleculeDefinition            {self.molecule_definition}",
            f"TranslationProbability        {self.translation_probability:.10g}",
            f"ReinsertionProbability        {self.reinsertion_probability:.10g}",
            f"SwapProbability               {self.swap_probability:.10g}",
            "CreateNumberOfMolecules       0",
            "",
        ]
        return "\n".join(lines)

    def output_filename(self) -> str:
        """The output path RASPA 2.0 will write for this system.

        RASPA names its output from the framework, unit-cell counts,
        temperature formatted with six decimals, and pressure in pascals
        formatted with six significant digits. The name is computed rather than
        guessed so that `required_outputs` can state the exact file before the
        run, and a wrong pressure or temperature in the input produces a missing
        output rather than a plausible one from another state point.
        """
        a, b, c = self.unit_cells
        pressure = f"{self.pressure_pa:.6g}"
        return (
            f"Output/System_0/output_{self.framework_name}_{a}.{b}.{c}_"
            f"{self.temperature_k:.6f}_{pressure}.data"
        )
