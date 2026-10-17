"""What the laboratory materializer builds, and what it refuses to build.

A bench package is judged by a different standard from a computation workspace.
Nobody runs it, so a malformed file is not a crash — it is a person reading a
protocol that says something the contract did not, or a sheet whose rows do not
match what the delivery will be measured against. So the assertions here are
about *what a person is told*: the protocol states the contract's own terms, the
template has one row per required output, the checklist quotes the criteria that
were frozen rather than any RAVEL wrote, and the three optional files appear
when a stated fact calls for them and not otherwise.

The other half is the refusal, and it is the half that matters most. A
laboratory materializer is the one place in RAVEL where inventing a number would
look helpful — a missing concentration has an obvious-looking value, and a
protocol with a gap in it is less useful than one without. **It must not.** Each
term it cannot do without has a test below that removes exactly that term and
asserts a refusal naming it, because the way this layer would fail is not by
crashing but by being quietly reasonable.

The fixture terms are not chemistry. They are the shape of a contract that
reaches a bench: something to do, something to do it to, conditions to do it
under, and criteria to be measured by.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from ravel.domain.contracts import AcceptanceCriterion, ExecutionContract
from ravel.domain.enums import CriterionProvenance
from ravel.domain.preparation import PreparationRefusal
from ravel.preparation import (
    AcceptanceTerms,
    LabMaterializer,
    MaterializationRefused,
    PreparationContext,
    PreparedInput,
)

#: The criteria the run will be judged by. Frozen before it starts, in the real
#: flow, and handed to the materializer rather than looked up by it.
CRITERIA = AcceptanceTerms(
    contract_id="ac-1",
    version=2,
    criteria=(
        AcceptanceCriterion(
            criterion_id="c-1",
            statement="The measured uptake is within 10% of the simulated value.",
            metric="uptake",
            threshold="within 10%",
            provenance=CriterionProvenance.LITERATURE_DERIVED,
            provenance_ref="10.1000/example",
        ),
        AcceptanceCriterion(
            criterion_id="c-2",
            statement="The sample mass is recorded for every run.",
            metric="mass",
            threshold="recorded",
            provenance=CriterionProvenance.USER_REQUIREMENT,
        ),
    ),
)

#: One sample the bench is given, and one document the contract also names.
#: A laboratory contract's inputs are usually physical things rather than
#: files, which is why only one of these resolves to bytes.
SAMPLES = ("batch-17 powder", "reference alumina")

CONDITIONS = {"temperature_c": "25", "relative_humidity_pct": "40"}

#: The one input that resolves to bytes. A stand-in, labelled as one: what the
#: materializer does with it is record where it came from and hash it.
CERTIFICATE = b"# a certificate of analysis, standing in for the real one\n"


def a_contract(**overrides: Any) -> ExecutionContract:
    """A contract of the shape that reaches a bench."""
    fields: dict[str, object] = {
        "project_id": "proj-a",
        "node_id": "n1",
        "objective": "Measure the water uptake of the doped sample.",
        "procedure": (
            "1. Condition the sample at 120 C for 2 h.\n"
            "2. Equilibrate at the stated conditions.\n"
            "3. Record the mass every 10 min for 4 h."
        ),
        "inputs": SAMPLES,
        "allowed_actions": ("run_experiment", "record"),
        "parameter_targets": dict(CONDITIONS),
        "allowed_ranges": {"temperature_c": "20..30"},
        "allowed_substitutions": ("ethanol -> methanol",),
        "required_outputs": ("mass_uptake.csv", "instrument_log.txt"),
        "resource_limits": {"bench_hours": "6"},
        "execution_requirements": {"lab": "bench-chemistry"},
    }
    fields.update(overrides)
    return ExecutionContract(**fields).freeze()  # type: ignore[arg-type]


def a_context(
    root: Path,
    *,
    contract: ExecutionContract | None = None,
    acceptance: AcceptanceTerms | None = CRITERIA,
    supplied: bool = False,
) -> PreparationContext:
    """Everything the materializer is given, and nothing it may go looking for."""
    return PreparationContext(
        project_id="proj-a",
        node_id="n1",
        node_display_id="N-1A2B3C4D",
        contract=contract if contract is not None else a_contract(),
        workspace_root=root,
        inputs=(PreparedInput(name=SAMPLES[0], data=CERTIFICATE, source="artifact_version:av-1"),)
        if supplied
        else (),
        acceptance=acceptance,
    )


def built(root: Path, **overrides: Any) -> Path:
    """Run the materializer and return the directory it wrote into."""
    LabMaterializer().materialize(a_context(root, **overrides))
    return root


def read_json(root: Path, name: str) -> dict[str, Any]:
    return dict(json.loads((root / name).read_text()))


def read_rows(root: Path, name: str) -> list[list[str]]:
    """A CSV file as rows, parsed rather than split.

    Parsed, because half of what these tests are about is that the writer
    quotes what needs quoting: a `.split(",")` here would agree with a broken
    writer about a broken file.
    """
    return [row for row in csv.reader(io.StringIO((root / name).read_text()))]


def refused(root: Path, **overrides: Any) -> MaterializationRefused:
    """The refusal a contract produces, asserting that it produces one."""
    with pytest.raises(MaterializationRefused) as raised:
        LabMaterializer().materialize(a_context(root, **overrides))
    return raised.value


# ── What it builds ─────────────────────────────────────────────────────────


def test_the_package_holds_each_file_a_bench_is_given(tmp_path: Path) -> None:
    """The eight, with the roles that say what each is for.

    Named individually against the plan's list, because the failure this
    prevents is a package that is missing one of them: a bench with no reagent
    list cannot check what it has been given, and a run that discovers the gap
    is a run that stops halfway through an experiment.

    One sample and no stop conditions, so that this is the package *without*
    any of the conditional files — those are tested on their own below.
    """
    contract = a_contract(inputs=(SAMPLES[0],))
    prepared_execution = LabMaterializer().materialize(
        a_context(tmp_path / "ws", contract=contract)
    )

    assert [(file.name, file.role.value) for file in prepared_execution.files] == [
        ("experimental_protocol.md", "INPUT"),
        ("sample_manifest.csv", "INPUT"),
        ("reagent_list.csv", "INPUT"),
        ("equipment_requirements.json", "INPUT"),
        ("measurement_plan.json", "INPUT"),
        ("data_collection_template.csv", "TEMPLATE"),
        ("acceptance_checklist.json", "INPUT"),
        ("lab_manifest.json", "MANIFEST"),
    ]
    for file in prepared_execution.files:
        assert (tmp_path / "ws" / file.name).is_file(), file.name


def test_the_protocol_is_the_contracts_own_terms_written_out(tmp_path: Path) -> None:
    """Every term appears, and each is the contract's text.

    The positive half of the rule below: a protocol that dropped the permitted
    substitutions would have a bench substituting what it liked, and one that
    dropped the stop conditions would have a bench carrying on past them.
    """
    contract = a_contract(
        stop_conditions=("the sample discolours",),
        escalation_conditions=("the balance reads outside its calibration",),
    )
    text = (built(tmp_path / "ws", contract=contract) / "experimental_protocol.md").read_text()

    assert contract.procedure.strip() in text
    assert contract.objective in text
    assert "batch-17 powder" in text  # the inputs, as named
    assert "temperature_c" in text and "25" in text
    assert "20..30" in text  # the permitted window, beside its parameter
    assert "ethanol may be replaced with methanol" in text
    assert "mass_uptake.csv" in text and "instrument_log.txt" in text
    assert "the sample discolours" in text
    assert "the balance reads outside its calibration" in text
    # The criteria are in the protocol as well as the checklist: the person
    # doing the work is told what the work is for, not only what to write down.
    assert "within 10% of the simulated value" in text
    assert "acceptance contract ac-1 version 2" in text.lower()


def test_a_section_with_nothing_in_it_says_so_rather_than_going_missing(
    tmp_path: Path,
) -> None:
    """An empty section reads as an omission; a stated absence does not.

    A contract that states no stop conditions is a fact about the contract, and
    the person reading the protocol needs it written down as a fact — a blank
    space is a reader wondering whether the file was truncated.
    """
    text = (built(tmp_path / "ws") / "experimental_protocol.md").read_text()

    assert "*The contract states no stop conditions.*" in text
    assert "*The contract states no conditions under which to ask.*" in text
    assert "The contract permits no substitution." not in text  # one is permitted
    assert "ethanol may be replaced with methanol" in text


def test_the_sample_manifest_has_one_row_per_sample_named(tmp_path: Path) -> None:
    """In the contract's order, with the label the bench writes on the vial."""
    rows = read_rows(built(tmp_path / "ws"), "sample_manifest.csv")

    assert rows[0] == ["sample_id", "sample_as_named_by_the_contract", "note"]
    assert [row[0] for row in rows[1:]] == ["S-1", "S-2"]
    assert [row[1] for row in rows[1:]] == list(SAMPLES)


