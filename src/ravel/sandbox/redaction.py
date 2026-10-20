"""Removing credentials from what a sandbox run leaves behind.

A command line is one of the reliable ways a credential escapes into a log:
`ps` shows it, a shell's `set -x` prints it, and a supervisor that stores the
argv verbatim has just written the deployment token into a JSON file with the
same permissions as every other run record. The supervisor therefore stores the
redacted argv, and it redacts captured output before it stores or returns it.

Two mechanisms, because one is not enough:

- **Patterns** catch the shapes credentials take in text — `password=...`,
  `Authorization: Bearer ...`, a token in a URL's userinfo or query string, a
  `--password VALUE` flag. They work without knowing the value.
- **Exact values** catch a secret whose spelling the patterns do not know. The
  supervisor is configured with the deployment token and with the values of the
  environment entries it is willing to inject, and replaces those exact strings.

Redaction is deliberately performed on the way *into* the record and again on
the way out, not only at the edge. A record read after a restart is read from
disk, and a disk record that still held the secret would make the write-time
redaction cosmetic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

REDACTED = "[REDACTED]"

#: `name=value` and `name: value` for names that denote a credential. The value
#: stops at whitespace, which leaves surrounding punctuation intact so a
#: redacted line stays readable.
_ASSIGNMENT = re.compile(
    r"(?i)\b("
    r"password|passwd|passphrase|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|authorization|client[_-]?secret"
    r")(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)

#: A bearer token, with or without the `Authorization:` prefix.
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")

#: URL userinfo: `scheme://user:password@host`. The username is kept because it
#: is not the secret and is often the diagnostic.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/@\s:]+):([^/@\s]+)@")

#: Credential flags. The flag stays so the reader can see what was passed, and
#: the value goes. Handles `--password value`, `--password=value`, and
#: `--token value`.
_FLAG = re.compile(
    r"(?i)(--?(?:password|passwd|token|api[_-]?key|access[_-]?key|secret|authorization)"
    r"(?:=|\s+))(\"[^\"]*\"|'[^']*'|[^\s]+)"
)

#: Query parameters that carry credentials.
_QUERY = re.compile(
    r"(?i)([?&](?:token|api[_-]?key|access[_-]?key|password|secret|sig|signature)=)[^&#\s]+"
)


class SecretRedactor:
    """Pattern and exact-value redaction, in one object.

    `secrets` are replaced literally and first, longest-first, so that a secret
    containing a substring another secret matches is not partially replaced.
    A secret shorter than four characters is ignored: replacing every `a` in
    the output would destroy the record without protecting anything, and no
    deployment credential is one character.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        cleaned = sorted(
            {secret for secret in secrets if isinstance(secret, str) and len(secret) >= 4},
            key=len,
            reverse=True,
        )
        self._secrets = tuple(cleaned)

    def redact(self, text: str) -> str:
        """A copy of `text` with every configured secret and credential shape removed."""
        if not text:
            return text
        result = text
        for secret in self._secrets:
            result = result.replace(secret, REDACTED)
        result = _URL_USERINFO.sub(r"\1:" + REDACTED + "@", result)
        result = _BEARER.sub("Bearer " + REDACTED, result)
        result = _ASSIGNMENT.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", result
        )
        result = _FLAG.sub(lambda match: f"{match.group(1)}{REDACTED}", result)
        result = _QUERY.sub(r"\1" + REDACTED, result)
        return result

    def redact_argv(self, argv: Sequence[str]) -> tuple[str, ...]:
        """Redact an argument list as a whole and per argument.

        Per argument matters because a flag and its value are separate strings:
        the pattern that spans `--password secret` never sees both at once in an
        argv list, so each element is also checked for the bare shapes. The
        whole-list pass still runs for the `--password=secret` form.
        """
        whole = self.redact(" ".join(argv))
        # If the whole-list pass changed the line, the safe representation is
        # the redacted line as one string rather than an argv whose element
        # boundaries no longer mean anything. Callers display it; they do not
        # execute it.
        if whole != " ".join(argv):
            return (whole,)
        return tuple(self.redact(part) for part in argv)

    def contains_secret(self, text: str) -> bool:
        """Whether any configured secret survives in `text`.

        Used by tests and by the supervisor's final assertion before it writes
        output. The pattern shapes are not checked here: this answers the
        narrower and more important question "is a value we hold still present".
        """
        return any(secret in text for secret in self._secrets)
