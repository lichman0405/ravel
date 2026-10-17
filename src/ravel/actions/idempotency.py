"""The external systems' own idempotency keys, computed in one place.

An Action ID makes a duplicate *RAVEL* call return the original record. It does
not make a duplicate *submission* safe: Slurm and SMTP each decide for
themselves whether two requests are the same one, and the only thing RAVEL can
do is give them a stable key they can honour.

- HPC: `submission_key = hash(project_id, node_id, contract_version, attempt)`.
  A retry of the same attempt carries the same key; a real retry is a new
  attempt and therefore a new key.
- Mail: the `Message-ID` is fixed by the draft. A provider may use it to
  correlate, but SMTP does not promise deduplication, which is why a send
  after an uncertain `DATA` is `UNKNOWN_DELIVERY` and waits for a human rather
  than being retried under the same header.

Both helpers are deterministic functions of facts already in the Action, so a
process that restarts computes the same value rather than inventing a new one.
"""

from __future__ import annotations

import hashlib
import json
import re

from ravel.domain.enums import ActionKind

#: A submission key has the same shape as every other hash in the premium
#: schemas, so a reader does not need a second format to validate it.
_KEY = re.compile(r"^sha256:[0-9a-f]{64}$")

#: RFC 5322 angle-addr's message-id form, loosely: `<local@domain>`. The point
#: of validating is not to parse the grammar but to refuse a "fixed" identifier
#: that is not one — an empty string would silently give every send the same
#: key, which is the failure mode this exists to prevent.
_MESSAGE_ID = re.compile(r"^<[^<>\s@]+@[^<>\s@]+>$")


def compute_submission_key(
    *,
    project_id: str,
    node_id: str,
    contract_version: int,
    attempt: int,
) -> str:
    """The HPC submission key for one attempt at one frozen contract.

    `attempt` is part of the key rather than a retry counter on the side: a
    genuine retry is a different attempt and must not collide with the attempt
    whose outcome is unknown. The caller supplies it from the attempt the run
    is actually on.

    Raises:
        ValueError: Any component is empty, or the attempt is not positive.
    """
    if not project_id or not node_id:
        raise ValueError("a submission key needs a project and a node")
    if contract_version < 1:
        raise ValueError("contract_version must be at least 1")
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    body = json.dumps(
        {
            "project_id": project_id,
            "node_id": node_id,
            "contract_version": contract_version,
            "attempt": attempt,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def validate_submission_key(value: str) -> str:
    """Refuse a submission key that is not the hash this module computes."""
    if not _KEY.fullmatch(value):
        raise ValueError(
            "submission_key must be 'sha256:' followed by 64 lowercase hex "
            "digits; a hand-written key cannot promise that two submissions "
            "of the same attempt are the same submission"
        )
    return value


def validate_message_id(value: str) -> str:
    """Refuse a Message-ID that does not have the shape of one.

    Raises:
        ValueError: The value is empty, has whitespace, or is not an
            angle-addr. A provider that cannot read it as an identifier cannot
            use it to correlate two sends.
    """
    if not value:
        raise ValueError("a mail Action requires a fixed Message-ID")
    if not _MESSAGE_ID.fullmatch(value):
        raise ValueError(
            f"Message-ID {value!r} must be angle-delimited, like "
            "<draft-123@ravel.example>; the value is what makes a resend "
            "recognisable and an arbitrary string does not identify anything"
        )
    return value


def attempt_number(payload: dict[str, object], *, default: int = 1) -> int:
    """The attempt a payload names, or the default for a first submission.

    Raises:
        ValueError: The named attempt is not a positive integer.
    """
    raw = payload.get("attempt", default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("payload.attempt must be an integer")
    if raw < 1:
        raise ValueError("payload.attempt must be at least 1")
    return raw


def mail_recipients(payload: dict[str, object]) -> dict[str, list[str]]:
    """The full recipient set an approval must cover.

    All three fields are returned even when only `to` is populated, because an
    approval that silently omitted `bcc` would bind less than the message
    actually sends.

    Raises:
        ValueError: A field is not a list of non-empty strings, or `to` is
            empty. A mail Action with no direct recipient is not a send this
            package can promise.
    """
    recipients: dict[str, list[str]] = {}
    for key in ("to", "cc", "bcc"):
        raw = payload.get(key, [])
        if isinstance(raw, str):
            raw = [raw]
        if raw is None:
            raw = []
        if not isinstance(raw, (list, tuple)):
            raise ValueError(f"payload.{key} must be a list of addresses")
        values: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"payload.{key} must contain non-empty addresses")
            values.append(item.strip())
        recipients[key] = values
    if not recipients["to"]:
        raise ValueError("a mail Action requires at least one 'to' recipient")
    return recipients


def normalise_payload(kind: ActionKind, payload: dict[str, object]) -> dict[str, object]:
    """Validate the kind-specific idempotency facts and return the payload.

    This runs before the hash is computed, because a missing identifier is not
    something an approval can repair: the Action would be created without the
    one fact that makes a retry the same submission.

    Raises:
        ValueError: The payload does not carry what the kind requires.
    """
    body = dict(payload)
    if kind is ActionKind.MAIL_SEND:
        body["message_id"] = validate_message_id(
            str(body.get("message_id", "")).strip()
        )
        mail_recipients(body)
    elif kind is ActionKind.COMPUTE_SUBMIT:
        key = body.get("submission_key")
        if key is not None:
            body["submission_key"] = validate_submission_key(str(key))
    return body


__all__ = [
    "attempt_number",
    "compute_submission_key",
    "mail_recipients",
    "normalise_payload",
    "validate_message_id",
    "validate_submission_key",
]
