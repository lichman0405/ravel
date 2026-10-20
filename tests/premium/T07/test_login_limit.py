"""The login failure budget, as the endpoint applies it.

The policy is in `ravel.gateway.auth.limits` and is exercised here through
`POST /auth/login` rather than through the limiter alone, because the two
properties that matter are both about the route's ordering of it:

- the budget is checked *before* the password is verified, so a pair inside its
  budget is refused even when the password would have been accepted. A limiter
  that ran afterwards would tell an attacker when a guess was right;
- it is checked before the account is looked up, so a locked username gives the
  same answer whether or not it exists.

The clock is the fixture's, advanced by hand. Fifteen minutes is asserted as a
number rather than sat through.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from tests.integration.gateway.conftest import PASSWORD, account
from tests.premium.T07.conftest import MutableClock

from ravel.state.database import Database

#: A source address that is not in `gateway_trusted_proxies`.
DIRECT = "203.0.113.9"
#: A second one, for the case that needs a different peer.
ELSEWHERE = "203.0.113.10"
#: The proxy the fixture configuration names.
PROXY = "127.0.0.1"


def _attempt(
    http: TestClient,
    *,
    username: str,
    password: str,
    headers: dict[str, str] | None = None,
) -> Any:
    return http.post(
        "/auth/login", json={"username": username, "password": password}, headers=headers or {}
    )


def _fail_five(http: TestClient, **kwargs: Any) -> None:
    for _ in range(5):
        refused = _attempt(http, password="not the password", **kwargs)
        assert refused.status_code == 401, refused.text


def test_five_failures_in_a_minute_refuse_even_the_right_password(
    app: Any, database: Database, clock: MutableClock
) -> None:
    """The sixth attempt is a 429, and it is a 429 for the correct password.

    That is the whole point of checking the budget first: an endpoint that
    answered 401 here and 200 on the right guess would let an attacker read
    "this is the one" out of the difference.
    """
    account(database, username="ada")

    with TestClient(app, client=(DIRECT, 5000)) as http:
        _fail_five(http, username="ada")

        locked = _attempt(http, username="ada", password=PASSWORD)
        assert locked.status_code == 429, locked.text
        assert locked.headers["Retry-After"] == "60"
        assert "credentials" not in locked.text.lower()

        # A pair inside its budget is refused before the account is looked up,
        # so a username that does not exist is throttled identically — the 429
        # cannot be used to ask whether an account is registered.
        _fail_five(http, username="nobody-here")
        ghost = _attempt(http, username="nobody-here", password="anything")
        assert ghost.status_code == 429, ghost.text
        assert ghost.headers["Retry-After"] == "60"

        # The minute is a window, not a lock: the same pair is usable again
        # once the failures age out of it.
        clock.advance(60)
        accepted = _attempt(http, username="ada", password=PASSWORD)
        assert accepted.status_code == 200, accepted.text


def test_twenty_failures_in_fifteen_minutes_lock_the_pair_for_fifteen_minutes(
    app: Any, database: Database, clock: MutableClock
) -> None:
    """Twenty failures spread across the window trigger the long lock.

    Spread across four minutes on purpose: five of the failures are inside the
    minute window each time, and none of them alone is enough to lock. The
    cumulative count is what does it, and the lock then refuses a correct
    password until its own fifteen minutes have passed.
    """
    account(database, username="ada")

    with TestClient(app, client=(DIRECT, 5000)) as http:
        for window in range(4):
            _fail_five(http, username="ada")
            if window < 3:
                # Past the minute window, so the next five are not themselves
                # throttled and the fifteen-minute window keeps counting.
                clock.advance(61)

        locked = _attempt(http, username="ada", password=PASSWORD)
        assert locked.status_code == 429, locked.text
        assert locked.headers["Retry-After"] == "900"

        # One second short of the lock, still refused and told how long.
        clock.advance(899)
        still = _attempt(http, username="ada", password=PASSWORD)
        assert still.status_code == 429, still.text
        assert still.headers["Retry-After"] == "1"

        # At the boundary the history has aged out of the fifteen-minute
        # window too, so the pair starts again rather than immediately
        # re-locking on the failures that caused the lock.
        clock.advance(1)
        accepted = _attempt(http, username="ada", password=PASSWORD)
        assert accepted.status_code == 200, accepted.text


def test_a_successful_sign_in_forgets_the_failures_before_it(
    app: Any, database: Database
) -> None:
    """The owner mistyping four times and then getting it right is not penalised.

    The success is evidence that the earlier failures were guesses, so the
    history is cleared and the budget is whole again — five more failures are
    each answered on their merits rather than the sixth being a lock.
    """
    account(database, username="ada")

    with TestClient(app, client=(DIRECT, 5000)) as http:
        for _ in range(4):
            assert _attempt(http, username="ada", password="wrong").status_code == 401
        assert _attempt(http, username="ada", password=PASSWORD).status_code == 200

        for _ in range(5):
            refused = _attempt(http, username="ada", password="wrong")
            assert refused.status_code == 401, refused.text


def test_the_budget_is_per_address_and_account_pair(
    app: Any, database: Database
) -> None:
    """Neither half alone forms the key.

    One address' failures do not touch another account's budget, and one
    account's failures do not follow it to another address. Otherwise a shared
    NAT locks out everybody behind it, or one attacker locks out a known user
    everywhere.
    """
    account(database, username="ada")
    account(database, username="bob")

    with TestClient(app, client=(DIRECT, 5000)) as http:
        _fail_five(http, username="ada")
        assert _attempt(http, username="ada", password=PASSWORD).status_code == 429
        # Same address, different account: untouched.
        assert _attempt(http, username="bob", password=PASSWORD).status_code == 200

    with TestClient(app, client=(ELSEWHERE, 5000)) as http:
        # Different address, same account: untouched.
        assert _attempt(http, username="ada", password=PASSWORD).status_code == 200


def test_the_account_half_of_the_key_is_case_folded(
    app: Any, database: Database
) -> None:
    """`ADA` and `ada` are one account, so they are one budget."""
    account(database, username="ada")

    with TestClient(app, client=(DIRECT, 5000)) as http:
        _fail_five(http, username="ADA")
        refused = _attempt(http, username="ada", password=PASSWORD)
        assert refused.status_code == 429, refused.text


def test_a_forwarded_address_is_believed_only_from_a_named_proxy(
    app: Any, database: Database
) -> None:
    """The deployment's reverse proxy may say who the client is; nobody else may.

    The chain is walked from the right, so an attacker who prepends entries
    cannot choose the bucket. The leftmost entry is the one a naïve reader
    would charge, and it is the one that must not be believed.
    """
    account(database, username="ada")

    with TestClient(app, client=(PROXY, 5000)) as http:
        # Five failures whose rightmost entry is the client, with an invented
        # entry to its left.
        for _ in range(5):
            refused = _attempt(
                http,
                username="ada",
                password="wrong",
                headers={"X-Forwarded-For": "10.9.9.9, 198.51.100.7"},
            )
            assert refused.status_code == 401, refused.text

        # The bucket is 198.51.100.7: the same client, with a different
        # invented left entry, is refused.
        same_client = _attempt(
            http,
            username="ada",
            password=PASSWORD,
            headers={"X-Forwarded-For": "10.9.9.8, 198.51.100.7"},
        )
        assert same_client.status_code == 429, same_client.text

        # A different rightmost entry is a different client.
        other_client = _attempt(
            http,
            username="ada",
            password=PASSWORD,
            headers={"X-Forwarded-For": "10.9.9.9, 198.51.100.8"},
        )
        assert other_client.status_code == 200, other_client.text

    # A peer that is not a named proxy cannot choose its bucket.
    with TestClient(app, client=(DIRECT, 5000)) as http:
        for index in range(5):
            refused = _attempt(
                http,
                username="ada",
                password="wrong",
                headers={"X-Forwarded-For": f"198.51.100.{index + 1}"},
            )
            assert refused.status_code == 401, refused.text
        # Every one of those was charged to the peer, so the peer's next
        # attempt is refused regardless of what it claims.
        refused = _attempt(
            http,
            username="ada",
            password=PASSWORD,
            headers={"X-Forwarded-For": "198.51.100.99"},
        )
        assert refused.status_code == 429, refused.text

    with TestClient(app, client=(ELSEWHERE, 5000)) as http:
        assert _attempt(http, username="ada", password=PASSWORD).status_code == 200


def test_a_malformed_forwarded_chain_charges_the_peer(
    app: Any, database: Database
) -> None:
    """A value that is not an address is not partially believed.

    The trusted proxy's own address is the honest attribution when its header
    cannot be read, because it is the hop that certainly carried the request.
    """
    account(database, username="ada")

    with TestClient(app, client=(PROXY, 5000)) as http:
        for _ in range(5):
            refused = _attempt(
                http,
                username="ada",
                password="wrong",
                headers={"X-Forwarded-For": "not-an-address, also-not-one"},
            )
            assert refused.status_code == 401, refused.text

        # Charged to the proxy's address: a request with no claim at all is
        # the same bucket.
        refused = _attempt(http, username="ada", password=PASSWORD)
        assert refused.status_code == 429, refused.text


def test_signing_in_through_the_proxy_opens_a_working_session(
    app: Any, database: Database
) -> None:
    """The cases above assert refusals; a real sign-in still works end to end."""
    account(database, username="ada")

    with TestClient(app, client=(PROXY, 5000)) as http:
        pair = _attempt(
            http,
            username="ada",
            password=PASSWORD,
            headers={"X-Forwarded-For": "198.51.100.7"},
        )
        assert pair.status_code == 200, pair.text
        me = http.get(
            "/auth/me", headers={"Authorization": f"Bearer {pair.json()['access_token']}"}
        )
        assert me.status_code == 200, me.text
