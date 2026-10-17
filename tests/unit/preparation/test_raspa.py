"""What the RASPA materializer builds, and what it refuses to build.

Two halves, and they are the two halves of preparation itself. The first is
mechanical: the workspace holds the files a run reads, every one of them is
named by the digest of the bytes that landed on disk, and the manifest says
where each came from. The second is the boundary: a contract that does not
state a temperature, that states one outside its own permitted range, that
names a framework it does not supply, or that runs on a host with no RASPA
installed is *refused* — with the class that decides Master's next step and a
sentence naming what was wrong.

Both halves are tested against a fixture installation and fixture bytes, and
neither of them is chemistry. What is under test is that RAVEL copies the bytes
it was given, hashes them as it writes them, records where they came from, and
supplies no number of its own — not that the fixture is a force field. A real
force field is thousands of lines of fitted parameters that a result depends
on, and nothing in this file could stand in for one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ravel.domain.contracts import ExecutionContract
from ravel.domain.preparation import PreparationRefusal
from ravel.preparation import (
    REQUIRED_TERMS,
    MaterializationRefused,
    PreparationContext,
    PreparedExecution,
    PreparedInput,
    RaspaMaterializer,
)

#: Stand-ins for what a project and an installation supply, written so that no
#: reader can mistake them for real data.
FRAMEWORK = b"# the structure file the project supplied, standing in for a CIF\n"
FORCE_FIELD = b"# the installation's force field, standing in for thousands of lines\n"
PSEUDO_ATOMS = b"# the installation's pseudo atoms\n"
MOLECULE = b"# the installation's CO2 definition\n"

#: The contract terms a RASPA input is written from. Spelled the way Master
#: writes them, because a name this test invented would be a name no contract
#: carries.
TERMS: dict[str, str] = {
    "temperature_k": "298.0",
    "pressure_bar": "1.0",
    "cycles": "10000",
    "initialization_cycles": "5000",
    "unit_cells": "2 2 2",
    "framework": "MFI.cif",
    "molecule": "CO2",
}


def an_installation(root: Path) -> Path:
    """A directory laid out the way a RASPA installation's data is.

    `share/raspa/forcefield` and `share/raspa/molecules`, which is where the
    materializer reads from and the layout `RAVEL_RASPA_DATA_DIR` names.
    """
    data = root / "share" / "raspa"
    (data / "forcefield").mkdir(parents=True)
    (data / "molecules").mkdir()
    (data / "forcefield" / "force_field_mixing_rules.def").write_bytes(FORCE_FIELD)
    (data / "forcefield" / "pseudo_atoms.def").write_bytes(PSEUDO_ATOMS)
    (data / "molecules" / "CO2.def").write_bytes(MOLECULE)
    return data


def a_contract(**overrides: Any) -> ExecutionContract:
    fields: dict[str, object] = {
        "project_id": "proj-a",
        "node_id": "n1",
        "objective": "Simulate the CO2 isotherm in MFI at 298 K.",
        "inputs": ("MFI.cif",),
        "parameter_targets": dict(TERMS),
        "allowed_ranges": {"temperature_k": "270..330", "pressure_bar": "0.5..2"},
        "required_outputs": ("isotherm.csv",),
        "resource_limits": {"wall_clock_hours": "2", "nodes": "1"},
        "execution_requirements": {"software": "raspa"},
    }
    fields.update(overrides)
    return ExecutionContract(**fields).freeze()  # type: ignore[arg-type]


def a_context(
    root: Path,
    *,
    supplied: bool = True,
    contract: ExecutionContract | None = None,
    **overrides: Any,
) -> PreparationContext:
    return PreparationContext(
        project_id="proj-a",
        node_id="n1",
        node_display_id="N-1A2B3C4D",
        contract=contract if contract is not None else a_contract(**overrides),
        workspace_root=root,
        inputs=(PreparedInput(name="MFI.cif", data=FRAMEWORK, source="artifact_version:av-1"),)
        if supplied
        else (),
    )


def prepared(root: Path, data_dir: Path, **overrides: Any) -> dict[str, Any]:
    """Run the materializer and read the manifest back off disk.

    Read from the file rather than from the returned document, because the
    document is what the *return value* claims and the file is what a run
    actually reads.
    """
    RaspaMaterializer(data_dir=data_dir).materialize(a_context(root, **overrides))
    manifest = root / "calculation_manifest.json"
    return dict(json.loads(manifest.read_text()))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── What it builds ─────────────────────────────────────────────────────────


def test_the_workspace_holds_everything_the_run_reads(tmp_path: Path) -> None:
    """The structure, the software's own data, the input file, and the script.

    Named individually, because a workspace missing one of these is a job that
    starts and fails on the cluster — which is the failure preparation exists
    to make impossible rather than to report.
    """
    data = an_installation(tmp_path)
    workspace = tmp_path / "ws"
    executor = RaspaMaterializer(data_dir=data)

    built = executor.materialize(a_context(workspace))

    assert [(file.name, file.role.value) for file in built.files] == [
        ("MFI.cif", "INPUT"),
        ("force_field_mixing_rules.def", "INPUT"),
        ("pseudo_atoms.def", "INPUT"),
        ("CO2.def", "INPUT"),
        ("simulation.input", "INPUT"),
        ("job.slurm", "JOB_SCRIPT"),
        ("calculation_manifest.json", "MANIFEST"),
    ]
    assert (workspace / "MFI.cif").read_bytes() == FRAMEWORK
    assert (workspace / "force_field_mixing_rules.def").read_bytes() == FORCE_FIELD
    assert (workspace / "CO2.def").read_bytes() == MOLECULE


def test_every_file_is_named_by_the_bytes_that_landed(tmp_path: Path) -> None:
    """The manifest's digests against the files on disk, one by one.

    This is the property the whole document rests on: a hash taken from a
    source rather than from what was written is a hash of a file the run may
    never have read.
    """
    data = an_installation(tmp_path)
    workspace = tmp_path / "ws"
    manifest = prepared(workspace, data)

    listed = manifest["generated_files"]
    assert isinstance(listed, list)
    for entry in listed:
        on_disk = (workspace / entry["name"]).read_bytes()
        assert entry["sha256"] == digest(on_disk), entry["name"]
        assert entry["size_bytes"] == len(on_disk), entry["name"]


def test_the_manifest_names_the_contract_the_workspace_was_built_from(
    tmp_path: Path,
) -> None:
    """Including the parameters, which are what a result is compared against."""
    data = an_installation(tmp_path)
    context = a_context(tmp_path / "ws")
    RaspaMaterializer(data_dir=data).materialize(context)
    manifest = json.loads((tmp_path / "ws" / "calculation_manifest.json").read_text())
    contract = context.contract

    assert manifest["project_id"] == "proj-a"
    assert manifest["node_id"] == "n1"
    assert manifest["node_display_id"] == "N-1A2B3C4D"
    assert manifest["execution_contract_ref"] == contract.contract_id
    assert manifest["execution_contract_version"] == 1
    assert manifest["materializer"] == "raspa"
    assert manifest["materializer_version"] == RaspaMaterializer(data_dir=data).version
    assert manifest["method"] == "GCMC"
    assert manifest["parameters"] == TERMS
    assert manifest["resource_request"] == {"wall_clock_hours": "2", "nodes": "1"}
    assert manifest["required_outputs"] == ["isotherm.csv"]
    assert manifest["software_data_dir"] == str(data)
    assert manifest["molecule_definition"] == "CO2.def"
    assert manifest["generated_at"]


def test_the_manifest_records_where_the_framework_came_from(tmp_path: Path) -> None:
    """The file that was copied, and which version of it was read.

    `generated_files` says what is in the directory; this says where the bytes
    came from, which is the question a reviewer asks when a result is not what
    the approved structure would have produced.
    """
    data = an_installation(tmp_path)
    workspace = tmp_path / "ws"
    manifest = prepared(workspace, data)

    assert manifest["inputs"] == [
        {
            "name": "MFI.cif",
            "sha256": digest(FRAMEWORK),
            "size_bytes": len(FRAMEWORK),
            "source": "artifact_version:av-1",
        }
    ]
    assert (workspace / "MFI.cif").read_bytes() == FRAMEWORK


def test_the_input_file_states_the_terms_the_contract_gave(tmp_path: Path) -> None:
    """Every number in it is one the contract stated, in the software's units.

    The pressure is the one value that is not written as given: a contract says
    bar because that is how a chemist says it, and RASPA is given pascals. That
    is arithmetic on a named unit rather than a choice, which is why it is the
    one transformation here.
    """
    data = an_installation(tmp_path)
    RaspaMaterializer(data_dir=data).materialize(a_context(tmp_path / "ws"))
    text = (tmp_path / "ws" / "simulation.input").read_text()

    assert "SimulationType                MonteCarlo" in text
    assert "NumberOfCycles                10000" in text
    assert "NumberOfInitializationCycles  5000" in text
    assert "FrameworkName                 MFI" in text
    assert "UnitCells                     2 2 2" in text
    assert "ExternalTemperature           298.0" in text
    assert "Pressure                      100000" in text
    assert "MoleculeDefinition            Local" in text
    assert "CutOff" not in text


def test_a_term_the_software_does_not_read_is_carried_and_not_written(
    tmp_path: Path,
) -> None:
    """A parameter the materializer does not know is not a parameter it drops.

    It stays in the manifest, because the manifest is the record of what the
    contract said. It does not reach the input file, because a keyword RAVEL
    invented for it would be a keyword the software ignores — which reads, in
    the record, exactly like a term that was honoured.
    """
    data = an_installation(tmp_path)
    terms = dict(TERMS, sample_mass_g="0.5")
    workspace = tmp_path / "ws"
    manifest = prepared(workspace, data, parameter_targets=terms)

    text = (workspace / "simulation.input").read_text()
    assert "0.5" not in text
    assert manifest["parameters"] == terms


def test_preparing_one_contract_twice_writes_the_same_files(tmp_path: Path) -> None:
    """A retried activity writes the same inputs, byte for byte.

    Which is what makes a second preparation harmless: everything a run reads
    has the same digest as it had the first time, so a run that was prepared
    twice cannot be a run whose inputs changed underneath it. The manifest is
    the one file that differs, and it differs in the one field that should —
    when it was written. A manifest that came out byte-identical would be
    claiming the second preparation happened at the moment of the first.
    """
    data = an_installation(tmp_path)
    executor = RaspaMaterializer(data_dir=data)
    contract = a_contract()

    first = executor.materialize(a_context(tmp_path / "one", contract=contract))
    second = executor.materialize(a_context(tmp_path / "two", contract=contract))

    def read_files(built: PreparedExecution) -> list[tuple[str, str]]:
        """Everything the manifest does not list itself under, name and digest."""
        return [
            (file.name, file.sha256)
            for file in built.files
            if file.name != "calculation_manifest.json"
        ]

    def without_stamp(document: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in document.items() if key != "generated_at"}

    assert read_files(first) == read_files(second)
    assert without_stamp(first.manifest) == without_stamp(second.manifest)
    assert first.manifest["generated_at"]


def test_the_checks_record_what_was_verified(tmp_path: Path) -> None:
    """Not only that it passed: what was looked at, where, and against what."""
    data = an_installation(tmp_path)
    built = RaspaMaterializer(data_dir=data).materialize(a_context(tmp_path / "ws"))
    checks = {check.name: check for check in built.checks}

    assert checks["software_available"].passed
    assert str(data) in checks["software_available"].detail
    assert checks["parameters_within_allowed_ranges"].passed
    assert "temperature_k 298.0 within 270.0..330.0" in (
        checks["parameters_within_allowed_ranges"].detail
    )


def test_the_worker_is_told_where_the_run_begins(tmp_path: Path) -> None:
    """The entry point, in the words a Worker states before it starts."""
    data = an_installation(tmp_path)
    workspace = tmp_path / "ws"
    built = RaspaMaterializer(data_dir=data).materialize(a_context(workspace))

    assert built.workspace_path == str(workspace)
    assert built.required_outputs == ("isotherm.csv",)
    assert built.execution_metadata["entrypoint"] == "job.slurm"
    assert built.execution_metadata["input"] == "simulation.input"
    assert built.execution_metadata["software"] == "raspa"


# ── What it refuses ────────────────────────────────────────────────────────


@pytest.mark.parametrize("term", REQUIRED_TERMS)
def test_a_term_the_contract_does_not_state_is_refused(tmp_path: Path, term: str) -> None:
    """The refusal that stops this materializer from doing science.

    A default would be a value the contract's author never chose and the
    result depends on, so a missing term is refused rather than filled in — and
    the sentence names the term, because the model reading it is the one that
    has to write it next.
    """
    data = an_installation(tmp_path)
    terms = {key: value for key, value in TERMS.items() if key != term}

    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(
            a_context(tmp_path / "ws", parameter_targets=terms)
        )

    assert refused.value.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert term in str(refused.value)
    assert "parameter_targets" in str(refused.value)


def test_a_term_that_is_not_a_number_is_refused(tmp_path: Path) -> None:
    """The contract stated a value, and the value is not one it can be given."""
    data = an_installation(tmp_path)
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(
            a_context(tmp_path / "ws", parameter_targets=dict(TERMS, cycles="many"))
        )
    assert refused.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "cycles" in str(refused.value)
    assert "many" in str(refused.value)


def test_a_value_outside_the_contracts_own_range_is_refused(tmp_path: Path) -> None:
    """The contract disagreeing with itself, caught before a run is built.

    A target outside the window the same contract permits would be found by the
    Worker at its first poll, five retries into an attempt nobody is watching.
    Refusing here is the same finding, five retries earlier and in front of the
    role that wrote both numbers.
    """
    data = an_installation(tmp_path)
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(
            a_context(tmp_path / "ws", parameter_targets=dict(TERMS, temperature_k="400"))
        )
    assert refused.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    message = str(refused.value)
    assert "temperature_k" in message
    assert "400" in message
    assert "270.0..330.0" in message


def test_unit_cells_that_are_not_three_counts_are_refused(tmp_path: Path) -> None:
    """The refusal says how to write one, because the reading model must fix it."""
    data = an_installation(tmp_path)
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(
            a_context(tmp_path / "ws", parameter_targets=dict(TERMS, unit_cells="2 2"))
        )
    assert refused.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "'2 2'" in str(refused.value)
    assert "2 2 2" in str(refused.value)


def test_a_deployment_with_no_raspa_installation_is_refused(tmp_path: Path) -> None:
    """Every host does not have the software, and that is not the contract's fault.

    The class is `ENVIRONMENT_UNAVAILABLE` rather than `UNSUPPORTED_ENVIRONMENT`
    because RAVEL does have the materializer: what is missing is an installation,
    and the sentence names the setting that would supply one.
    """
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=None).materialize(a_context(tmp_path / "ws"))
    assert refused.value.refusal is PreparationRefusal.ENVIRONMENT_UNAVAILABLE
    assert "RAVEL_RASPA_DATA_DIR" in str(refused.value)


def test_a_configured_installation_that_is_not_there_is_refused(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere"
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=missing).materialize(a_context(tmp_path / "ws"))
    assert refused.value.refusal is PreparationRefusal.ENVIRONMENT_UNAVAILABLE
    assert str(missing) in str(refused.value)


def test_a_molecule_the_installation_does_not_define_is_refused(tmp_path: Path) -> None:
    data = an_installation(tmp_path)
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(
            a_context(tmp_path / "ws", parameter_targets=dict(TERMS, molecule="Xe"))
        )
    assert refused.value.refusal is PreparationRefusal.ENVIRONMENT_UNAVAILABLE
    assert "molecules/Xe.def" in str(refused.value)


def test_a_molecule_named_as_a_path_is_refused(tmp_path: Path) -> None:
    """A contract decides what is simulated, never where the files come from.

    The molecule is the one value in this materializer that a model chose. Left
    unread, `../../../../etc/hostname` would be joined onto the installation's
    molecules directory, found to be a file, and copied into the workspace — an
    arbitrary read driven by a string in a contract, and the workspace is
    handed to a Compute Worker.
    """
    data = an_installation(tmp_path)
    outside = tmp_path / "outside.def"
    outside.write_text("# not a molecule\n")
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(
            a_context(
                tmp_path / "ws",
                parameter_targets=dict(TERMS, molecule="../outside"),
            )
        )
    assert refused.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "../outside" in str(refused.value)
    assert not (tmp_path / "ws" / "outside.def").exists()


def test_an_installation_missing_its_force_field_is_refused(tmp_path: Path) -> None:
    data = an_installation(tmp_path)
    (data / "forcefield" / "force_field_mixing_rules.def").unlink()
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(a_context(tmp_path / "ws"))
    assert refused.value.refusal is PreparationRefusal.ENVIRONMENT_UNAVAILABLE
    assert "force_field_mixing_rules.def" in str(refused.value)


def test_a_framework_the_contract_does_not_supply_is_refused(tmp_path: Path) -> None:
    """The contract and the project disagreeing about which file this is."""
    data = an_installation(tmp_path)
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(
            a_context(tmp_path / "ws", parameter_targets=dict(TERMS, framework="ZIF.cif"))
        )
    assert refused.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    message = str(refused.value)
    assert "ZIF.cif" in message
    assert "MFI.cif" in message


def test_a_framework_the_project_holds_nothing_under_is_refused(tmp_path: Path) -> None:
    """Named as an input, and no such file was resolved.

    The caller could not find it — the project has no artifact by that name —
    and the refusal says which inputs *were* resolved, because that is what
    Master needs to see to know whether the contract is wrong or the project
    is short of a file.
    """
    data = an_installation(tmp_path)
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(
            a_context(tmp_path / "ws", supplied=False)
        )
    assert refused.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "MFI.cif" in str(refused.value)
    assert "none" in str(refused.value)


def test_a_framework_in_a_format_the_software_cannot_read_is_refused(
    tmp_path: Path,
) -> None:
    """Checked because the framework's *name* is what the input file states."""
    data = an_installation(tmp_path)
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(
            a_context(
                tmp_path / "ws",
                inputs=("MFI.txt",),
                parameter_targets=dict(TERMS, framework="MFI.txt"),
            )
        )
    assert refused.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert ".cif" in str(refused.value)


