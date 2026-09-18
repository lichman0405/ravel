"""What the gateway writes when a source becomes evidence.

The unit tests cover what it refuses. These cover what it records, against a
real PostgreSQL and a real MinIO, because the properties at stake are properties
of those two systems: an append-only table, a foreign key to a project, and an
object in a bucket whose bytes are what the recorded hash describes.

The test that earns its place here is the snapshot mismatch. It is a check the
live suite cannot provoke — a store that writes one thing and reports the hash
of another — and it is the check that makes `verify` meaningful. If a
registration could record a hash that does not describe the stored bytes, then
every later comparison would be comparing a number with itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import BinaryIO

import pytest
from sqlalchemy.orm import Session

from ravel.config import Settings
from ravel.domain.enums import AccessStatus, EvidenceSourceTier
from ravel.domain.project import Project
from ravel.research.gateway import (
    SNAPSHOT_KIND,
    ResearchSourceGateway,
    SourceRefused,
    SourceRequest,
)
from ravel.research.leads import Retrieval
from ravel.state.database import Database
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import ArtifactStore, S3ArtifactStore, StoredObject, hash_chunks

PAGE = b"""<html><head><title>Catalyst Stability Under Load</title></head>
<body><p>The catalyst retained 91 percent of its activity after 500 hours.</p>
</body></html>"""

URL = "https://pubs.acs.org/doi/10.1021/example"


@pytest.fixture
def session(database: Database) -> Iterator[Session]:
    with database.transaction() as session:
        yield session


def _gateway(
    session: Session, project_id: str, settings: Settings, store: ArtifactStore
) -> ResearchSourceGateway:
    """A gateway for one project, with the address guard answered without DNS.

    Every hostname here resolves to a public address so that what these tests
    exercise is the writing and not the network policy, which has its own file.
    """
    return ResearchSourceGateway(
        session=session,
        project_id=project_id,
        settings=settings,
        store=store,
        resolver=lambda host: ["93.184.216.34"],
    )


@pytest.fixture
def gateway(
    session: Session,
    project: Project,
    artifact_store: S3ArtifactStore,
    integration_settings: Settings,
) -> ResearchSourceGateway:
    return _gateway(session, project.project_id, integration_settings, artifact_store)


def _read(url: str = URL, body: bytes = PAGE) -> Retrieval:
    digest, _ = hash_chunks([body])
    return Retrieval(
        requested_url=url,
        final_url=url,
        access_status=AccessStatus.OK,
        retrieved_at=datetime.now(UTC),
        content_hash=digest,
        size_bytes=len(body),
        media_type="text/html",
        title="Catalyst Stability Under Load",
        body=body,
        excerpt="The catalyst retained 91 percent of its activity after 500 hours.",
    )


def _restricted(url: str = URL) -> Retrieval:
    return Retrieval(
        requested_url=url,
        final_url=url,
        access_status=AccessStatus.PAYWALLED,
        retrieved_at=datetime.now(UTC),
        status_code=402,
        media_type="text/html",
        note="HTTP 402: payment required",
    )


class CorruptingStore:
    """A store that uploads the bytes and reports the hash of different ones.

    Not a hypothetical: the failure this models is any path where what reached
    storage is not what was read — a truncated upload, a transcoding proxy, a
    bug in a chunked writer. The gateway compares the two hashes, and this is
    what makes that comparison testable on demand rather than only in
    production.

    Every other method is forwarded explicitly rather than through
    `__getattr__`, so that this satisfies `ArtifactStore` as far as a type
    checker is concerned: a delegating wrapper that the checker cannot verify is
    a wrapper that can silently stop matching the interface it claims to be.
    """

    def __init__(self, inner: ArtifactStore) -> None:
        self._inner = inner

    def put(self, key: str, chunks: Iterable[bytes], *, media_type: str = "") -> StoredObject:
        stored = self._inner.put(key, chunks, media_type=media_type)
        wrong, _ = hash_chunks([b"different bytes entirely"])
        return StoredObject(key=stored.key, content_hash=wrong, size_bytes=stored.size_bytes)

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def open(self, key: str) -> BinaryIO:
        return self._inner.open(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def verify(self, key: str, expected_hash: str) -> bool:
        return self._inner.verify(key, expected_hash)

    def delete(self, key: str) -> None:
        self._inner.delete(key)


def test_a_read_source_is_registered_with_a_snapshot_that_hashes_the_stored_bytes(
    gateway: ResearchSourceGateway, project: Project, session: Session
) -> None:
    """The hash describes an object in the bucket, not a number computed in passing.

    That is the difference between a hash that can be checked later and one that
    can only be believed.
    """
    registration = gateway.register(SourceRequest.of(URL), _read(), actor_id="agent")

    assert registration.was_read
    assert registration.source.access_status is AccessStatus.OK
    assert registration.source.content_hash == registration.retrieval.content_hash
    assert registration.snapshot_ref

    stored = gateway.store.get(registration.snapshot_ref)  # type: ignore[union-attr]
    assert stored == PAGE, "the object holds the bytes that were read"
    digest, _ = hash_chunks([stored])
    assert digest == registration.source.content_hash


def test_a_snapshot_is_an_artifact_with_its_own_provenance(
    gateway: ResearchSourceGateway, project: Project, session: Session
) -> None:
    """The stored copy is addressable on its own, and says where it came from.

    An artifact that only existed as a string in `snapshot_ref` would be bytes
    in a bucket with nothing recording which source they are a copy of.
    """
    registration = gateway.register(SourceRequest.of(URL), _read(), actor_id="agent")

    artifacts = ArtifactRepository(session, project.project_id, gateway.store)  # type: ignore[arg-type]
    artifact = artifacts.get(artifact_id=registration.source.artifact_ref)
    (version,) = artifacts.versions(artifact.artifact_id)

    assert artifact.kind == SNAPSHOT_KIND
    assert artifact.provenance == URL
    assert artifact.media_type == "text/html"
    assert version.version == 1
    assert version.storage_key == registration.snapshot_ref
    assert version.content_hash == registration.source.content_hash


def test_a_source_whose_snapshot_does_not_match_what_was_read_is_refused(
    session: Session,
    project: Project,
    artifact_store: S3ArtifactStore,
    integration_settings: Settings,
) -> None:
    """The check that makes `verify` more than a comparison with itself.

    Every registration compares the hash the store reports with the hash the
    reading produced. When they differ, something between the read and the write
    changed the bytes — and a row recording the reading's hash would describe
    an object that is not in the bucket.
    """
    gateway = _gateway(
        session, project.project_id, integration_settings, CorruptingStore(artifact_store)
    )

    with pytest.raises(SourceRefused) as raised:
        gateway.register(SourceRequest.of(URL), _read(), actor_id="agent")

    assert "the stored bytes are not the read bytes" in str(raised.value)


def test_a_restricted_source_is_recorded_as_restricted_and_not_as_empty(
    gateway: ResearchSourceGateway, project: Project, session: Session
) -> None:
    """ "RAVEL could not read this" is a finding, and it keeps its reason.

    The failure this prevents is the plausible one: a paywalled source stored as
    a source with no content, which reads downstream as a source that said
    nothing rather than one that was never read.
    """
    registration = gateway.register(
        SourceRequest.of(URL, title="Catalyst Stability Under Load"),
        _restricted(),
        actor_id="agent",
    )

    source = registration.source
    assert source.access_status is AccessStatus.PAYWALLED
    assert source.content_hash is None
    assert source.artifact_ref is None
    assert source.snapshot_ref is None
    assert not source.was_read
    # The title the provider supplied survives, and the notes say where it came
    # from, so a citation built on this row is not dressed up as a reading.
    assert source.title == "Catalyst Stability Under Load"
    assert "title from the lead" in source.notes


def test_the_tier_and_its_rule_are_written_into_the_row(
    gateway: ResearchSourceGateway, project: Project, session: Session
) -> None:
    """A tier with no reason is a number someone has to take on faith."""
    registration = gateway.register(
        SourceRequest.of(URL, declared_type="component"), _read(), actor_id="agent"
    )

    assert registration.tier.tier is EvidenceSourceTier.B
    assert "tier B (crossref:component)" in registration.source.notes
    assert "supporting information" in registration.source.notes


def test_registering_without_a_snapshot_records_the_retrievals_own_hash(
    gateway: ResearchSourceGateway, project: Project
) -> None:
    """The store is optional, and its absence is visible in the row.

    With no snapshot there is nothing in the bucket to point at, so the hash is
    the one the reading computed. The row says so by having no `snapshot_ref`,
    which is how a later reader can tell which kind of hash it is looking at.
    """
    registration = gateway.register(
        SourceRequest.of(URL), _read(), actor_id="agent", snapshot=False
    )

    assert registration.source.content_hash == _read().content_hash
    assert registration.snapshot_ref is None
    assert registration.source.artifact_ref is None


def test_a_gateway_cannot_write_into_another_project(
    session: Session,
    project: Project,
    other_project: Project,
    artifact_store: S3ArtifactStore,
    integration_settings: Settings,
) -> None:
    """The scope comes from the gateway, so no caller supplies a project id.

    The repository would refuse a row belonging to another project anyway; the
    shape here means the question never arises.
    """
    gateway = _gateway(
        session, other_project.project_id, integration_settings, artifact_store
    )

    registration = gateway.register(SourceRequest.of(URL), _read(), actor_id="agent")

    assert registration.source.project_id == other_project.project_id
    assert gateway.sources.all() != []
    # Nothing landed in the first project.
    assert gateway.sources.all()[0].project_id != project.project_id


def test_verify_confirms_a_source_that_still_says_what_was_recorded(
    gateway: ResearchSourceGateway, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`verify` re-reads the source and compares it with the row.

    The fetcher is replaced with one that returns the same bytes, because the
    question here is what `verify` does with an answer, not whether the internet
    is still serving the page. Whether the real fetch path works is settled by
    the live suite.
    """
    registration = gateway.register(SourceRequest.of(URL), _read(), actor_id="agent")

    class SameBytes:
        def get(self, url: str) -> Retrieval:
            return _read(url)

    monkeypatch.setattr(ResearchSourceGateway, "fetcher_for", property(lambda self: SameBytes()))

    verification = gateway.verify(registration.source)

    assert verification.matches
    assert verification.recorded_hash == verification.observed_hash
    assert "unchanged" in verification.detail


