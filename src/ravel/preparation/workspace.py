"""The directory a prepared run happens in, and the files RAVEL puts in it.

A materializer does one thing: it reads a frozen contract and writes a
directory. This module is the writing half — the thing every materializer uses
rather than reimplements, because the properties that matter here are the ones
that would be got wrong once and then copied:

- **Every file is hashed as it is written**, and the hash goes in the manifest.
  A result is traceable to its inputs only if the inputs are named by content
  rather than by path, and a path is not evidence: the file at
  `simulation.input` tomorrow is not the file the run read yesterday.
- **A name that could escape the workspace is refused**, by the same rule
  `commit_terms` applies to a required output. A materializer writes names that
  came out of a contract, and a contract is written by a model.
- **A symlink at the target is refused rather than followed**, because a plain
  file name is not on its own a promise that the bytes land in this directory.

The manifest is deliberately *not* one of the files it lists. Its own hash
cannot be inside it — the document would have to contain a digest of itself —
and a manifest that listed itself with the hash of some earlier version would
be a manifest that disagrees with the directory it describes. What it does list
is everything else, with the role each file plays, which is what lets a reader
tell a contract's inputs from the job script RAVEL generated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ravel.domain.artifacts import unusable_filename_reason
from ravel.domain.clock import utcnow
from ravel.domain.contracts import AcceptanceCriterion, ExecutionContract
from ravel.domain.preparation import PreparationCheck, PreparationRefusal

__all__ = [
    "AcceptanceTerms",
    "FileRole",
    "MaterializationRefused",
    "PreparationContext",
    "PreparedExecution",
    "PreparedFile",
    "PreparedInput",
    "Workspace",
    "build_manifest",
]


class FileRole(StrEnum):
    """What a prepared file is for, from the closed list.

    Recorded per file because the question a reader asks about a workspace is
    not only "what is in it" but "which of these came from the contract and
    which did RAVEL generate". A job script RAVEL wrote and an input the
    contract supplied are both files in one directory, and a run whose script
    turned out to be wrong is a different finding from one whose input was.
    """

    #: Supplied by, or named by, the contract: structures, molecule
    #: definitions, force fields, sample lists.
    INPUT = "INPUT"
    #: What starts the run — a scheduler script, a command list.
    JOB_SCRIPT = "JOB_SCRIPT"
    #: Something to be filled in rather than read: a recording sheet, a
    #: sample label sheet, a checklist a person completes at the bench.
    TEMPLATE = "TEMPLATE"
    #: The manifest itself.
    MANIFEST = "MANIFEST"


class MaterializationRefused(Exception):
    """A contract that could not be turned into a workspace.

    Raised rather than returned so that a materializer cannot forget to check:
    a refusal that was a return value would be a value some caller ignores, and
    what ignoring it produces is a run started in a directory that was never
    built. The exception carries the class of refusal as well as the sentence,
    because the class is what decides Master's next step.

    This is not an error in the ordinary sense. A contract RAVEL cannot
    materialize is a fact about the contract, and the caller's answer is to
    record the refusal and park the node where Master is asked —
    `WAITING_DECISION` — rather than to retry until something gives.
    """

    def __init__(self, refusal: PreparationRefusal, reason: str) -> None:
        if not isinstance(refusal, PreparationRefusal):
            raise TypeError(
                f"a refusal must be one of {[member.value for member in PreparationRefusal]}, "
                f"got {refusal!r}; the class decides which of Master's tools the "
                "situation calls for, so a free-text one cannot be routed"
            )
        if not reason.strip():
            raise ValueError(
                "a refusal must say why in words: Master reads the sentence, and "
                "the class alone does not say what was wrong with the contract"
            )
        super().__init__(reason)
        self.refusal = refusal
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PreparedFile:
    """One file in a workspace, named by its content.

    `sha256` is of the bytes as written, so two preparations of the same
    contract produce the same digests and a changed input is visible as a
    changed digest rather than as a file that is still there.
    """

    name: str
    role: FileRole
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PreparedInput:
    """Bytes a contract named as an input, and where RAVEL found them.

    The bytes travel with the name because a materializer has no database and
    no object store: resolving `"framework.cif"` to a file is the caller's
    job, and doing it there is what keeps a materializer a function of its
    arguments. A materializer that resolved inputs itself would be a second
    reader of authoritative state, free to read a different artifact version
    than the run it is preparing for.

    `source` is what the input was read from, in a form a person can follow —
    an artifact and version, a path — and it goes into the manifest, so a
    result is traceable past "some file called framework.cif existed".
    """

    name: str
    data: bytes
    source: str = ""

    @property
    def sha256(self) -> str:
        """The digest of the bytes as RAVEL read them."""
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True, slots=True)
class AcceptanceTerms:
    """The frozen criteria a delivery will be measured against.

    Handed to a materializer rather than looked up by it, for the same reason
    the contract is: a materializer that read the project would be a second
    reader of authoritative state, free to read a different version of the
    criteria than the one the delivery will be judged against. The lookup is
    the caller's — `NodeRunActivities.prepare_execution` — and it reads the
    contract the node is *bound* to, by reference.

    It lives here rather than in the materializer that first needed it, because
    it is part of what every materializer is given. A package that tells a
    bench what a delivery must contain and a package that tells a machine what
    to print are the same question asked of the same document, and a second
    kind of package would otherwise be handed the criteria in a second shape.

    `contract_id` and `version` travel with the criteria so that whatever is
    written from them can name the exact document it came from: a criterion
    quoted without its provenance is a threshold nobody can trace back.
    """

    contract_id: str
    version: int
    criteria: tuple[AcceptanceCriterion, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparationContext:
    """Everything a materializer is given, and nothing it may go looking for.

    The contract is the frozen one the run will execute under, passed whole
    rather than by reference: a materializer that read the project's state
    itself would be a second reader of authoritative state, and one that could
    prepare a contract version other than the one it was handed.

    `workspace_root` is decided by the caller — the runtime decides where
    prepared work lives, and a materializer does not get to choose — and the
    workspace is created under it.

    `inputs` are the bytes of what the contract named, resolved by the caller.
    A materializer reads them and puts the ones it needs into the workspace;
    an input the contract named but that is not here is one the caller could
    not find, and the materializer's answer is to say so rather than to write
    a workspace with a gap in it.

    `acceptance` is the frozen criteria for this node, when it has any. It is
    `None` for the many contracts that name no environment at all, and for a
    contract whose environment needs no more than the execution terms — but
    *not* for a node that will be judged: a materializer whose package has to
    state what the delivery is measured against treats its absence as a term
    the contract does not state, and refuses.
    """

    project_id: str
    node_id: str
    node_display_id: str
    contract: ExecutionContract
    workspace_root: Path
    inputs: tuple[PreparedInput, ...] = ()
    acceptance: AcceptanceTerms | None = None

    def input_named(self, name: str) -> PreparedInput | None:
        """The input this name resolves to, or `None` if nothing was resolved.

        Exact, and case-sensitive: file names are identifiers here, and two
        inputs differing only in case are two files on the filesystem this
        workspace is written to.
        """
        for supplied in self.inputs:
            if supplied.name == name:
                return supplied
        return None


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    """What a materializer built, ready to be recorded and run.

    `execution_metadata` is the part a Worker needs and the manifest does not
    carry: where the run is started from, which files it is expected to produce,
    the software and version. It is `str` to `str` because it is read by a
    Worker stating what it is about to do, not by a program branching on it.
    """

    workspace_path: str
    materializer: str
    materializer_version: str
    files: tuple[PreparedFile, ...]
    manifest: dict[str, Any]
    checks: tuple[PreparationCheck, ...] = ()
    required_outputs: tuple[str, ...] = ()
    execution_metadata: dict[str, str] = field(default_factory=dict)


class Workspace:
    """One node's prepared directory, written file by file.

    Instantiated with the root the caller chose. Files are written one at a
    time and each returns what was written, so a materializer accumulates the
    list it hands to `build_manifest` as a by-product of doing its work rather
    than by walking the directory afterwards — the difference being that a
    walk would also pick up whatever else happened to be there.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._files: list[PreparedFile] = []

    @property
    def files(self) -> tuple[PreparedFile, ...]:
        """Everything written so far, in the order it was written."""
        return tuple(self._files)

    def write_text(self, name: str, text: str, *, role: FileRole) -> PreparedFile:
        """Write a text file, UTF-8, with the newline the file ends in as given."""
        return self.write_bytes(name, text.encode("utf-8"), role=role)

    def write_json(
        self, name: str, document: Mapping[str, Any], *, role: FileRole
    ) -> PreparedFile:
        """Write a JSON document, indented so a person can read it.

        Sorted keys and a trailing newline, because these files are read by
        people at a bench as often as by programs, and a diff of two manifests
        is only useful if the same manifest always serialises the same way.
        """
        return self.write_text(
            name,
            json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
            role=role,
        )

    def write_bytes(self, name: str, data: bytes, *, role: FileRole) -> PreparedFile:
        """Write bytes into the workspace and remember their digest.

        Raises:
            ValueError: The name is not a file name, or something is already
                sitting at it that would send the write elsewhere.
        """
        target = self._target(name)
        target.write_bytes(data)
        written = PreparedFile(
            name=name,
            role=role,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
        self._files.append(written)
        return written

    def copy_from(self, name: str, source: Path, *, role: FileRole) -> PreparedFile:
        """Copy a file into the workspace, hashed as it lands.

        The bytes are read and written through `write_bytes` rather than
        `shutil.copy`, so that what is recorded is the digest of what RAVEL
        wrote and not of what was at the source a moment earlier.
        """
        return self.write_bytes(name, Path(source).read_bytes(), role=role)

    def _target(self, name: str) -> Path:
        """Where `name` lands, having established that nothing sends it elsewhere.

        Two ways a write could leave this directory, and they are the two checks
        below. The first is the shape of the name: a separator or a `..` would
        make `simulation.input` mean something other than a file in this
        directory, and the rule against both is `unusable_filename_reason`'s —
        the one `commit_terms` already applies to a required output, so a
        workspace cannot hold a file whose name a contract could not have asked
        for. The second is what is already at the target: a symlink there is
        followed by every ordinary write, so a name that is a plain file name
        would still put RAVEL's bytes outside the workspace. Nothing legitimate
        creates one — the workspace is written by RAVEL and read by a run — so
        finding one is refused rather than resolved.

        The workspace *root* is the caller's business: it comes from
        `Settings.runtime_path`, which resolves its components against the
        runtime root and refuses any that escape it.

        Raises:
            ValueError: The name is not a file name, or a symlink is in the way.
        """
        problem = unusable_filename_reason(name)
        if problem is not None:
            raise ValueError(f"{name!r} cannot name a prepared file: {problem}")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / name
        if target.is_symlink():
            raise ValueError(
                f"{name!r} is a symlink to {target.resolve()}, and a prepared "
                f"file is written into {self.root} rather than wherever a link "
                "points; remove it if this workspace is to be written again"
            )
        return target


def build_manifest(
    context: PreparationContext,
    *,
    materializer: str,
    materializer_version: str,
    files: tuple[PreparedFile, ...],
    method: str = "",
    parameters: Mapping[str, Any] | None = None,
    resource_request: Mapping[str, Any] | None = None,
    inputs: Sequence[Mapping[str, Any]] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The document that says what was built, from what, by which code.

    The keys are the plan's, and each is here because a reader needs it to
    answer a question the directory itself cannot: *which* contract version
    these files were built from (a node may run twice under revised terms),
    which method and parameters they encode (the same input file with a
    different temperature is a different experiment), which version of the
    materializer wrote them (a materializer is code, and code changes), and
    what the run is expected to produce.

    `inputs` is where the files came *from*, as against `generated_files`,
    which is what is in the directory now. The two answer different questions:
    a framework copied in appears in both, but only here does it say which
    artifact version those bytes were read out of — and a run whose framework
    was silently a different structure than the one the review approved is
    exactly the failure that reference exists to make visible.

    `extra` is where a materializer adds what is specific to its kind of
    environment — a computed simulation box, a sample count — without this
    function having to know about it.
    """
    document: dict[str, Any] = {
        "project_id": context.project_id,
        "node_id": context.node_id,
        "node_display_id": context.node_display_id,
        "objective": context.contract.objective,
        "execution_contract_ref": context.contract.contract_id,
        "execution_contract_version": context.contract.version,
        "materializer": materializer,
        "materializer_version": materializer_version,
        "method": method,
        "parameters": dict(parameters or {}),
        "resource_request": dict(resource_request or {}),
        "inputs": [dict(entry) for entry in inputs],
        "required_outputs": list(context.contract.required_outputs),
        "generated_files": [
            {
                "name": file.name,
                "role": file.role.value,
                "sha256": file.sha256,
                "size_bytes": file.size_bytes,
            }
            for file in files
        ],
        "generated_at": utcnow().isoformat(),
    }
    if extra:
        document.update(extra)
    return document