def test_a_job_with_no_time_limit_is_refused(tmp_path: Path) -> None:
    """A job submitted without one holds a cluster node until somebody notices."""
    data = an_installation(tmp_path)
    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(
            a_context(tmp_path / "ws", resource_limits={"nodes": "1"})
        )
    assert refused.value.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert "wall_clock_hours" in str(refused.value)


# ── The job script ─────────────────────────────────────────────────────────


def test_the_job_script_states_the_limits_the_contract_gave(tmp_path: Path) -> None:
    data = an_installation(tmp_path)
    RaspaMaterializer(data_dir=data).materialize(a_context(tmp_path / "ws"))
    script = (tmp_path / "ws" / "job.slurm").read_text()

    assert script.startswith("#!/bin/bash\n")
    assert "#SBATCH --job-name=ravel-N-1A2B3C4D" in script
    assert "#SBATCH --nodes=1" in script
    assert "#SBATCH --time=02:00:00" in script
    assert "simulation.input" in script
    assert "RASPA_BIN" in script


def test_a_limit_the_contract_does_not_state_is_not_invented(tmp_path: Path) -> None:
    """No `--nodes` line rather than a node count RAVEL picked.

    The scheduler's own default then applies, which is a deployment fact a
    cluster administrator set — unlike a number this module made up and wrote
    into the record as though the contract had stated it.
    """
    data = an_installation(tmp_path)
    RaspaMaterializer(data_dir=data).materialize(
        a_context(tmp_path / "ws", resource_limits={"wall_clock_hours": "1.5"})
    )
    script = (tmp_path / "ws" / "job.slurm").read_text()

    assert "--nodes" not in script
    assert "--time=01:30:00" in script


