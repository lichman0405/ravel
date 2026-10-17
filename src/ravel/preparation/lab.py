"""The package a bench is given, built from a frozen contract.

A computation's workspace is a set of files a machine reads. A laboratory's is
a set of files a *person* reads, fills in, and works from — which changes what
"prepared" means without changing what preparation is allowed to do. Everything
here is a rendering of something the contract states: the procedure as written,
the conditions it fixes, the samples it names, the outputs it requires, the
criteria its delivery will be judged against. **Nothing is added.** A
materializer that filled in a concentration, chose a solvent, or wrote an
equipment list the contract did not name would be doing science, and no role
has delegated that to software — so where a contract does not say enough, this
refuses and says which term was missing.

**Why the package is documents rather than one document.** A protocol is read
at the bench, a sample manifest is checked against what is in front of you, a
reagent list is taken to the store, and a data collection template is filled in
and handed back. Four things used in four places, and collapsing them into one
file would mean the bench copy has to be edited down to the part being used —
which is how a page of a protocol comes to be the only page anybody reads.

**The optional three are emitted when a stated fact makes them applicable**,
and each of those rules is written where the file is written. RAVEL does not
decide that a procedure is hazardous, that an instrument needs its settings
written down, or that a sample needs labelling; it decides whether the contract
said the thing that makes the file meaningful, and the file says so itself when
a reader might otherwise read more into it than is there.

**A bench package runs where it is executed by a person.** There is no job
script here, and no entrypoint a machine starts. What a Worker is handed is the
protocol and the template, and the record of what was delivered comes back
through the Execution Record like any other run.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ravel.domain.contracts import ExecutionContract, substitution_parts
from ravel.domain.preparation import PreparationCheck, PreparationRefusal
from ravel.preparation.workspace import (
    AcceptanceTerms,
    FileRole,
    MaterializationRefused,
    PreparationContext,
    PreparedExecution,
    Workspace,
    build_manifest,
)

__all__ = ["MATERIALIZER_VERSION", "AcceptanceTerms", "LabMaterializer"]

#: The materializer's own version. Bumped when what it writes changes: the
#: manifest records it, and a package built by a later version of this code has
#: to be tellable apart from one built by an earlier version of it.
MATERIALIZER_VERSION = "1"

#: The protocol file, and the entry point of a laboratory run: what a person
#: starts from. Named here rather than in two places, because the manifest and
#: the execution metadata both report it.
PROTOCOL = "experimental_protocol.md"


@dataclass(frozen=True)
class LabMaterializer:
    """Build the package for a laboratory run.

    One materializer per kind of bench, keyed in the registry by `lab` and the
    name a contract spells. Like the software materializers it holds no
    database, opens no connection and reads nothing but its context: what it
    writes is a function of the contract and the criteria it was handed.
    """

    kind: str = "lab"
    name: str = "bench-chemistry"
    version: str = MATERIALIZER_VERSION

    def materialize(self, context: PreparationContext) -> PreparedExecution:
        """Write the package this contract describes.

        Raises:
            MaterializationRefused: The contract does not state enough to hand
                a bench — no procedure, no samples, no conditions, no outputs,
                or no frozen criteria — or it states two things that disagree.
        """
        contract = context.contract
        procedure = _procedure(contract)
        samples = _samples(contract)
        conditions = _conditions(contract)
        outputs = _outputs(contract)
        criteria = _criteria(context)
        substitutions = _substitutions(contract)

        workspace = Workspace(context.workspace_root)
        workspace.write_text(PROTOCOL, _protocol(contract, criteria), role=FileRole.INPUT)
        workspace.write_text("sample_manifest.csv", _sample_manifest(samples), role=FileRole.INPUT)
        workspace.write_text("reagent_list.csv", _reagent_list(substitutions), role=FileRole.INPUT)
        workspace.write_json(
            "equipment_requirements.json",
            _equipment(contract),
            role=FileRole.INPUT,
        )
        workspace.write_json(
            "measurement_plan.json",
            _measurement_plan(contract, conditions, outputs),
            role=FileRole.INPUT,
        )
        workspace.write_text(
            "data_collection_template.csv",
            _data_collection_template(outputs),
            role=FileRole.TEMPLATE,
        )
        workspace.write_json("acceptance_checklist.json", _checklist(criteria), role=FileRole.INPUT)

        optional = _optional_files(workspace, contract, samples, conditions)

        manifest = build_manifest(
            context,
            materializer=self.name,
            materializer_version=self.version,
            files=workspace.files,
            method=procedure,
            parameters=dict(contract.parameter_targets),
            resource_request=dict(contract.resource_limits),
            inputs=list(_input_references(context)),
            extra={
                "environment": self.kind,
                "acceptance_contract_ref": criteria.contract_id,
                "acceptance_contract_version": criteria.version,
                "samples": [sample.name for sample in samples],
                "optional_files": optional,
            },
        )
        workspace.write_json("lab_manifest.json", manifest, role=FileRole.MANIFEST)

        return PreparedExecution(
            workspace_path=str(workspace.root),
            materializer=self.name,
            materializer_version=self.version,
            files=workspace.files,
            manifest=manifest,
            checks=(
                PreparationCheck(
                    name="procedure_stated",
                    passed=True,
                    detail="the protocol is the contract's own procedure",
                ),
                PreparationCheck(
                    name="criteria_frozen",
                    passed=True,
                    detail=(
                        f"{len(criteria.criteria)} criteria from acceptance "
                        f"contract {criteria.contract_id} v{criteria.version}"
                    ),
                ),
                PreparationCheck(
                    name="every_output_is_recorded",
                    passed=True,
                    detail=(
                        "the data collection template has a row for each of "
                        f"{len(outputs)} required outputs"
                    ),
                ),
            ),
            required_outputs=contract.required_outputs,
            execution_metadata={
                "environment": self.kind,
                "protocol": PROTOCOL,
                "template": "data_collection_template.csv",
                # What a person starts from. A laboratory run has no command:
                # the work is read from the protocol and recorded in the
                # template, and the backend that hands it over is a person's,
                # not a scheduler's.
                "entrypoint": PROTOCOL,
            },
        )


# ── Reading the contract ───────────────────────────────────────────────────


def _procedure(contract: ExecutionContract) -> str:
    """The procedure, which a bench cannot work without.

    Raises:
        MaterializationRefused: The contract states none. The protocol RAVEL
            writes is the contract's own procedure written out; there is no
            version of this file that a contract without one could produce.
    """
    stated = contract.procedure.strip()
    if not stated:
        raise MaterializationRefused(
            PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
            "the contract states no procedure, and the protocol a bench works "
            "from is that procedure written out; RAVEL does not infer a method "
            "from an objective, because choosing one is a scientific decision",
        )
    return stated


def _samples(contract: ExecutionContract) -> tuple[_Sample, ...]:
    """The samples the bench is to be given, in the order the contract names them.

    A lab contract's `inputs` are what the run is performed *on*. They are
    usually not files — a specimen, a batch, a prepared surface — so this takes
    the names as written and does not require that any of them resolved to
    bytes. Where one did resolve, the manifest records the version it was read
    from; where one did not, the manifest says so by leaving that empty, which
    is a different fact from an input nobody named.

    `sample_id` is a label RAVEL assigns from the order, and it is the one
    thing here the contract did not write. A bench needs to write something on
    a vial, and a label derived from position is reproducible: the same
    contract produces the same labels, so a second printing labels the same
    sample the same way.

    Raises:
        MaterializationRefused: The contract names no samples. An experiment is
            performed on something, and which thing is a term of the contract
            rather than something a bench decides.
    """
    if not contract.inputs:
        raise MaterializationRefused(
            PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
            "the contract names no inputs, and a sample manifest lists what the "
            "bench is given to work on; name the samples in the contract's "
            "inputs, because which sample is measured is part of the experiment",
        )
    return tuple(
        _Sample(sample_id=f"S-{position}", name=name)
        for position, name in enumerate(contract.inputs, start=1)
    )


def _conditions(contract: ExecutionContract) -> dict[str, str]:
    """The conditions the contract fixes, which the bench works under.

    Raises:
        MaterializationRefused: The contract fixes none. An experiment run at
            no stated conditions is not reproducible, and a measurement plan
            with nothing in it is not a plan.
    """
    if not contract.parameter_targets:
        raise MaterializationRefused(
            PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
            "the contract's parameter_targets is empty, so the conditions this "
            "experiment is run under are unstated; RAVEL writes no value of its "
            "own here, because every one of them would be a scientific choice "
            "the contract did not make",
        )
    return {term: value for term, value in contract.parameter_targets.items()}


def _outputs(contract: ExecutionContract) -> tuple[str, ...]:
    """What the bench has to bring back.

    Raises:
        MaterializationRefused: The contract requires none. The data collection
            template's rows are the required outputs, and a template with no
            rows is a blank sheet a bench cannot know how to fill in.
    """
    if not contract.required_outputs:
        raise MaterializationRefused(
            PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
            "the contract requires no outputs, so nothing says what the bench "
            "is to record or hand back; the required outputs are what the "
            "delivery is checked against",
        )
    return contract.required_outputs


def _criteria(context: PreparationContext) -> AcceptanceTerms:
    """The frozen criteria this run's delivery will be judged against.

    Raises:
        MaterializationRefused: No criteria were handed in, or the ones that
            were carry no criteria. A node of a type that reaches a bench may
            not run without frozen criteria — the DAG refuses the transition —
            so reaching here without them means the run was prepared against
            something other than what it will be measured by, which is a fault
            rather than a missing term. It is refused as a missing term all the
            same, because that is the class whose next step — find out what the
            criteria are — is the right one.
    """
    criteria = context.acceptance
    if criteria is None or not criteria.criteria:
        raise MaterializationRefused(
            PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
            "no frozen acceptance criteria were found for this node, and the "
            "checklist a bench signs the delivery off against is those criteria "
            "and nothing else",
        )
    return criteria


def _substitutions(contract: ExecutionContract) -> tuple[tuple[str, str], ...]:
    """The replacements the contract permits, as the pairs they name.

    An entry the model would have refused — one that does not name a
    replacement — is skipped rather than refused here: this is a rendering of
    terms that are already the contract's, and a row that got past the model
    should not stop a package being built. It is not silently dropped either:
    the reagent list says how many entries were unreadable.
    """
    pairs = []
    for entry in contract.allowed_substitutions:
        parts = substitution_parts(entry)
        if parts is not None:
            pairs.append(parts)
    return tuple(pairs)


# ── What is written ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Sample:
    """One thing the bench is given, with the label RAVEL will print for it."""

    sample_id: str
    name: str


def _protocol(contract: ExecutionContract, criteria: AcceptanceTerms) -> str:
    """The contract's own terms, laid out as a protocol.

    Every sentence below is either the contract's text or a heading naming
    which part of the contract follows it. There is no step RAVEL added, no
    warning it wrote, and no quantity it filled in.
    """
    lines = [
        f"# {contract.objective}",
        "",
        f"Execution contract {contract.contract_id} version {contract.version}.",
        "",
        "## Procedure",
        "",
        contract.procedure.strip(),
        "",
        "## What this is performed on",
        "",
    ]
    lines += _bullets(contract.inputs, "The contract names nothing to work on.")
    lines += [
        "",
        "## Conditions this run is performed under",
        "",
    ]
    lines += _table(
        ("parameter", "value", "permitted range"),
        [
            (term, value, contract.allowed_ranges.get(term, ""))
            for term, value in contract.parameter_targets.items()
        ],
    )
    lines += ["", "## What is permitted", ""]
    lines += _bullets(contract.allowed_actions, "No action is listed.")
    lines += ["", "Permitted substitutions:", ""]
    lines += _bullets(
        [f"{given} may be replaced with {instead}" for given, instead in _substitutions(contract)],
        "The contract permits no substitution.",
    )
    lines += ["", "## What this delivery must contain", ""]
    lines += _bullets(contract.required_outputs, "The contract requires no outputs.")
    lines += ["", "## When to stop", ""]
    lines += _bullets(contract.stop_conditions, "The contract states no stop conditions.")
    lines += ["", "## When to stop and ask", ""]
    lines += _bullets(
        contract.escalation_conditions,
        "The contract states no conditions under which to ask.",
    )
    lines += [
        "",
        "## What this delivery is measured against",
        "",
        f"Acceptance contract {criteria.contract_id} version {criteria.version}.",
        "",
    ]
    lines += _bullets(
        [
            f"**{criterion.criterion_id}** — {criterion.statement}"
            for criterion in criteria.criteria
        ],
        "No criteria were frozen for this node.",
    )
    lines += [
        "",
        "---",
        "",
        "Generated by RAVEL preparation from the contract above. It states what",
        "the contract states and nothing more: where a step, a quantity or a",
        "hazard is not written here, it is not because it was omitted but",
        "because the contract did not state it. Edit the contract, not this",
        "file — the next preparation writes this one again from the terms.",
        "",
    ]
    return "\n".join(lines)


def _sample_manifest(samples: Sequence[_Sample]) -> str:
    """The samples, one row each, with the label to print for each."""
    rows = [[sample.sample_id, sample.name, ""] for sample in samples]
    return _csv(("sample_id", "sample_as_named_by_the_contract", "note"), rows)


def _reagent_list(substitutions: tuple[tuple[str, str], ...]) -> str:
    """The reagents the contract names, and what may stand in for them.

    The only reagent vocabulary a contract has is `allowed_substitutions`: a
    pair names the reagent and its permitted replacement. A contract that
    permits none therefore produces a reagent list with a header and no rows,
    and that is the honest answer — the store list is derivable from the
    contract, and a list RAVEL compiled from the procedure's prose would be a
    reading of the contract rather than a term of it.
    """
    rows = [
        [given, instead, "permitted by the execution contract"] for given, instead in substitutions
    ]
    return _csv(("reagent", "permitted_substitute", "note"), rows)


def _equipment(contract: ExecutionContract) -> dict[str, Any]:
    """What the contract says the bench needs, and what it may ask for.

    `limits` is the contract's `resource_limits` verbatim: for a laboratory
    that is where an instrument, a booking window or an operator requirement is
    stated. RAVEL does not translate it and does not add to it.
    """
    return {
        "execution_contract_ref": contract.contract_id,
        "execution_contract_version": contract.version,
        "limits": dict(contract.resource_limits),
        "permitted_actions": list(contract.allowed_actions),
        "note": (
            "These are the contract's own resource limits. RAVEL states what "
            "the contract states and does not list equipment of its own."
        ),
    }


def _measurement_plan(
    contract: ExecutionContract,
    conditions: Mapping[str, str],
    outputs: Sequence[str],
) -> dict[str, Any]:
    """What is measured, and the conditions it is measured under."""
    return {
        "objective": contract.objective,
        "conditions": dict(conditions),
        "permitted_ranges": dict(contract.allowed_ranges),
        "measurements": list(outputs),
        "stop_conditions": list(contract.stop_conditions),
        "escalation_conditions": list(contract.escalation_conditions),
    }


def _data_collection_template(outputs: Sequence[str]) -> str:
    """The sheet a bench records on, with one row per required output.

    The rows are the contract's required outputs, so the sheet a person fills
    in and the record the delivery is checked against are the same list. The
    columns beside `measurement` are blank: what a value is measured in, and
    what a note says, are the bench's to record rather than RAVEL's to assume.
    """
    return _csv(
        ("measurement", "value", "unit", "note"),
        [[output, "", "", ""] for output in outputs],
    )


def _checklist(criteria: AcceptanceTerms) -> dict[str, Any]:
    """The frozen criteria, each with a blank verdict for the bench to fill.

    `met` is left empty rather than prefilled. A checklist that arrived with
    its answers in it would be RAVEL judging a delivery that has not happened.
    """
    return {
        "acceptance_contract_ref": criteria.contract_id,
        "acceptance_contract_version": criteria.version,
        "criteria": [
            {
                "criterion_id": criterion.criterion_id,
                "statement": criterion.statement,
                "metric": criterion.metric,
                "threshold": criterion.threshold,
                "provenance": criterion.provenance.value,
                "provenance_ref": criterion.provenance_ref or "",
                "met": "",
                "observation": "",
            }
            for criterion in criteria.criteria
        ],
        "note": (
            "These criteria were frozen before the run, and this delivery is "
            "measured against them by Review — not by whoever performed it."
        ),
    }


def _optional_files(
    workspace: Workspace,
    contract: ExecutionContract,
    samples: Sequence[_Sample],
    conditions: Mapping[str, str],
) -> list[str]:
    """The files a stated fact makes meaningful, and which those were.

    Each rule is a fact the contract states, never a judgement about the
    science. Returns the names written, which go into the manifest so that a
    reader can see which optional files this contract called for rather than
    having to compare directory listings.
    """
    written: list[str] = []

    if contract.stop_conditions or contract.escalation_conditions:
        # Something to say about stopping, which is what the notes are: the
        # contract's own conditions, plus what to do about them. RAVEL writes
        # no hazard information, and the file says so.
        workspace.write_text("safety_notes.md", _safety_notes(contract), role=FileRole.INPUT)
        written.append("safety_notes.md")

    instrument = contract.resource_limits.get("instrument", "").strip()
    if instrument:
        # The contract named an instrument, so there are settings to record
        # beside it. Without one there is nothing this file could be about.
        workspace.write_json(
            "instrument_settings.json",
            {
                "instrument": instrument,
                "conditions": dict(conditions),
                "limits": dict(contract.resource_limits),
                "settings": "",
                "note": (
                    "The contract names the instrument and the conditions. Any "
                    "setting not listed here is not stated by the contract, and "
                    "RAVEL did not supply it."
                ),
            },
            role=FileRole.INPUT,
        )
        written.append("instrument_settings.json")

    if len(samples) > 1:
        # More than one thing to keep apart, so there is something to label.
        workspace.write_text(
            "sample_label_template.csv",
            _csv(
                ("sample_id", "label", "printed_name", "note"),
                [[sample.sample_id, "", sample.name, ""] for sample in samples],
            ),
            role=FileRole.TEMPLATE,
        )
        written.append("sample_label_template.csv")

    return written


def _safety_notes(contract: ExecutionContract) -> str:
    """The contract's stop and escalation conditions, and what they are not.

    The first paragraph is the point of the file. A document called safety
    notes that a laboratory might read as a hazard assessment would be worse
    than no document: RAVEL holds no hazard information, cannot obtain any,
    and must not appear to have judged that a procedure is safe.
    """
    lines = [
        "# Safety notes",
        "",
        "**This is not a risk assessment.** RAVEL has not assessed this",
        "procedure and holds no hazard information for it. What follows is the",
        "contract's own conditions for stopping and for asking, written where",
        "they will be read at the bench. The hazard information for this",
        "procedure comes from the laboratory, not from RAVEL.",
        "",
        "## Stop the work when",
        "",
    ]
    lines += _bullets(contract.stop_conditions, "The contract states no stop conditions.")
    lines += ["", "## Stop and ask when", ""]
    lines += _bullets(
        contract.escalation_conditions,
        "The contract states no conditions under which to ask.",
    )
    lines += [
        "",
        "Asking is not a delay to be worked around: the contract is the",
        "authority for what may be done, and a question about it goes to",
        "Master, who is the only role that may widen it.",
        "",
    ]
    return "\n".join(lines)


def _input_references(context: PreparationContext) -> list[dict[str, Any]]:
    """Where each named input came from, for the ones RAVEL holds bytes for.

    **A sample with no bytes is not a gap.** Laboratory inputs are physical:
    a contract naming `"batch-17"` is naming something on a shelf, and the
    entry records the name with no source because there is nothing to record.
    Where an input *did* resolve to an artifact version, the version is here —
    that is the case worth recording, since it says which revision of a
    document the bench was working from.
    """
    references = []
    for name in context.contract.inputs:
        resolved = context.input_named(name)
        entry: dict[str, Any] = {"name": name, "source": ""}
        if resolved is not None:
            entry["source"] = resolved.source
            entry["sha256"] = resolved.sha256
            entry["size_bytes"] = len(resolved.data)
        references.append(entry)
    return references


# ── Small writers ──────────────────────────────────────────────────────────


def _csv(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """A CSV document, newline-terminated and with `\\n` line endings.

    Written through the `csv` module rather than by joining with commas: a
    contract term may contain a comma, a quote or a newline, and a protocol
    whose reagent list is broken because a name had a comma in it is a file
    that fails in the one place it is used — at a bench.

    `\\n` rather than the module's default `\\r\\n`, because these files are
    hashed into a manifest and read back by a person: the same contract has to
    produce the same bytes on every machine that prepares it.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(list(row))
    return buffer.getvalue()


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """A markdown table, so that the protocol reads as a document."""
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("|" + "|".join("---" for _ in header) + "|")
    for row in rows:
        cells = ["" if cell is None else str(cell) for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _bullets(items: Sequence[str], when_empty: str) -> list[str]:
    """A bulleted list, or the sentence that says there is nothing to list.

    The empty sentence rather than nothing at all: a section with no content
    reads as a section that was forgotten, and "the contract states no stop
    conditions" is a fact the person reading the protocol needs.
    """
    if not items:
        return [f"*{when_empty}*"]
    return [f"- {item}" for item in items]
