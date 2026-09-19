"""The cache that makes "nothing is registered that was not opened" mechanical.

`register_source` takes a reference rather than a URL and a hash, so the rule
`docs/05` states as a sequence — *open the original source, then register* — is
enforced by what this process happens to be holding rather than by what the
model says it did. These tests are about the two ways that could stop being
true: a reference that resolves to something other than the reading it named,
and a cache that grows without bound because nothing evicts.

The eviction tests are deliberately about *which* entry goes. An eviction
policy that dropped the newest would make a session that opens two sources and
registers the first one fail for a reason that has nothing to do with sources.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ravel.domain.enums import AccessStatus
from ravel.research.leads import Retrieval
from ravel.research.opened import DEFAULT_MAX_BYTES, DEFAULT_MAX_ITEMS, OpenedSources
from ravel.state.store import hash_chunks

BASE_TIME = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def _read(
    url: str = "https://example.org/paper", *, body: bytes = b"x" * 10, at: int = 0
) -> Retrieval:
    """One successful reading, as the fetcher would return it."""
    digest, _ = hash_chunks([body])
    return Retrieval(
        requested_url=url,
        final_url=url,
        access_status=AccessStatus.OK,
        retrieved_at=BASE_TIME + timedelta(seconds=at),
        content_hash=digest,
        size_bytes=len(body),
        media_type="text/html",
        body=body,
    )


def test_what_was_remembered_can_be_recalled_by_its_reference() -> None:
    opened = OpenedSources()
    retrieval = _read()

    reference = opened.remember(retrieval)

    assert opened.recall(reference) is retrieval
    assert opened.size == 1
    assert opened.bytes_held == 10


def test_a_reference_this_process_never_issued_resolves_to_nothing() -> None:
    """The refusal the tool turns into "open it again", rather than a guess."""
    assert OpenedSources().recall("ret-0123456789abcdef01234567") is None


def test_two_readings_of_one_url_at_two_moments_are_two_entries() -> None:
    """A second look is evidence about the source now, and is not collapsed.

    The reference is derived from when the source was read as well as from what
    came back, so a page that changed between two readings is two references —
    which is the fact the second reading exists to record.
    """
    opened = OpenedSources()

    first = opened.remember(_read(at=0))
    second = opened.remember(_read(at=60))

    assert first != second
    assert opened.size == 2


def test_the_same_reading_remembered_twice_is_one_entry() -> None:
    """References are stable, so an entry is not silently renumbered."""
    opened = OpenedSources()
    retrieval = _read()

    assert opened.remember(retrieval) == opened.remember(retrieval)
    assert opened.size == 1
    assert opened.bytes_held == 10, "the second remember must not double-count"


def test_forgetting_one_entry_gives_back_its_bytes() -> None:
    opened = OpenedSources()
    reference = opened.remember(_read(body=b"y" * 40))

    opened.forget(reference)

    assert opened.recall(reference) is None
    assert opened.size == 0
    assert opened.bytes_held == 0


def test_a_retrieval_with_no_body_is_held_at_no_cost() -> None:
    """A paywalled source is a finding worth registering and costs nothing.

    Counting its absent body as some default size would make a session that
    probed a hundred restricted sources evict the one paper it read.
    """
    restricted = Retrieval(
        requested_url="https://pubs.acs.org/doi/10.1021/x",
        final_url="https://pubs.acs.org/doi/10.1021/x",
        access_status=AccessStatus.PAYWALLED,
        retrieved_at=BASE_TIME,
    )

    opened = OpenedSources()
    opened.remember(restricted)

    assert opened.size == 1
    assert opened.bytes_held == 0


def test_the_byte_budget_evicts_the_oldest_first() -> None:
    opened = OpenedSources(max_bytes=25, max_items=100)

    oldest = opened.remember(_read(at=0, body=b"a" * 10))
    middle = opened.remember(_read(at=1, body=b"b" * 10))
    newest = opened.remember(_read(at=2, body=b"c" * 10))

    assert opened.recall(oldest) is None, "the oldest reading is the one to let go"
    assert opened.recall(middle) is not None
    assert opened.recall(newest) is not None
    assert opened.bytes_held == 20


def test_the_item_budget_evicts_even_when_nothing_is_large() -> None:
    """A run of abstract stubs would otherwise sit under the byte budget for ever."""
    opened = OpenedSources(max_bytes=1024, max_items=2)

    first = opened.remember(_read(at=0))
    second = opened.remember(_read(at=1))
    third = opened.remember(_read(at=2))

    assert opened.recall(first) is None
    assert opened.recall(second) is not None
    assert opened.recall(third) is not None
    assert opened.size == 2


def test_one_retrieval_larger_than_the_budget_is_held_rather_than_refused() -> None:
    """Eviction is of the oldest, and the newest is what the caller just opened.

    Dropping the entry a caller is about to register would turn a large PDF
    into a registration that cannot be made, for a reason the agent cannot see.
    """
    opened = OpenedSources(max_bytes=5, max_items=100)

    reference = opened.remember(_read(body=b"z" * 50))

    assert opened.recall(reference) is not None
    assert opened.size == 1


def test_clearing_drops_everything_and_its_bytes() -> None:
    opened = OpenedSources()
    opened.remember(_read(at=0))
    opened.remember(_read(at=1))

    opened.clear()

    assert opened.size == 0
    assert opened.bytes_held == 0


def test_the_default_budgets_are_the_documented_ones() -> None:
    """Stated so that a change to either is a change to a number somebody chose."""
    opened = OpenedSources()
    opened.remember(_read())

    assert DEFAULT_MAX_BYTES == 64 * 1024 * 1024
    assert DEFAULT_MAX_ITEMS == 256
    assert opened.size == 1