def test_a_name_with_a_comma_in_it_does_not_break_the_sheet(tmp_path: Path) -> None:
    """The reason these files are written through the `csv` module.

    A sample named `"batch-17, ground"` is an ordinary thing to call a sample,
    and a writer that joined with commas would shift every column after it —
    at the one place the file is used, which is beside the balance.
    """
    contract = a_contract(inputs=("batch-17, ground", "reference alumina"))
    rows = read_rows(built(tmp_path / "ws", contract=contract), "sample_manifest.csv")

    assert rows[1][1] == "batch-17, ground"
    assert rows[1][2] == ""  # the comma did not become a column


def test_the_data_collection_template_has_a_row_for_every_required_output(
    tmp_path: Path,
) -> None:
    """The sheet a bench fills in and the delivery that is judged are one list.

    A template missing a row is an output nobody records, and the failure
    surfaces at Review — after the experiment has been done and cannot be
    repeated from the record.
    """
    rows = read_rows(built(tmp_path / "ws"), "data_collection_template.csv")

    assert rows[0] == ["measurement", "value", "unit", "note"]
    assert [row[0] for row in rows[1:]] == ["mass_uptake.csv", "instrument_log.txt"]
    assert all(row[1:] == ["", "", ""] for row in rows[1:])


def test_the_reagent_list_is_the_substitutions_the_contract_permits(
    tmp_path: Path,
) -> None:
    """The contract's only reagent vocabulary, and it is stated not inferred."""
    contract = a_contract(allowed_substitutions=("ethanol -> methanol", "NaOH -> KOH"))
    rows = read_rows(built(tmp_path / "ws", contract=contract), "reagent_list.csv")

    assert rows[0] == ["reagent", "permitted_substitute", "note"]
    assert [(row[0], row[1]) for row in rows[1:]] == [
        ("ethanol", "methanol"),
        ("NaOH", "KOH"),
    ]


