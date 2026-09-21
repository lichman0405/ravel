"""Artifacts and their versions.

Artifact bytes never enter PostgreSQL. The database holds the metadata — hash,
size, media type, provenance — and the bytes live in S3-compatible storage
under a key derived from content, so the same bytes stored twice occupy one
object and an object's key cannot disagree with its contents.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.ids import DisplayPrefix, display_id, new_id

#: Where a version's bytes live: projects/{project}/{artifact}/{version}/{name}
ARTIFACT_KEY_TEMPLATE = "projects/{project_id}/{artifact_id}/{version}/{filename}"

#: The `kind` carried by every artifact a mock backend produced.
#:
#: Simulation is marked with a *kind* rather than with a naming convention or a
#: sentence in the provenance, because those are free text and this has to be
#: checkable. One value in one column means "was this simulated?" is a
#: comparison, and a guard that asks it cannot be talked out of the answer.
SIMULATED_KIND = "simulated"


def simulated_provenance(backend: str, scenario: str) -> str:
    """The provenance line for an artifact a mock produced.

    Free text, deliberately: it is what a person reads when they want to know
    which mock and which named scenario the bytes came from. `kind` remains the
    part that is checked.
    """
    return f"{backend}:{scenario}"


def is_simulated(kind: str) -> bool:
    """Whether an artifact's kind marks its contents as simulated.

    Every guard asks this rather than comparing against `SIMULATED_KIND`
    directly, so a second such kind would be a change in one place.
    """
    return kind == SIMULATED_KIND


def unusable_filename_reason(filename: str) -> str | None:
    """Why `filename` cannot name an artifact, or `None` if it can.

    Stated once here because two callers a long way apart have to agree on it:
    `artifact_key` asks when bytes are stored, and a node's terms ask when the
    task is planned. A name one accepts and the other rejects is a task that
    runs and then cannot deliver, and the Worker that runs it can do nothing
    about it — a live run failed an activity five retries deep on an output
    whose name was a sentence containing a slash.

    The rule is exactly what a storage key needs of a name, and no more. A
    tighter one — no spaces, ASCII only, nothing a header would have to escape —
    would be this function inventing a restriction the store does not have, and
    it would refuse names that are perfectly good files; the surfaces that paste
    a name into a header already answer for that themselves.
    """
    if not filename.strip():
        return "it is empty"
    if "/" in filename or "\\" in filename:
        return "it contains a path separator"
    if filename in {".", ".."}:
        return "it names a directory rather than a file"
    return None


def artifact_key(project_id: str, artifact_id: str, version: int, filename: str) -> str:
    """The storage key for one artifact version.

    Raises:
        ValueError: A component would escape its prefix in the key, or the
            filename is not one — see `unusable_filename_reason`.
    """
    for name, value in (
        ("project_id", project_id),
        ("artifact_id", artifact_id),
    ):
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError(f"{name}={value!r} is not usable in a storage key")
    problem = unusable_filename_reason(filename)
    if problem is not None:
        raise ValueError(f"filename={filename!r} is not usable in a storage key: {problem}")
    if version < 1:
        raise ValueError("artifact version starts at 1")
    return ARTIFACT_KEY_TEMPLATE.format(
        project_id=project_id, artifact_id=artifact_id, version=version, filename=filename
    )


class ArtifactVersion(Record):
    """One immutable set of bytes.

    "Immutable" is enforced three ways: the record is frozen, the storage key
    contains the content hash's version counter, and the store refuses to
    overwrite an existing key. A changed file becomes a new version — there is
    no code path that replaces bytes in place.
    """

    version_id: str = Field(default_factory=new_id)
    artifact_id: str
    project_id: str
    version: int = Field(ge=1)
    storage_key: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    media_type: str = ""
    filename: str = ""
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    node_id: str | None = None
    execution_ref: str | None = None
    note: str = ""

    @property
    def sha256(self) -> str:
        """The digest part of the content hash, whichever algorithm prefixed it."""
        return self.content_hash.split(":", 1)[-1]


class Artifact(Record):
    """A logical artifact, whose versions accumulate.

    The artifact is the identity a node references; the versions are what it
    actually produced. A computation that was re-run with corrected inputs
    produces version 2 of the same artifact, and the node's reference does not
    have to change to point at the newer bytes.
    """

    artifact_id: str = Field(default_factory=new_id)
    display_id: str = Field(default_factory=lambda: display_id(DisplayPrefix.ARTIFACT))
    project_id: str
    name: str = Field(min_length=1)
    kind: str = ""
    media_type: str = ""
    provenance: str = ""
    node_id: str | None = None
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)


class ArtifactRegistration(Record):
    """An artifact together with the versions being registered for it.

    A convenience for the repository layer: it is the shape a caller has when it
    writes new bytes, and it keeps the "which version is this" question in one
    place instead of at every call site.
    """

    artifact: Artifact
    versions: tuple[ArtifactVersion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _versions_belong_to_the_artifact(self) -> ArtifactRegistration:
        for version in self.versions:
            if version.artifact_id != self.artifact.artifact_id:
                raise ValueError(
                    f"version {version.version} belongs to artifact "
                    f"{version.artifact_id}, not {self.artifact.artifact_id}"
                )
            if version.project_id != self.artifact.project_id:
                raise ValueError(
                    f"version {version.version} belongs to project "
                    f"{version.project_id}, not {self.artifact.project_id}"
                )
        numbers = [version.version for version in self.versions]
        if len(set(numbers)) != len(numbers):
            raise ValueError("two versions of the same artifact share a version number")
        return self

    @property
    def latest(self) -> ArtifactVersion:
        """The highest-numbered version in this registration."""
        return max(self.versions, key=lambda version: version.version)
