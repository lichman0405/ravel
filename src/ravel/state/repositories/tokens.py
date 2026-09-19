"""Refresh grants, stored as the history of a login rather than its current state.

Not project-scoped, and deliberately: a login is to RAVEL, not to a project,
and the authority it carries is whatever memberships the user holds when a
request arrives. Scoping a grant to a project would put a second, staler copy
of the authorization decision into the token.

The repository holds facts and decides whether a grant may be exchanged. It
does not mint secrets — `ravel.gateway.auth.tokens` does that — so the rule
"a fast digest is right for a token and wrong for a password" is stated once,
where the token is made, rather than assumed here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ravel.domain.clock import utcnow
from ravel.domain.identity import RefreshToken, RevokedTokenFamily
from ravel.domain.ids import new_id
from ravel.state.mapping import from_row
from ravel.state.tables import RefreshTokenRow, RevokedTokenFamilyRow


class RefreshRefused(PermissionError):
    """A presented refresh grant that will not be exchanged.

    One type rather than one per cause, because the caller's answer is the
    same in every case: refuse, and say no more than that. The `reason` is for
    the log and for tests. It is not for the response body — telling a caller
    whether a token was expired, revoked, or already spent tells whoever stole
    one whether the theft was noticed, and "already spent" is the answer that
    would let them stop before the family is cut.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RefreshTokenRepository:
    """Grants issued to a user, and the families that have been revoked."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ── Reading ─────────────────────────────────────────────────────────────

    def by_hash(self, token_hash: str) -> RefreshToken | None:
        """The grant a presented token digests to, if it was ever issued.

        The lookup is by digest because the digest is what the unique index is
        on; the secret itself exists only in the client's hands and in the
        response that carried it.
        """
        row = (
            self.session.query(RefreshTokenRow)
            .filter(RefreshTokenRow.token_hash == token_hash)
            .one_or_none()
        )
        return from_row(RefreshToken, row) if row is not None else None

    def children_of(self, token_id: str) -> list[RefreshToken]:
        """The grants issued by exchanging this one.

        A non-empty list is the replay signal: a token is single-use, so a
        second presentation of one that already produced a successor means the
        secret was copied.
        """
        rows = (
            self.session.query(RefreshTokenRow)
            .filter(RefreshTokenRow.parent_id == token_id)
            .order_by(RefreshTokenRow.issued_at)
            .all()
        )
        return [from_row(RefreshToken, row) for row in rows]

    def was_presented(self, token_id: str) -> bool:
        """Whether this grant has already been exchanged."""
        return bool(self.children_of(token_id))

    def revocation(self, family_id: str) -> RevokedTokenFamily | None:
        """The revocation of this family, if it has been revoked."""
        row = self.session.get(RevokedTokenFamilyRow, family_id)
        return from_row(RevokedTokenFamily, row) if row is not None else None

    def is_revoked(self, family_id: str) -> bool:
        """Whether this login chain is no longer accepted."""
        return self.revocation(family_id) is not None

    def family(self, family_id: str) -> list[RefreshToken]:
        """Every grant in one chain, oldest first."""
        rows = (
            self.session.query(RefreshTokenRow)
            .filter(RefreshTokenRow.family_id == family_id)
            .order_by(RefreshTokenRow.issued_at)
            .all()
        )
        return [from_row(RefreshToken, row) for row in rows]

    # ── Writing ─────────────────────────────────────────────────────────────

    def issue(
        self,
        *,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        issued_at: datetime | None = None,
        family_id: str | None = None,
        parent_id: str | None = None,
    ) -> RefreshToken:
        """Record a grant. A first grant opens its own family.

        The caller supplies the digest and the grant's timing, so this method
        decides nothing about either; what it decides is the shape of the
        chain, which is the part a caller must not get wrong on its own. The
        split is the same one `expires_at` already made: when a grant lives is
        policy and belongs to the caller that read the settings, and how a
        chain is shaped is a rule this repository exists to hold.

        `issued_at` defaults to now. A caller states it only when recording a
        grant that was issued at some other moment, which the database still
        holds to the only rule that matters: a grant must outlive its issue.

        Raises:
            IntegrityError: `expires_at` is not after `issued_at`. Raised by
                the check constraint rather than checked here, so that the rule
                holds for every writer rather than for this one.
        """
        grant = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            issued_at=issued_at or utcnow(),
            parent_id=parent_id,
            family_id=family_id or new_id(),
        )
        self.session.add(RefreshTokenRow(**grant.model_dump(mode="python")))
        return grant

    def rotate(
        self,
        *,
        presented_hash: str,
        new_token_hash: str,
        expires_at: datetime,
    ) -> RefreshToken:
        """Exchange a grant for its successor, or refuse.

        A grant is single-use. Exchanging one that has already produced a
        successor means two parties hold the same secret, so the family is
        revoked before the refusal is raised: the thief and the victim both
        lose the chain, which is the only safe answer once a token is known to
        have been copied. Rotating first and checking afterwards would leave
        the copy alive for as long as the check took.

        Raises:
            RefreshRefused: The token was never issued, its family is revoked,
                it has expired, or it has already been exchanged.
        """
        presented = self.by_hash(presented_hash)
        if presented is None:
            raise RefreshRefused("unknown token")

        if self.is_revoked(presented.family_id):
            raise RefreshRefused("revoked family")

        if presented.is_expired():
            raise RefreshRefused("expired")

        if self.was_presented(presented.token_id):
            self.revoke_family(
                family_id=presented.family_id,
                user_id=presented.user_id,
                reason=f"refresh token {presented.token_id} was presented twice",
            )
            raise RefreshRefused("replayed")

        return self.issue(
            user_id=presented.user_id,
            token_hash=new_token_hash,
            expires_at=expires_at,
            family_id=presented.family_id,
            parent_id=presented.token_id,
        )

    def revoke_family(
        self, *, family_id: str, user_id: str, reason: str = ""
    ) -> RevokedTokenFamily:
        """Stop accepting a whole login chain.

        Idempotent in effect and not in fact: revoking an already-revoked
        family is refused by the primary key rather than by a check here, so
        two concurrent detections cannot both believe they were the one that
        cut the chain. The caller that loses the race is told, and the reason
        recorded is the first one, which is the detection that mattered.
        """
        existing = self.revocation(family_id)
        if existing is not None:
            return existing
        revocation = RevokedTokenFamily(
            family_id=family_id, user_id=user_id, reason=reason, revoked_at=utcnow()
        )
        self.session.add(RevokedTokenFamilyRow(**revocation.model_dump(mode="python")))
        return revocation