def test_the_checklist_quotes_the_frozen_criteria_and_prejudges_nothing(
    tmp_path: Path,
) -> None:
    """Each criterion with its provenance, and a verdict nobody has written.

    A checklist that arrived with `met` filled in would be RAVEL judging a
    delivery that has not happened, and the one thing Review may not be is
    pre-empted — by a preparation layer, of all things.
    """
    checklist = read_json(built(tmp_path / "ws"), "acceptance_checklist.json")

    assert checklist["acceptance_contract_ref"] == "ac-1"
    assert checklist["acceptance_contract_version"] == 2
    first, second = checklist["criteria"]
    assert first["criterion_id"] == "c-1"
    assert first["statement"] == "The measured uptake is within 10% of the simulated value."
    assert first["metric"] == "uptake"
    assert first["threshold"] == "within 10%"
    assert first["provenance"] == "literature_derived"
    assert first["provenance_ref"] == "10.1000/example"
    assert second["provenance"] == "user_requirement"
    assert all(criterion["met"] == "" for criterion in checklist["criteria"])
    assert all(criterion["observation"] == "" for criterion in checklist["criteria"])


def test_the_manifest_names_the_criteria_the_package_was_built_against(
    tmp_path: Path,
) -> None:
    """So that a package can be traced to the document it was measured by.

    Two versions of the same node's criteria produce two different checklists,
    and without the reference in the manifest there is nothing to say which one
    a bench was working from.
    """
    contract = a_contract()
    manifest = read_json(built(tmp_path / "ws", contract=contract), "lab_manifest.json")

    assert manifest["materializer"] == "bench-chemistry"
    assert manifest["materializer_version"] == "1"
    assert manifest["execution_contract_ref"] == contract.contract_id
    assert manifest["execution_contract_version"] == contract.version
    assert manifest["acceptance_contract_ref"] == "ac-1"
    assert manifest["acceptance_contract_version"] == 2
    assert manifest["environment"] == "lab"
    assert manifest["samples"] == list(SAMPLES)
    assert manifest["required_outputs"] == ["mass_uptake.csv", "instrument_log.txt"]
    assert manifest["method"].startswith("1. Condition the sample")
    assert manifest["parameters"] == CONDITIONS
    assert manifest["resource_request"] == {"bench_hours": "6"}
    # The manifest lists every file but itself — a document cannot carry the
    # digest of the version of itself that lists it.
    names = [entry["name"] for entry in manifest["generated_files"]]
    assert "lab_manifest.json" not in names
    assert "experimental_protocol.md" in names
    assert (tmp_path / "ws" / "lab_manifest.json").is_file()


