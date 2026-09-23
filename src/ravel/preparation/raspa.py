"""A workspace a RASPA calculation can be started in.

RASPA is the one real compute stack V0 prepares for. What this module does is
the whole of preparation for it: read a frozen contract, write the directory
the software runs in, and record what was written. It runs nothing — the job
script it generates is what a compute backend submits, and this module never
learns whether that ever happened.

**Where every number comes from.** From the contract, and from nowhere else.
RASPA has defaults for most of its keywords and a materializer could lean on
them, but a default that RAVEL did not write down is a value the contract's
author never chose and the run's result depends on — so a contract that does
not state the temperature, the pressure, the run length, or the system's size
is refused with `MISSING_SCIENTIFIC_PARAMETER` rather than filled in. The
refusal names the keys it looked for, because the model that reads it is the
one that has to write them next.

**What is not the contract's business.** The syntax. `SimulationType
MonteCarlo`, `MoleculeDefinition Local`, that the pressure goes in pascals and
the framework file has to be named after the framework: these are facts about
the software, and a contract that had to state them would be a contract
written for one version of one program. They are decided here, documented
here, and stable.

**What is copied and what is generated.** The framework comes out of the
project — those bytes exist because something fetched or computed them, and
they were resolved by the caller into `PreparationContext.inputs`. The force
field, the pseudo-atoms, and the molecule definition come out of a RASPA
installation's data directory, because they are the software's own reference
data and RAVEL does not carry a copy: a force field is a set of numbers a
result depends on, and one RAVEL invented would be a result nobody could
attribute to anything. A deployment with no RASPA data directory configured
refuses the contract and says so.

**What a second preparation of the same contract does.** Writes the same
bytes: the workspace is keyed to a node and a contract version, a version is
frozen, and the inputs and parameters do not change. A file left behind by an
earlier materializer version would not be in the manifest — the manifest lists
what this run wrote rather than what the directory contains — and nothing in
the input file refers to it, so it is inert rather than misleading.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ravel.domain.contracts import ExecutionContract
from ravel.domain.preparation import PreparationCheck, PreparationRefusal
from ravel.preparation.workspace import (
    FileRole,
    MaterializationRefused,
    PreparationContext,
    PreparedExecution,
    PreparedInput,
    Workspace,
    build_manifest,
)

__all__ = ["REQUIRED_TERMS", "RaspaMaterializer"]

#: What the materializer's own version names. Bumped when the files it writes
#: change, because a workspace is only comparable to another one built by the
#: same version.
MATERIALIZER_VERSION = "1"

#: The contract terms a RASPA input cannot be written without, in the order a
#: refusal names them. Everything else in `parameter_targets` is read past —
#: the materializer reads these and writes nothing it was not given.
REQUIRED_TERMS: tuple[str, ...] = (
    "temperature_k",
    "pressure_bar",
    "cycles",
    "initialization_cycles",
    "unit_cells",
    "framework",
    "molecule",
)

#: The extensions RASPA reads a framework from. Checked because the framework
#: name in `simulation.input` is the file's stem and RASPA looks for the file
#: itself, so a name with an extension it cannot parse is a job that starts and
#: fails on a missing structure rather than a contract that was refused here.
_FRAMEWORK_SUFFIXES = frozenset({".cif", ".cssr", ".pdb", ".xyz", ".vasp", ".xsf"})

#: The data a RASPA installation supplies, beside the executable. Both paths are
#: relative to the configured `RAVEL_RASPA_DATA_DIR`, which is the installation's
#: `share/raspa` directory.
_FORCE_FIELD_FILES = ("force_field_mixing_rules.def", "pseudo_atoms.def")


@dataclass(frozen=True)
class RaspaMaterializer:
    """Writes one node's RASPA workspace.

    `data_dir` is the RASPA installation's `share/raspa` directory — the one
    holding `forcefield/` and `molecules/`. It is optional because a deployment
    may run RAVEL without a RASPA installed; it is not defaulted, because a
    guess about where the software keeps its force field is a guess about the
    numbers a result rests on.
    """

    #: `None` when this deployment has no RASPA data directory, which is a
    #: deployment fact rather than an error: the refusal it produces names the
    #: setting that would fix it.
    data_dir: Path | None = None
    kind: str = "software"
    name: str = "raspa"
    version: str = MATERIALIZER_VERSION

    # ── The one entry point ────────────────────────────────────────────────

    def materialize(self, context: PreparationContext) -> PreparedExecution:
        """Write the workspace this contract describes.

        Raises:
            MaterializationRefused: The contract is missing a term, states one
                this software cannot use, or the installation is not there.
        """
        contract = context.contract
        terms = _terms(contract)
        framework = _framework(context, terms)
        data_dir = self._data_dir()
        force_field = _from_installation(data_dir, "forcefield", _FORCE_FIELD_FILES[0])
        pseudo_atoms = _from_installation(data_dir, "forcefield", _FORCE_FIELD_FILES[1])
        molecule_name = terms["molecule"]
        molecule = _from_installation(data_dir, "molecules", f"{molecule_name}.def")
        limits = _resource_limits(contract)

        workspace = Workspace(context.workspace_root)
        written = workspace.write_bytes(
            framework.name, framework.data, role=FileRole.INPUT
        )
        workspace.copy_from(force_field.name, force_field, role=FileRole.INPUT)
        workspace.copy_from(pseudo_atoms.name, pseudo_atoms, role=FileRole.INPUT)
        workspace.copy_from(molecule.name, molecule, role=FileRole.INPUT)
        workspace.write_text(
            "simulation.input", _simulation_input(terms), role=FileRole.INPUT
        )
        workspace.write_text(
            "job.slurm",
            _job_script(contract, limits, context.node_display_id),
            role=FileRole.JOB_SCRIPT,
        )

        manifest = build_manifest(
            context,
            materializer=self.name,
            materializer_version=self.version,
            files=workspace.files,
            method="GCMC",
            parameters=dict(contract.parameter_targets),
            resource_request=dict(contract.resource_limits),
            inputs=[
                {
                    "name": framework.name,
                    "sha256": written.sha256,
                    "size_bytes": written.size_bytes,
                    "source": framework.source,
                }
            ],
            extra={
                "software_data_dir": str(data_dir),
                "molecule_definition": molecule.name,
            },
        )
        workspace.write_json(
            "calculation_manifest.json", manifest, role=FileRole.MANIFEST
        )

        return PreparedExecution(
            workspace_path=str(workspace.root),
            materializer=self.name,
            materializer_version=self.version,
            files=workspace.files,
            manifest=manifest,
            checks=(
                PreparationCheck(
                    name="software_available",
                    passed=True,
                    detail=f"RASPA data read from {data_dir}",
                ),
                PreparationCheck(
                    name="parameters_within_allowed_ranges",
                    passed=True,
                    detail=_ranges_checked(contract, terms),
                ),
            ),
            required_outputs=contract.required_outputs,
            execution_metadata={
                "software": self.name,
                "input": "simulation.input",
                "entrypoint": "job.slurm",
                "submitted_by": "job.slurm",
            },
        )

    # ── The installation ───────────────────────────────────────────────────

    def _data_dir(self) -> Path:
        """The RASPA data directory, having checked it is there.

        Raises:
            MaterializationRefused: No directory is configured, or it is not a
                directory. Both are `ENVIRONMENT_UNAVAILABLE` rather than
                `UNSUPPORTED_ENVIRONMENT`: RAVEL has the materializer, and this
                host cannot do its job — which is a different next step for
                Master than a plan naming software nobody has ever heard of.
        """
        if self.data_dir is None:
            raise MaterializationRefused(
                PreparationRefusal.ENVIRONMENT_UNAVAILABLE,
                "this deployment has no RASPA installation configured, so no "
                "workspace can be built for it; set RAVEL_RASPA_DATA_DIR to the "
                "share/raspa directory of an installation, or decide that this "
                "node does not run here",
            )
        root = Path(self.data_dir)
        if not root.is_dir():
            raise MaterializationRefused(
                PreparationRefusal.ENVIRONMENT_UNAVAILABLE,
                f"RAVEL_RASPA_DATA_DIR is {root} and no such directory exists; "
                "the RASPA data a workspace is built from is read from there",
            )
        return root


# ── Reading the contract ───────────────────────────────────────────────────


def _missing(terms: list[str]) -> MaterializationRefused:
    """The refusal for terms a RASPA input cannot be written without."""
    named = ", ".join(terms)
    return MaterializationRefused(
        PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
        f"the contract's parameter_targets does not state {named}, and a RASPA "
        f"input cannot be written without it; this materializer reads "
        f"{', '.join(REQUIRED_TERMS)} and supplies no value of its own, because "
        "a number RAVEL chose would be a scientific decision the contract did "
        "not make",
    )


def _terms(contract: ExecutionContract) -> dict[str, str]:
    """The contract's terms, with every one it must state checked present.

    Presence only. What each value has to *be* is checked where it is used, so
    that the message names the term and what was wrong with it rather than
    saying the contract was unreadable.

    Raises:
        MaterializationRefused: A required term is absent.
    """
    targets = contract.parameter_targets
    absent = [term for term in REQUIRED_TERMS if not targets.get(term, "").strip()]
    if absent:
        raise _missing(absent)
    return {term: targets[term].strip() for term in REQUIRED_TERMS}


def _number(term: str, text: str, *, positive: bool = True) -> float:
    """One term read as a number.

    Raises:
        MaterializationRefused: It is not a number this software can use. The
            class is `INCONSISTENT_CONTRACT` rather than a missing term: the
            contract stated a value, and the value is not one.
    """
    try:
        value = float(text)
    except ValueError:
        raise MaterializationRefused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, which is not a number; a "
            f"value is only a term if the software can be given it",
        ) from None
    if positive and value <= 0:
        raise MaterializationRefused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, and a RASPA run has no "
            "meaning at or below zero",
        )
    return value


def _whole_number(term: str, text: str) -> int:
    """One term read as a whole number of something counted."""
    value = _number(term, text)
    if value != int(value):
        raise MaterializationRefused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states {term} as {text!r}, which is not a whole "
            "number; the software counts these in units, not in fractions",
        )
    return int(value)


def _unit_cells(text: str) -> str:
    """Three whole numbers, as `UnitCells` wants them."""
    parts = text.split()
    if len(parts) != 3:
        raise MaterializationRefused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract states unit_cells as {text!r}, and a simulation box "
            "is three counts; write it as '<a> <b> <c>', for example '2 2 2'",
        )
    return " ".join(str(_whole_number("unit_cells", part)) for part in parts)


def _ranges_checked(contract: ExecutionContract, terms: dict[str, str]) -> str:
    """Check the stated values against the windows the same contract permits.

    A contract that states a temperature and a window for it has said two
    things, and a target outside its own window is the contract disagreeing
    with itself rather than a value anybody chose. Checked here because this is
    the first place both are read together — and refused, rather than allowed
    to run: the Worker would find the same disagreement at the first poll, five
    retries into an attempt nobody can see.

    Returns:
        What was compared, for the record.

    Raises:
        MaterializationRefused: A stated value is outside its permitted range.
    """
    checked: list[str] = []
    for term in ("temperature_k", "pressure_bar", "cycles"):
        window = contract.permitted_range(term)
        if window is None:
            continue
        low, high = window
        value = _number(term, terms[term])
        if not contract.permits_value(term, value):
            raise MaterializationRefused(
                PreparationRefusal.INCONSISTENT_CONTRACT,
                f"the contract states {term} as {terms[term]} and permits "
                f"{low}..{high} for it; the terms disagree, and a run is built "
                "from what the contract says either way",
            )
        checked.append(f"{term} {value} within {low}..{high}")
    return "; ".join(checked) or "the contract states no range for these terms"


def _resource_limits(contract: ExecutionContract) -> dict[str, str]:
    """The limits the job script is submitted under.

    `wall_clock_hours` is required. A job submitted without a time limit is a
    job that holds a cluster node until somebody notices, and RAVEL's answer to
    "the contract did not say" is to refuse rather than to pick a number that a
    result would then be governed by.

    Everything else is passed through when the contract states it and omitted
    when it does not, so that the scheduler's own default applies rather than
    one this module invented.

    Raises:
        MaterializationRefused: No wall-clock limit is stated.
    """
    limits = contract.resource_limits
    hours = limits.get("wall_clock_hours", "").strip()
    if not hours:
        raise MaterializationRefused(
            PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
            "the contract's resource_limits does not state wall_clock_hours, and "
            "a job script is written with the time limit the contract permits "
            "or not at all",
        )
    return limits


def _framework(context: PreparationContext, terms: dict[str, str]) -> PreparedInput:
    """The structure the calculation is run on, out of the project's own data.

    Raises:
        MaterializationRefused: The contract names a framework that is not one
            of its inputs, or whose bytes the caller could not resolve. Both
            are the contract and the project disagreeing, which is
            `INCONSISTENT_CONTRACT`: nothing is missing from what Master wrote.
    """
    name = terms["framework"]
    suffix = Path(name).suffix.lower()
    if suffix not in _FRAMEWORK_SUFFIXES:
        raise MaterializationRefused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract names framework {name!r}, which is not a structure "
            f"file RASPA reads; the formats are "
            f"{', '.join(sorted(_FRAMEWORK_SUFFIXES))}",
        )
    if name not in context.contract.inputs:
        raise MaterializationRefused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract names framework {name!r} and its inputs are "
            f"{list(context.contract.inputs)}; the framework a run is simulated "
            "on has to be one of the files the contract supplies",
        )
    supplied = context.input_named(name)
    if supplied is None:
        resolved = [entry.name for entry in context.inputs]
        raise MaterializationRefused(
            PreparationRefusal.INCONSISTENT_CONTRACT,
            f"the contract names {name!r} as an input and this project holds no "
            f"file by that name; the inputs that were resolved are {resolved or 'none'}",
        )
    return supplied


def _from_installation(data_dir: Path, subdirectory: str, filename: str) -> Path:
    """One file out of the RASPA installation.

    Raises:
        MaterializationRefused: It is not there. The installation is incomplete
            or configured wrong, which is a host fact rather than anything
            about the contract.
    """
    path = data_dir / subdirectory / filename
    if not path.is_file():
        raise MaterializationRefused(
            PreparationRefusal.ENVIRONMENT_UNAVAILABLE,
            f"the RASPA installation at {data_dir} has no {subdirectory}/{filename}; "
            "a workspace is built from the installation's own reference data, and "
            "RAVEL does not carry a copy of it",
        )
    return path


# ── What is written ────────────────────────────────────────────────────────


def _simulation_input(terms: dict[str, str]) -> str:
    """The RASPA input file, from the contract's terms and nothing else.

    Every keyword here is one the contract stated, plus the three the software
    needs to be told about the *form* of the run rather than its content:
    `SimulationType MonteCarlo`, `MoleculeDefinition Local` (the definition is
    the file beside this one, rather than one from a library path), and
    `CreateNumberOfMolecules 0` (a GCMC run starts from an empty framework and
    the swaps decide the loading). Everything else is RASPA's own default,
    which is what "not in this file" means.

    The pressure is converted from bar to pascals. That is arithmetic on a unit
    the contract named, not a choice: `pressure_bar` says what it is in.
    """
    temperature = _number("temperature_k", terms["temperature_k"])
    pressure = _number("pressure_bar", terms["pressure_bar"]) * 1e5
    cells = _unit_cells(terms["unit_cells"])
    framework_name = Path(terms["framework"]).stem
    lines = [
        "# Written by RAVEL preparation from the node's execution contract.",
        "# Values come from the contract; the form of the input is RAVEL's.",
        "SimulationType                MonteCarlo",
        f"NumberOfCycles                {_whole_number('cycles', terms['cycles'])}",
        "NumberOfInitializationCycles  "
        f"{_whole_number('initialization_cycles', terms['initialization_cycles'])}",
        "RestartFile                   no",
        "",
        "Framework 0",
        f"FrameworkName                 {framework_name}",
        f"UnitCells                     {cells}",
        f"ExternalTemperature           {temperature}",
    ]
    cutoff = terms.get("cutoff_angstrom", "").strip()
    if cutoff:
        lines.append(f"CutOff                        {_number('cutoff_angstrom', cutoff)}")
    lines += [
        "",
        "Component 0 MoleculeName      " + terms["molecule"],
        "MoleculeDefinition            Local",
        f"Pressure                      {pressure:.6g}",
        "CreateNumberOfMolecules       0",
        "",
    ]
    return "\n".join(lines)


def _job_script(
    contract: ExecutionContract, limits: dict[str, str], display_id: str
) -> str:
    """The scheduler script that starts the calculation.

    The RASPA executable is named through the environment, because where it is
    installed is a host fact rather than a contract term: a cluster that has it
    on `PATH` needs nothing, and one that does not sets `RASPA_BIN`. The script
    runs in the directory it was submitted from, which is the workspace
    preparation wrote — the one place the input files and the executable meet.
    """
    header = [
        "#!/bin/bash",
        f"#SBATCH --job-name=ravel-{display_id}",
    ]
    for key, flag in (
        ("nodes", "nodes"),
        ("cpus_per_task", "cpus-per-task"),
        ("memory_gb", "mem"),
    ):
        stated = limits.get(key, "").strip()
        if stated:
            header.append(f"#SBATCH --{flag}={_scheduler_value(key, stated)}")
    header += [
        f"#SBATCH --time={_wall_clock(limits['wall_clock_hours'])}",
        f"#SBATCH --output=ravel-{display_id}-%j.out",
        "",
        "# Generated by RAVEL preparation for node "
        f"{display_id} under execution contract {contract.contract_id} version "
        f"{contract.version}.",
        "# Edit the contract, not this file: the next preparation writes this",
        "# one again from the terms.",
        "",
        "set -euo pipefail",
        'cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")}"',
        'exec "${RASPA_BIN:-simulation}" simulation.input',
        "",
    ]
    return "\n".join(header)


def _scheduler_value(term: str, text: str) -> str:
    """One resource limit, as a number this module formatted.

    **Nothing the contract wrote is ever copied into the job script.** The value
    RAVEL read is parsed and re-formatted, so what reaches the file is a number
    rather than a term, and a term that is not a number is refused here.

    That matters more than it looks. A job script is a shell script submitted to
    a cluster and run as whoever submits it, and a `#SBATCH` header is part of
    it: a value carrying a newline ends the directive and starts a command on
    the line below, and the contract's `resource_limits` are free text written
    by Master — a model — about a machine it does not own. Interpolating that
    text unread would make a contract term a way to run commands on the compute
    host, which is exactly the authority a contract is not.

    Raises:
        MaterializationRefused: The value is not a positive number of the kind
            the limit is counted in. `INCONSISTENT_CONTRACT`, because the
            contract did state a value and this is not one the software can be
            given — the same refusal `_number` raises for a parameter.
    """
    if term == "memory_gb":
        # Not a whole number of gigabytes: half a gigabyte is a real request.
        return f"{_number(term, text):g}G"
    return str(_whole_number(term, text))


def _wall_clock(hours: str) -> str:
    """A duration in hours as the scheduler spells it, `HH:MM:SS`."""
    value = _number("wall_clock_hours", hours)
    whole = int(value)
    minutes = round((value - whole) * 60)
    return f"{whole:02d}:{minutes:02d}:00"
