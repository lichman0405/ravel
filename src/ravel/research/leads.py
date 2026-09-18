"""What a search returns, and what opening it produces.

These two types are the whole reason the Evidence Ledger can be trusted.

A `Lead` is what a search engine, a metadata API, or a model's suggestion
gives you: a pointer, with no guarantee that anything is behind it. It carries
a snippet, and a snippet is precisely the thing that must never become
evidence — search engines synthesize them, model summaries invent them, and
neither is a source. There is no method on `Lead` that produces evidence, and
no constructor of `EvidenceSource` accepts one.

A `Retrieval` is the record of actually opening something: the URL that finally
answered after redirects, the status code, the media type, the moment it was
read, and the hash of the bytes. It is the only input from which the gateway
will register a source. When the bytes could not be obtained, the `Retrieval`
says so through `access_status` and carries no hash and no body — a restricted
source is a fact about the source, and RAVEL records it rather than guessing
past it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from ravel.domain.base import Record
from ravel.domain.enums import AccessStatus


class Lead(Record):
    """A pointer to something that might be a source. Not evidence.

    Kept deliberately thin. A lead is what a search gave back, and enriching it
    here — fetching the page, resolving the DOI, filling in the abstract — would
    blur the line this type exists to draw. Opening a lead is a separate,
    recorded act that produces a `Retrieval`.
    """

    url: str = Field(min_length=1)
    title: str = ""
    snippet: str = ""
    #: Which connector or provider produced this lead. Recorded so that a
    #: surprising result can be traced to the thing that returned it.
    provider: str = ""
    #: The provider's own ranking, when it gives one. Not comparable across
    #: providers, and not a quality signal.
    rank: int | None = None
    published: str | None = None
    #: Identifiers the provider supplied, such as a DOI. A DOI is carried
    #: through only when the provider's own record contains it; RAVEL never
    #: constructs one from a title.
    identifiers: dict[str, str] = Field(default_factory=dict)

    @property
    def declared_doi(self) -> str | None:
        """The DOI the provider's record carries, if it carried one.

        Named for what it is rather than for what it resembles. RAVEL does not
        parse a DOI out of a URL: `10.1234/...` appearing in a path is a string
        that looks like one, and a source registered under an identifier its
        publisher never issued is worse than a source with no identifier.
        """
        return self.identifiers.get("doi") or None


class Retrieval(Record):
    """One attempt to open a URL, and what actually came back.

    `requested_url` and `final_url` are both kept: a redirect chain is how a
    DOI resolves to a publisher, and a source that answered at a different URL
    than the one asked for is worth being able to see afterwards.
    """

    requested_url: str = Field(min_length=1)
    final_url: str = Field(min_length=1)
    access_status: AccessStatus
    retrieved_at: datetime
    status_code: int | None = None
    media_type: str | None = None
    content_hash: str | None = None
    size_bytes: int | None = None
    title: str = ""
    #: The bytes, when they were obtained and the caller asked to keep them.
    #: Large bodies are streamed to the artifact store instead of being held
    #: here, so this is often None even for a successful retrieval.
    body: bytes | None = None
    #: A short, verbatim excerpt of what was read — never a summary, never
    #: reconstructed. Empty when nothing was read.
    excerpt: str = ""
    #: Why access failed, in the words of the response. Empty on success.
    note: str = ""

    @model_validator(mode="after")
    def _content_only_when_read(self) -> Retrieval:
        """Only a source that was actually read may carry content.

        This mirrors the check on `EvidenceSource`, and it is the same rule:
        a hash or a body on a `PAYWALLED` retrieval would assert that RAVEL
        read bytes it could not reach. Enforcing it at the boundary means a
        connector cannot pass fabricated content along by mistake.
        """
        if self.access_status is not AccessStatus.OK:
            if self.content_hash is not None:
                raise ValueError(
                    f"{self.final_url} is {self.access_status.value} but carries a "
                    "content_hash; nothing was read, so there is nothing to hash"
                )
            if self.body is not None:
                raise ValueError(
                    f"{self.final_url} is {self.access_status.value} but carries a body; "
                    "restricted content was not read, so there are no bytes"
                )
            if self.excerpt:
                raise ValueError(
                    f"{self.final_url} is {self.access_status.value} but carries an "
                    "excerpt; a restricted source's text was not read"
                )
        return self

    @property
    def was_read(self) -> bool:
        """Whether the content was actually obtained."""
        return self.access_status is AccessStatus.OK

    @property
    def is_document(self) -> bool:
        """Whether what came back is a document rather than a redirect or an error page."""
        return self.was_read and bool(self.content_hash)
