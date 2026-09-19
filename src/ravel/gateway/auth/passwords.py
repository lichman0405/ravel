"""Password hashing, and the one place a plaintext password exists.

Argon2id is chosen by the deployment rather than by taste here: it is the
current recommendation for new work, it is memory-hard, and the `argon2-cffi`
parameters below are recorded *in the hash string itself*, so a hash written
under these settings keeps verifying after the settings change. Raising the
cost later therefore does not invalidate anyone's password — which is the
property that makes it safe to raise.

The library is imported at the top and never wrapped in a try/except. A
fallback to a weaker algorithm when Argon2 is missing would be a silent
downgrade triggered by an installation mistake, and it would be invisible
from the outside: everything would still verify.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

#: RFC 9106's second recommended option: 64 MiB, one pass, four lanes. The
#: first (2 GiB) is aimed at a machine doing nothing else; this one is sized
#: for a Gateway that also serves the TUI, and is still expensive enough that
#: guessing is not the cheap path.
_HASHER = PasswordHasher(
    time_cost=1,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def hash_password(password: str) -> str:
    """Hash a password for storage.

    The result carries its own salt and parameters, so nothing else needs to
    be stored beside it and no two hashes of the same password are equal.

    Raises:
        ValueError: The password is empty. An empty password is a missing
            password, and storing one would make "no password set" and "the
            password is nothing" the same row.
    """
    if not password:
        raise ValueError("a password cannot be empty")
    return _HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Whether a password matches a stored hash.

    Returns a bool rather than raising, because every failure here means the
    same thing to the caller. The exceptions are still distinguished
    internally, so that a hash written by a different algorithm is a
    programming error rather than a wrong password — but both end as `False`,
    since a login screen must not become an oracle for which accounts have
    malformed hashes.
    """
    try:
        return _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Whether a hash was made under weaker parameters than the current ones.

    Checked after a successful verification, which is the only moment the
    plaintext is available to rehash with.
    """
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True
