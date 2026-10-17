"""What the writing half of preparation guarantees.

Three properties, and each is here because the failures they prevent are quiet:
a file whose hash was never taken is a result that cannot be traced to its
input; a name that escapes the workspace is a write somewhere RAVEL does not
own; and a refusal that was swallowed is a run started in a directory that was
never built. None of the three would be visible in a passing run.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ravel.domain.contracts import ExecutionContract
from ravel.domain.preparation import PreparationCheck, PreparationRefusal
from ravel.preparation import (
    FileRole,
    MaterializationRefused,
    PreparationContext,
    PreparedExecution,
    Workspace,
    build_manifest,
)


def a_contract(**overrides: object) -> ExecutionContract:
    """A frozen contract for a node that runs a calculation."""
    fields: dict[str, object] = {
        "project_id": "proj-a",
        "node_id": "n1",
        "objective": "Simulate the adsorption isotherm.",
        "required_outputs": ("results.txt",),
        "parameter_targets": {"temperature_k": "298"},
        "execution_requirements": {"software": "raspa"},
    }
    fields.update(overrides)
    return ExecutionContract(**fields).freeze()  # type: ignore[arg-type]


def a_context(root: Path, **overrides: object) -> PreparationContext:
    return PreparationContext(
        project_id="proj-a",
        node_id="n1",
        node_display_id="N-1A2B3C4D",
        contract=a_contract(**overrides),
        workspace_root=root,
    )


def test_a_written_file_is_named_by_its_content(tmp_path: Path) -> None:
    """The hash is of the bytes as written, not of what was at the source."""
    workspace = Workspace(tmp_path / "ws")
    written = workspace.write_text(
        "simulation.input", "SimulationType MonteCarlo\n", role=FileRole.INPUT
    )
    body = b"SimulationType MonteCarlo\n"
    assert written.sha256 == hashlib.sha256(body).hexdigest()
    assert written.size_bytes == len(body)
    assert written.role is FileRole.INPUT
    assert (tmp_path / "ws" / "simulation.input").read_bytes() == body


def test_the_workspace_lists_what_it_wrote_in_order(tmp_path: Path) -> None:
    """Not by walking the directory afterwards, which would also find files
    that were already there."""
    workspace = Workspace(tmp_path / "ws")
    (tmp_path / "ws").mkdir(parents=True)
    (tmp_path / "ws" / "left-behind.txt").write_text("not mine")
    workspace.write_text("a.txt", "a", role=FileRole.INPUT)
    workspace.write_json("manifest.json", {"a": 1}, role=FileRole.MANIFEST)
    assert [file.name for file in workspace.files] == ["a.txt", "manifest.json"]


def test_json_is_written_the_same_way_every_time(tmp_path: Path) -> None:
    """So that a diff of two manifests says what changed and nothing else."""
    workspace = Workspace(tmp_path / "ws")
    first = workspace.write_json(
        "one.json", {"b": 2, "a": {"d": 4, "c": 3}}, role=FileRole.MANIFEST
    )
    second = workspace.write_json(
        "two.json", {"a": {"c": 3, "d": 4}, "b": 2}, role=FileRole.MANIFEST
    )
    assert first.sha256 == second.sha256


@pytest.mark.parametrize(
    "name",
    ["../escape.txt", "sub/dir.txt", "..", "", "back\\slash.txt"],
)
def test_a_name_that_is_not_a_file_name_is_refused(tmp_path: Path, name: str) -> None:
    """A materializer writes names a model wrote, so the name is checked here.

    The rule is `unusable_filename_reason`'s — the same one `commit_terms`
    applies to a required output — so a workspace cannot contain a file the
    contract could not have asked for.
    """
    workspace = Workspace(tmp_path / "ws")
    with pytest.raises(ValueError, match="cannot name a prepared file"):
        workspace.write_text(name, "x", role=FileRole.INPUT)
    assert not (tmp_path / "escape.txt").exists()
    assert workspace.files == ()


def test_a_symlink_where_a_file_belongs_cannot_send_the_write_elsewhere(
    tmp_path: Path,
) -> None:
    """A plain file name is not on its own a promise about where bytes land.

    `simulation.input` passes the name rule — no separator, not `.` or `..` —
    and an ordinary write to it still goes wherever the link points. So the
    target is looked at before it is written, and a workspace that has grown a
    symlink is refused rather than written through.
    """
    outside = tmp_path / "outside.txt"
    outside.write_text("not RAVEL's to overwrite")
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    (workspace_root / "simulation.input").symlink_to(outside)

    workspace = Workspace(workspace_root)
    with pytest.raises(ValueError, match="is a symlink to"):
        workspace.write_text("simulation.input", "SimulationType MonteCarlo\n", role=FileRole.INPUT)

    assert outside.read_text() == "not RAVEL's to overwrite"
    assert workspace.files == ()


def test_a_refusal_carries_the_class_that_routes_it() -> None:
    """The class decides Master's next step, so it cannot be a free sentence."""
    refused = MaterializationRefused(
        PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
        "the contract names no temperature",
    )
    assert refused.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert "temperature" in str(refused)