def test_a_limit_that_is_not_a_number_never_reaches_the_job_script(
    tmp_path: Path,
) -> None:
    """A contract term is not text to copy into a shell script.

    A `#SBATCH` header is part of the script the cluster runs, so a value
    carrying a newline ends the directive and starts a command on the line
    below. `resource_limits` is free text written by Master about a machine it
    does not own, and the term below is that shape exactly: it is refused
    rather than read, written, or stripped down to something that would run.

    Asserted on the *refusal* and on the absence of a file, because a
    materializer that wrote the script and then complained would already have
    left the command somewhere a later step could submit.
    """
    data = an_installation(tmp_path)
    workspace = tmp_path / "ws"
    smuggled = "1\necho pwned > /tmp/ravel-was-here"

    with pytest.raises(MaterializationRefused) as refused:
        RaspaMaterializer(data_dir=data).materialize(
            a_context(
                workspace,
                resource_limits={"wall_clock_hours": "2", "nodes": smuggled},
            )
        )

    assert refused.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "nodes" in str(refused.value)
    assert not (workspace / "job.slurm").exists()


def test_every_limit_is_written_as_the_number_that_was_read(tmp_path: Path) -> None:
    """What reaches the script is formatted by RAVEL, not copied from the contract.

    The contract spells these with spaces and a trailing zero; the script has
    neither. That is the visible half of the rule above: the file holds numbers
    this module produced, so there is no path by which a term becomes part of
    the script as text.
    """
    data = an_installation(tmp_path)
    RaspaMaterializer(data_dir=data).materialize(
        a_context(
            tmp_path / "ws",
            resource_limits={
                "wall_clock_hours": "2",
                "nodes": " 1 ",
                "cpus_per_task": "16",
                "memory_gb": "4.0",
            },
        )
    )
    script = (tmp_path / "ws" / "job.slurm").read_text()

    assert "#SBATCH --nodes=1" in script
    assert "#SBATCH --cpus-per-task=16" in script
    assert "#SBATCH --mem=4G" in script
