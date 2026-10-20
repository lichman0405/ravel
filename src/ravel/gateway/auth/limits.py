"""How often one address may guess one account's password.

Authentication answers *who*; this module bounds how many times it will answer
"no". Without it, `POST /auth/login` is an offline-feeling oracle served over
the network: Argon2 makes each guess expensive for the attacker, but nothing
stops a client from making guesses forever, and a weak password is found by the
thousandth one exactly as surely as by the first.

Three decisions shape the implementation:

**The key is a pair, not an address.** Limiting by address alone lets one
attacker behind a shared NAT lock out every account there. Limiting by account
alone lets anyone anywhere lock out a known user. The pair means a failed guess
costs its own source, and a successful sign-in from that source clears it.

**A lock refuses even a correct password.** The check runs before the hash is
verified, so a caller inside the window learns nothing from timing or from
whether the right password eventually works. Refusing only wrong passwords
would turn the limiter into an oracle for the answer it is meant to protect.

**A forwarded address is believed only from a proxy we named.** The Gateway is
one process behind a reverse proxy in the deployment this is written for, so
the TCP peer of every request is the proxy. `X-Forwarded-For` is consulted
only when the peer itself is in `RAVEL_GATEWAY_TRUSTED_PROXIES`, and the chain
is read from the right: each hop is trusted while it is a configured proxy,
and the first address that is not a proxy is the client. Reading from the left
would let the client choose its own bucket by inventing earlier entries.

The state is per process. V0 runs one Gateway process on one CVM, and the doc
for this task says so; a second process would mean a second limiter, and this
would then need shared state rather than being quietly weaker.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable

from fastapi import Request

#: The documented policy, in one place so that the prose and the behaviour
#: cannot drift apart. Five failures in a minute, twenty in fifteen minutes,
#: and a triggered pair waits fifteen minutes before it is asked again.
FAILURES_PER_MINUTE = 5
MINUTE_SECONDS = 60.0
FAILURES_PER_WINDOW = 20
WINDOW_SECONDS = 15 * 60.0
LOCK_SECONDS = 15 * 60.0

#: How many pairs are remembered at once. Each entry is a handful of floats,
#: so the bound is about making an attacker who varies usernames spend memory
#: at a predictable rate rather than one the process cannot survive. Evicting
#: the least recently used pair is a real trade-off — a flood can forget a
#: lock — and is stated here rather than left implicit.
MAX_TRACKED_PAIRS = 10_000

#: How an unparseable or absent peer is keyed. Shared by everyone whose address
#: cannot be read, which is the conservative direction: they share a throttle.
UNKNOWN_ADDRESS = "unknown"


def _address(value: str) -> str | None:
    """A canonical IP address, or `None` when the value is not one.

    IPv6 addresses are compared in their compressed form so that two spellings
    of one address do not become two buckets.
    """
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


class TrustedProxies:
    """The proxies whose `X-Forwarded-For` this Gateway will believe.

    Empty by default, and empty means the header is ignored entirely: a direct
    client can set it to anything, and honouring it in that case would let the
    client pick its own rate-limit bucket.
    """

    def __init__(self, entries: str | Iterable[str] = "") -> None:
        raw = entries.split(",") if isinstance(entries, str) else list(entries)
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for entry in raw:
            text = entry.strip()
            if not text:
                continue
            try:
                networks.append(ipaddress.ip_network(text, strict=False))
            except ValueError as error:
                raise ValueError(
                    "RAVEL_GATEWAY_TRUSTED_PROXIES must be a comma-separated "
                    "list of IP addresses or CIDRs"
                ) from error
        self._networks = tuple(networks)

    def __bool__(self) -> bool:
        return bool(self._networks)

    def __len__(self) -> int:
        return len(self._networks)

    def trusts(self, address: str) -> bool:
        """Whether an address is one of the configured proxies."""
        parsed = _address(address)
        if parsed is None:
            return False
        candidate = ipaddress.ip_address(parsed)
        return any(candidate in network for network in self._networks)


def client_address(request: Request, trusted: TrustedProxies) -> str:
    """The address a login attempt is charged to.

    The TCP peer unless that peer is a trusted proxy. In that case the
    `X-Forwarded-For` chain is walked from the right, skipping proxies, and the
    first address that is not one is the client. A chain containing a value
    that is not an IP address is not partially believed: the peer is used
    instead, because attributing a request to a value the caller chose is worse
    than attributing it to the proxy that carried it.
    """
    peer = _address(request.client.host) if request.client else None
    if peer is None:
        return UNKNOWN_ADDRESS
    if not trusted.trusts(peer):
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [part.strip() for part in forwarded.split(",") if part.strip()]
    for hop in reversed(hops):
        address = _address(hop)
        if address is None:
            return peer
        if not trusted.trusts(address):
            return address
    # Every hop is a proxy, or there were no hops. The leftmost (if any) is
    # the closest thing to a client the chain offers; charging the peer would
    # make every request through this proxy share one bucket.
    return _address(hops[0]) or peer if hops else peer


def pair_key(address: str, username: str) -> tuple[str, str]:
    """The bucket one login attempt belongs to.

    The account half is case-folded and stripped because usernames are matched
    case-insensitively by the repository, and `Ada` and `ada` must not be two
    budgets for one account.
    """
    return (address, username.strip().casefold())


class _Pair:
    """One pair's recent failures and, when triggered, its lock."""

    __slots__ = ("failures", "locked_until")

    def __init__(self) -> None:
        self.failures: deque[float] = deque()
        self.locked_until = 0.0