def test_every_written_file_is_named_by_the_digest_of_what_landed(
    tmp_path: Path,
) -> None:
    """The digests are of the bytes on disk, not of what was meant to be written."""
    root = built(tmp_path / "ws")
    manifest = read_json(root, "lab_manifest.json")

    for entry in manifest["generated_files"]:
        data = (root / entry["name"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest(), entry["name"]
        assert entry["size_bytes"] == len(data), entry["name"]


def test_the_same_contract_writes_the_same_bytes(tmp_path: Path) -> None:
    """A second preparation of one contract produces one package, not two.

    What makes that true is that nothing here reads a clock or a random source
    while writing a content file: the manifest records when it was generated and
    is excluded for exactly that reason, and everything else has to be a
    function of the terms.

    *One* contract, prepared into two directories — a contract built twice is
    two contracts, since the identifier is minted at construction, and comparing
    two of those would be asserting that two different documents produce the
    same files.
    """
    contract = a_contract()
    first = built(tmp_path / "first", contract=contract)
    second = built(tmp_path / "second", contract=contract)

    for name in (
        "experimental_protocol.md",
        "sample_manifest.csv",
        "reagent_list.csv",
        "equipment_requirements.json",
        "measurement_plan.json",
        "data_collection_template.csv",
        "acceptance_checklist.json",
    ):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_an_input_that_resolved_to_bytes_recorded_which_version_it_was(
    tmp_path: Path,
) -> None:
    """And one that did not is recorded as a name, not as a gap.

    A laboratory contract usually names physical things, so an entry with no
    source is the ordinary case rather than a missing file. Where an input *did*
    resolve, the version is what makes the package traceable past "a file
    called this existed".
    """
    manifest = read_json(built(tmp_path / "ws", supplied=True), "lab_manifest.json")

    assert [entry["name"] for entry in manifest["inputs"]] == list(SAMPLES)
    resolved, physical = manifest["inputs"]
    assert resolved["source"] == "artifact_version:av-1"
    assert resolved["sha256"] == hashlib.sha256(CERTIFICATE).hexdigest()
    assert resolved["size_bytes"] == len(CERTIFICATE)
    assert physical["source"] == ""
    assert "sha256" not in physical


def test_the_execution_metadata_says_where_a_person_starts(tmp_path: Path) -> None:
    """The Worker reads this to know what it is about to hand over."""
    prepared_execution = LabMaterializer().materialize(a_context(tmp_path / "ws"))

    assert prepared_execution.execution_metadata["entrypoint"] == "experimental_protocol.md"
    assert prepared_execution.execution_metadata["template"] == "data_collection_template.csv"
    assert prepared_execution.execution_metadata["environment"] == "lab"
    assert prepared_execution.required_outputs == ("mass_uptake.csv", "instrument_log.txt")
    assert all(check.passed for check in prepared_execution.checks)


# ── The optional three ─────────────────────────────────────────────────────


def test_the_optional_files_are_absent_when_nothing_calls_for_them(
    tmp_path: Path,
) -> None:
    """One sample, no stop conditions, no instrument named: a package of eight."""
    root = built(tmp_path / "ws", contract=a_contract(inputs=("batch-17 powder",)))

    assert not (root / "safety_notes.md").exists()
    assert not (root / "instrument_settings.json").exists()
    assert not (root / "sample_label_template.csv").exists()
    assert read_json(root, "lab_manifest.json")["optional_files"] == []


def test_the_optional_files_appear_when_a_stated_fact_calls_for_them(
    tmp_path: Path,
) -> None:
    """And the manifest says which ones this contract called for.

    Stated as `optional_files` rather than left to a directory listing, so that
    "this contract needed no safety notes" and "the file was lost" are different
    observations.
    """
    contract = a_contract(
        stop_conditions=("the sample discolours",),
        resource_limits={"bench_hours": "6", "instrument": "TGA-2"},
    )
    root = built(tmp_path / "ws", contract=contract)

    assert (root / "safety_notes.md").is_file()
    assert (root / "instrument_settings.json").is_file()
    assert (root / "sample_label_template.csv").is_file()
    assert read_json(root, "lab_manifest.json")["optional_files"] == [
        "safety_notes.md",
        "instrument_settings.json",
        "sample_label_template.csv",
    ]


def test_the_safety_notes_say_what_they_are_not(tmp_path: Path) -> None:
    """A file called safety notes that reads as a risk assessment is worse than none.

    RAVEL holds no hazard information and cannot obtain any. It may state the
    contract's own stop conditions where a person will read them; it may not
    appear to have judged that a procedure is safe, and the first thing the
    file says is that it has not.
    """
    contract = a_contract(stop_conditions=("the sample discolours",))
    text = (built(tmp_path / "ws", contract=contract) / "safety_notes.md").read_text()

    assert "This is not a risk assessment" in text
    assert "The hazard information for this" in text
    assert "the sample discolours" in text


def test_the_instrument_settings_carry_the_contracts_conditions_and_no_others(
    tmp_path: Path,
) -> None:
    """The settings file is the contract's conditions plus the instrument it named."""
    contract = a_contract(resource_limits={"bench_hours": "6", "instrument": "TGA-2"})
    settings = read_json(built(tmp_path / "ws", contract=contract), "instrument_settings.json")

    assert settings["instrument"] == "TGA-2"
    assert settings["conditions"] == CONDITIONS
    assert settings["settings"] == "", "RAVEL supplied a setting the contract did not state"
    assert settings["limits"] == {"bench_hours": "6", "instrument": "TGA-2"}


def test_a_label_sheet_exists_only_when_there_is_more_than_one_thing_to_keep_apart(
    tmp_path: Path,
) -> None:
    """Two samples and one row each, keyed to the ids the manifest uses."""
    contract = a_contract(inputs=SAMPLES)
    rows = read_rows(built(tmp_path / "ws2", contract=contract), "sample_label_template.csv")

    assert rows[0] == ["sample_id", "label", "printed_name", "note"]
    assert [(row[0], row[2]) for row in rows[1:]] == [
        ("S-1", "batch-17 powder"),
        ("S-2", "reference alumina"),
    ]
    assert all(row[1] == "" for row in rows[1:])  # the label is for a person to write


# ── What it refuses ────────────────────────────────────────────────────────


def test_a_contract_with_no_procedure_is_refused(tmp_path: Path) -> None:
    """The member that exists to stop a guess being made.

    A procedure is what a protocol *is*. A materializer that wrote one from the
    objective would be choosing a method, which is Master's to choose.
    """
    error = refused(tmp_path / "ws", contract=a_contract(procedure="   "))

    assert error.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert "states no procedure" in error.reason
    assert "scientific decision" in error.reason
    assert not (tmp_path / "ws").exists() or list((tmp_path / "ws").iterdir()) == []


def test_a_contract_that_names_no_sample_is_refused(tmp_path: Path) -> None:
    """Everything here is done *to* something, and which thing is a term."""
    error = refused(tmp_path / "ws", contract=a_contract(inputs=()))

    assert error.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert "names no inputs" in error.reason


def test_a_contract_with_no_conditions_is_refused(tmp_path: Path) -> None:
    """An experiment at unstated conditions is not one that can be reproduced."""
    error = refused(tmp_path / "ws", contract=a_contract(parameter_targets={}))

    assert error.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert "parameter_targets is empty" in error.reason
    assert "scientific choice" in error.reason


def test_a_contract_with_no_required_outputs_is_refused(tmp_path: Path) -> None:
    """A recording sheet with no rows is a blank page."""
    error = refused(tmp_path / "ws", contract=a_contract(required_outputs=()))

    assert error.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert "requires no outputs" in error.reason


def test_a_node_with_no_frozen_criteria_is_refused(tmp_path: Path) -> None:
    """A checklist cannot be written from criteria nobody froze.

    Review measures the delivery against the frozen criteria and nothing else,
    so a package that stated a checklist without them would be telling a bench
    it will be judged by something RAVEL made up at the bench.
    """
    error = refused(tmp_path / "ws", acceptance=None)

    assert error.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert "no frozen acceptance criteria" in error.reason


def test_criteria_that_were_handed_over_empty_are_refused_too(tmp_path: Path) -> None:
    """The same refusal for an empty set, because it is the same missing term."""
    error = refused(
        tmp_path / "ws",
        acceptance=AcceptanceTerms(contract_id="ac-1", version=2, criteria=()),
    )

    assert error.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert "no frozen acceptance criteria" in error.reason


def test_nothing_is_written_when_a_contract_is_refused(tmp_path: Path) -> None:
    """The refusal is checked before the first file, not after it.

    A refused contract that left a half-written package behind would be a
    directory a backend could hand to a bench, and the one thing a refusal
    promises is that nothing was built.
    """
    with pytest.raises(MaterializationRefused):
        LabMaterializer().materialize(a_context(tmp_path / "ws", acceptance=None))

    assert not (tmp_path / "ws").exists()