def test_a_refusal_must_be_one_of_the_known_classes() -> None:
    with pytest.raises(TypeError) as refused:
        MaterializationRefused("MISSING_PARAMETER", "x")  # type: ignore[arg-type]
    assert "MISSING_SCIENTIFIC_PARAMETER" in str(refused.value)


def test_a_refusal_must_say_why() -> None:
    with pytest.raises(ValueError, match="why in words"):
        MaterializationRefused(PreparationRefusal.INCONSISTENT_CONTRACT, "  ")


def test_the_manifest_names_the_contract_the_files_were_built_from(
    tmp_path: Path,
) -> None:
    """The whole point of the document: a workspace is traceable or it is a
    directory of files nobody can attribute."""
    context = a_context(tmp_path / "ws")
    workspace = Workspace(tmp_path / "ws")
    written = workspace.write_text("simulation.input", "x", role=FileRole.INPUT)
    manifest = build_manifest(
        context,
        materializer="raspa",
        materializer_version="1",
        files=workspace.files,
        method="GCMC",
        parameters={"temperature_k": "298"},
    )

    assert manifest["project_id"] == "proj-a"
    assert manifest["node_id"] == "n1"
    assert manifest["execution_contract_ref"] == context.contract.contract_id
    assert manifest["execution_contract_version"] == context.contract.version
    assert manifest["materializer"] == "raspa"
    assert manifest["materializer_version"] == "1"
    assert manifest["method"] == "GCMC"
    assert manifest["parameters"] == {"temperature_k": "298"}
    assert manifest["required_outputs"] == ["results.txt"]
    assert manifest["generated_files"] == [
        {
            "name": "simulation.input",
            "role": "INPUT",
            "sha256": written.sha256,
            "size_bytes": 1,
        }
    ]
    assert manifest["generated_at"]


def test_the_manifest_does_not_list_itself(tmp_path: Path) -> None:
    """A document cannot contain its own digest, and one that listed an older
    one would disagree with the directory it describes."""
    context = a_context(tmp_path / "ws")
    workspace = Workspace(tmp_path / "ws")
    workspace.write_text("simulation.input", "x", role=FileRole.INPUT)
    document = build_manifest(
        context, materializer="raspa", materializer_version="1", files=workspace.files
    )
    workspace.write_json("calculation_manifest.json", document, role=FileRole.MANIFEST)
    assert [file.name for file in workspace.files] == [
        "simulation.input",
        "calculation_manifest.json",
    ]
    reread = (tmp_path / "ws" / "calculation_manifest.json").read_text()
    assert "calculation_manifest.json" not in reread


def test_a_materializer_puts_what_is_its_own_beside_what_every_manifest_has(
    tmp_path: Path,
) -> None:
    """`extra` is where a kind of environment adds what only it knows."""
    context = a_context(tmp_path / "ws")
    manifest = build_manifest(
        context,
        materializer="bench-chemistry",
        materializer_version="1",
        files=(),
        extra={"samples": ["S-1", "S-2"]},
    )
    assert manifest["samples"] == ["S-1", "S-2"]
    assert manifest["objective"] == "Simulate the adsorption isotherm."


def test_a_prepared_execution_carries_checks_and_metadata(tmp_path: Path) -> None:
    """The result a materializer hands back holds everything the record needs."""
    workspace = Workspace(tmp_path / "ws")
    written = workspace.write_text("job.slurm", "#!/bin/bash\n", role=FileRole.JOB_SCRIPT)
    prepared = PreparedExecution(
        workspace_path=str(workspace.root),
        materializer="raspa",
        materializer_version="1",
        files=workspace.files,
        manifest={"node_id": "n1"},
        checks=(PreparationCheck(name="software_available", passed=True),),
        required_outputs=("results.txt",),
        execution_metadata={"software": "raspa", "entrypoint": "job.slurm"},
    )
    assert prepared.files == (written,)
    assert prepared.checks[0].passed
    assert prepared.execution_metadata["entrypoint"] == "job.slurm"
    assert prepared.required_outputs == ("results.txt",)