class LoginRateLimiter:
    """The per-pair failure budget described at the top of this module.

    The clock is injectable so a test can advance fifteen minutes without
    sleeping fifteen minutes. It is a monotonic clock in production: a wall
    clock that steps backwards would extend a lock, and one that steps forward
    would end it early.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        failures_per_minute: int = FAILURES_PER_MINUTE,
        minute_seconds: float = MINUTE_SECONDS,
        failures_per_window: int = FAILURES_PER_WINDOW,
        window_seconds: float = WINDOW_SECONDS,
        lock_seconds: float = LOCK_SECONDS,
        max_pairs: int = MAX_TRACKED_PAIRS,
    ) -> None:
        if min(failures_per_minute, failures_per_window, max_pairs) < 1:
            raise ValueError("rate limits have to permit at least one attempt")
        if min(minute_seconds, window_seconds, lock_seconds) <= 0:
            raise ValueError("rate-limit windows have to be forward in time")
        if minute_seconds > window_seconds:
            raise ValueError("the minute window cannot outlast the window that contains it")
        self._now = now
        self._per_minute = failures_per_minute
        self._minute = minute_seconds
        self._per_window = failures_per_window
        self._window = window_seconds
        self._lock_seconds = lock_seconds
        self._max_pairs = max_pairs
        self._pairs: OrderedDict[tuple[str, str], _Pair] = OrderedDict()
        self._lock = threading.Lock()

    def retry_after(self, key: tuple[str, str]) -> int | None:
        """Seconds the caller must wait, or `None` when it may try now.

        Called *before* the password is verified. A pair already locked is
        answered from the lock alone; a pair that has spent its budget is
        answered from the failure that spent it. Reaching twenty failures
        inside the fifteen-minute window is what turns the short refusal into
        the long one, and the transition happens here so that it also applies
        to a caller that stops sending failures.
        """
        now = self._now()
        with self._lock:
            pair = self._pairs.get(key)
            if pair is None:
                return None
            self._prune(pair, now)
            if pair.locked_until > now:
                return max(1, math.ceil(pair.locked_until - now))
            if len(pair.failures) >= self._per_window:
                pair.locked_until = now + self._lock_seconds
                return max(1, math.ceil(self._lock_seconds))
            recent = [stamp for stamp in pair.failures if now - stamp < self._minute]
            if len(recent) >= self._per_minute:
                return max(1, math.ceil(self._minute - (now - recent[0])))
            return None

    def record_failure(self, key: tuple[str, str]) -> None:
        """Charge one failed attempt to a pair."""
        now = self._now()
        with self._lock:
            pair = self._pairs.get(key)
            if pair is None:
                pair = self._pairs[key] = _Pair()
                while len(self._pairs) > self._max_pairs:
                    self._pairs.popitem(last=False)
            self._pairs.move_to_end(key)
            self._prune(pair, now)
            pair.failures.append(now)
            if len(pair.failures) >= self._per_window:
                pair.locked_until = max(pair.locked_until, now + self._lock_seconds)

    def succeeded(self, key: tuple[str, str]) -> None:
        """Forget a pair's history, because the password was right.

        Called only after the account was found, active, and its password
        verified. A successful sign-in is the proof the failures were guesses
        rather than the owner mistyping, and leaving the history in place would
        let an attacker's earlier guesses count against the owner afterwards.
        """
        with self._lock:
            self._pairs.pop(key, None)

    def _prune(self, pair: _Pair, now: float) -> None:
        while pair.failures and now - pair.failures[0] >= self._window:
            pair.failures.popleft()


__all__ = [
    "FAILURES_PER_MINUTE",
    "FAILURES_PER_WINDOW",
    "LOCK_SECONDS",
    "MAX_TRACKED_PAIRS",
    "MINUTE_SECONDS",
    "UNKNOWN_ADDRESS",
    "WINDOW_SECONDS",
    "LoginRateLimiter",
    "TrustedProxies",
    "client_address",
    "pair_key",
]
