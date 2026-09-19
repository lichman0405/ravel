"""Who is asking, and what they are allowed to do once identified.

Two questions, answered in that order and never conflated:

- **Authentication** — `passwords` and `tokens` answer "which account is
  this?". The answer is an identifier and nothing more.
- **Authorization** — the Gateway then reads that account's *membership* in the
  project being addressed. The answer is a role, and it is read from
  PostgreSQL on every request.

The split is what makes revocation immediate. A membership withdrawn now takes
effect on the next request, because no claim about authority was ever put in
the token. An access token that carried `role: PROJECT_OWNER` would keep saying
so until it expired, and the only lever left would be shortening its life.
"""

from __future__ import annotations

from ravel.gateway.auth.passwords import hash_password, needs_rehash, verify_password
from ravel.gateway.auth.tokens import (
    AccessGrant,
    InvalidAccessToken,
    TokenService,
    require_a_real_secret,
)

__all__ = [
    "AccessGrant",
    "InvalidAccessToken",
    "TokenService",
    "hash_password",
    "needs_rehash",
    "require_a_real_secret",
    "verify_password",
]
