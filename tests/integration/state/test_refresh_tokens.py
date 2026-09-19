"""Rotation, replay detection, and the append-only rule that makes both work.

The design claim under test is that a login is a *history*: exchanging a token
writes a second row naming the first as its parent, and "has this been used?"
is answered by whether a child exists rather than by a flag somebody set.

That matters because a flag is only as good as the write that sets it. A
process that dies between accepting a token and recording the acceptance
leaves a token that looks unused and is still valid — which is exactly the
state a replay wants. Two rows cannot be in that state: either the child was
written, or the exchange did not happen.

The append-only trigger is asserted here too. It is what keeps "a child exists"
meaningful, and it is the reason none of this needed a new guard: a table
absent from `UPDATABLE_TABLES` is refused by the database itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ravel.domain.clock import utcnow
from ravel.state.database import Database
from ravel.state.repositories.identity import UserRepository
from ravel.state.repositories.tokens import RefreshRefused, RefreshTokenRepository

pytestmark = pytest.mark.integration

TTL = timedelta(days=30)


@pytest.fixture(autouse=True)
def _clean_database(clean: None) -> None:
    """Start each case from an empty database.

    A grant chain is a history, so cases that assert on the shape of one have
    to be the only chain in the table. Usernames are unique, so without this
    the second case to register `rotator` fails on the constraint rather than
    on what it was testing.
    """


def _a_user(database: Database) -> str:
    with database.transaction() as session:
        return UserRepository(session).create(username="rotator").user_id


def _expiry() -> datetime:
    """A grant lifetime, as a caller would compute it from the settings."""
    return utcnow() + TTL


# ── Rotation ────────────────────────────────────────────────────────────────


def test_a_first_grant_opens_its_own_family(database: Database) -> None:
    """A grant with no parent starts a chain, and names it."""
    user_id = _a_user(database)
    with database.transaction() as session:
        first = RefreshTokenRepository(session).issue(
            user_id=user_id, token_hash="a" * 64, expires_at=_expiry()
        )

    assert first.parent_id is None
    assert first.family_id, "a first grant has to belong to some family"


def test_rotating_extends_the_chain_rather_than_editing_it(database: Database) -> None:
    """The first grant still reads exactly as it did, and a child points at it.

    This is the whole difference between a history and a row that moves. If
    rotation edited the first row, the fact that it was ever issued — with the
    expiry it was granted under — would be gone.
    """
    user_id = _a_user(database)
    with database.transaction() as session:
        repository = RefreshTokenRepository(session)
        first = repository.issue(user_id=user_id, token_hash="a" * 64, expires_at=_expiry())
    with database.transaction() as session:
        repository = RefreshTokenRepository(session)
        second = repository.rotate(
            presented_hash="a" * 64, new_token_hash="b" * 64, expires_at=_expiry()
        )

    assert second.family_id == first.family_id
    assert second.parent_id == first.token_id

    with database.read_only() as session:
        repository = RefreshTokenRepository(session)
        chain = repository.family(first.family_id)
        assert [grant.token_hash for grant in chain] == ["a" * 64, "b" * 64]
        assert [grant.token_id for grant in chain] == [first.token_id, second.token_id]


def test_the_presented_token_is_spent_by_the_exchange(database: Database) -> None:
    """A token is single-use, and its use is readable from the rows."""
    user_id = _a_user(database)
    with database.transaction() as session:
        repository = RefreshTokenRepository(session)
        first = repository.issue(user_id=user_id, token_hash="a" * 64, expires_at=_expiry())
        assert repository.was_presented(first.token_id) is False
    with database.transaction() as session:
        RefreshTokenRepository(session).rotate(
            presented_hash="a" * 64, new_token_hash="b" * 64, expires_at=_expiry()
        )

    with database.read_only() as session:
        assert RefreshTokenRepository(session).was_presented(first.token_id) is True


# ── Refusals ────────────────────────────────────────────────────────────────


def test_an_unknown_token_is_refused(database: Database) -> None:
    """A grant that was never issued cannot be exchanged into one that was."""
    with (
        database.transaction() as session,
        pytest.raises(RefreshRefused, match="unknown token"),
    ):
        RefreshTokenRepository(session).rotate(
            presented_hash="f" * 64, new_token_hash="b" * 64, expires_at=_expiry()
        )


def test_an_expired_token_is_refused(database: Database) -> None:
    """Expiry is read from the row, not recomputed from a lifetime setting.

    A grant issued under a longer lifetime than today's setting keeps the
    lifetime it was granted. The grant here was issued two hours ago for one
    hour, which is the only shape an expired grant can have — the check
    constraint refuses one that expires before it was issued, so the test
    cannot take the shortcut of issuing a live grant with a past expiry.
    """
    user_id = _a_user(database)
    with database.transaction() as session:
        RefreshTokenRepository(session).issue(
            user_id=user_id,
            token_hash="a" * 64,
            issued_at=utcnow() - timedelta(hours=2),
            expires_at=utcnow() - timedelta(hours=1),
        )
    with database.transaction() as session, pytest.raises(RefreshRefused, match="expired"):
        RefreshTokenRepository(session).rotate(
            presented_hash="a" * 64, new_token_hash="b" * 64, expires_at=_expiry()
        )


def test_replaying_a_spent_token_is_refused_and_cuts_the_whole_family(
    database: Database,
) -> None:
    """The one case where the refusal has a side effect, and it must have one.

    Two parties hold this secret — that is what a second presentation means —
    so leaving the chain alive would leave the copy usable. Both the thief and
    the victim lose it, which is the only safe answer once a copy exists.
    """
    user_id = _a_user(database)
    with database.transaction() as session:
        repository = RefreshTokenRepository(session)
        first = repository.issue(user_id=user_id, token_hash="a" * 64, expires_at=_expiry())
    with database.transaction() as session:
        repository = RefreshTokenRepository(session)
        repository.rotate(
            presented_hash="a" * 64, new_token_hash="b" * 64, expires_at=_expiry()
        )

    with database.transaction() as session, pytest.raises(RefreshRefused, match="replayed"):
        RefreshTokenRepository(session).rotate(
            presented_hash="a" * 64, new_token_hash="c" * 64, expires_at=_expiry()
        )

    with database.read_only() as session:
        repository = RefreshTokenRepository(session)
        revocation = repository.revocation(first.family_id)
        assert revocation is not None
        assert revocation.user_id == user_id
        assert first.token_id in revocation.reason
        # And the successor, which nobody did anything wrong with, is dead too.
        assert repository.is_revoked(first.family_id) is True

    with (
        database.transaction() as session,
        pytest.raises(RefreshRefused, match="revoked family"),
    ):
        RefreshTokenRepository(session).rotate(
            presented_hash="b" * 64, new_token_hash="d" * 64, expires_at=_expiry()
        )


def test_a_second_detection_does_not_rewrite_the_first_reason(database: Database) -> None:
    """Revocation is a fact, so the first account of it is the one that stands.

    Two processes can notice a replay at the same moment; the row records the
    detection that got there first rather than being overwritten by the loser.
    """
    user_id = _a_user(database)
    with database.transaction() as session:
        repository = RefreshTokenRepository(session)
        first = repository.issue(user_id=user_id, token_hash="a" * 64, expires_at=_expiry())
        repository.revoke_family(family_id=first.family_id, user_id=user_id, reason="first")
    with database.transaction() as session:
        repository = RefreshTokenRepository(session)
        again = repository.revoke_family(
            family_id=first.family_id, user_id=user_id, reason="second"
        )

    assert again.reason == "first"


# ── The database's own rule ─────────────────────────────────────────────────


@pytest.mark.parametrize("table", ["refresh_tokens", "revoked_token_families"])
def test_a_grant_cannot_be_edited_or_deleted(database: Database, table: str) -> None:
    """Append-only, enforced by PostgreSQL rather than by this repository.

    A repository that simply offered no update method would leave the rule to
    whoever writes the next repository. The trigger is what makes "a child
    exists" a fact about the past instead of a claim about the present.
    """
    user_id = _a_user(database)
    with database.transaction() as session:
        RefreshTokenRepository(session).issue(
            user_id=user_id, token_hash="a" * 64, expires_at=_expiry()
        )

    with database.transaction() as session, pytest.raises(IntegrityError):
        session.execute(sa.text(f"UPDATE {table} SET user_id = 'someone-else'"))
    with database.transaction() as session, pytest.raises(IntegrityError):
        session.execute(sa.text(f"DELETE FROM {table}"))

    with database.read_only() as session:
        assert RefreshTokenRepository(session).by_hash("a" * 64) is not None