def test_verify_reports_a_source_whose_bytes_have_changed(
    gateway: ResearchSourceGateway, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed source is reported rather than corrected.

    The record of what was read at the time stands; a later reader decides what
    a change means. Silently updating the row would destroy the only evidence
    that the source ever said anything else.
    """
    registration = gateway.register(SourceRequest.of(URL), _read(), actor_id="agent")
    changed = b"<html><title>Catalyst Stability Under Load</title><p>Retracted.</p></html>"

    class Changed:
        def get(self, url: str) -> Retrieval:
            return _read(url, changed)

    monkeypatch.setattr(ResearchSourceGateway, "fetcher_for", property(lambda self: Changed()))

    verification = gateway.verify(registration.source)

    assert not verification.matches
    assert verification.recorded_hash != verification.observed_hash
    assert "has changed" in verification.detail


def test_verify_of_a_restricted_source_compares_the_refusal_it_got(
    gateway: ResearchSourceGateway, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no hash to compare, so the access status is compared instead.

    "Still behind the same paywall" is the strongest statement available about
    something RAVEL was never able to read, and the detail says which comparison
    was made rather than leaving it to be inferred.
    """
    registration = gateway.register(SourceRequest.of(URL), _restricted(), actor_id="agent")

    class StillPaywalled:
        def get(self, url: str) -> Retrieval:
            return _restricted(url)

    monkeypatch.setattr(
        ResearchSourceGateway, "fetcher_for", property(lambda self: StillPaywalled())
    )

    verification = gateway.verify(registration.source)

    assert verification.matches
    assert verification.access_status is AccessStatus.PAYWALLED
    assert "no content hash" in verification.detail


def test_verify_reports_a_source_that_has_become_unreachable(
    gateway: ResearchSourceGateway, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that no longer answers is a result of the check, not a fault in it."""
    registration = gateway.register(SourceRequest.of(URL), _read(), actor_id="agent")

    class Unreachable:
        def get(self, url: str) -> Retrieval:
            raise OSError("network is unreachable")

    monkeypatch.setattr(ResearchSourceGateway, "fetcher_for", property(lambda self: Unreachable()))

    verification = gateway.verify(registration.source)

    assert not verification.matches
    assert "could not be re-opened" in verification.detail
    assert verification.recorded_hash == registration.source.content_hash
