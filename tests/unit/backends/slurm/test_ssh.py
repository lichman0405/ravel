"""Redaction, and the one place a credential could escape.

`Redactor` is small enough to look obviously correct and important enough that
"obviously" is not the standard. What it guards is not a debug convenience: a
`detail` line ends up in a PostgreSQL column and an exception message ends up in
a log file, and both outlive the process that held the password.
"""

from __future__ import annotations

import pytest

from ravel.backends.slurm.ssh import (
    REDACTED,
    CommandResult,
    Redactor,
    _clean,
)

SECRET = "correct-horse-battery-staple"


def test_a_secret_is_removed_wherever_it_appears() -> None:
    redact = Redactor((SECRET,))
    text = f"sshpass failed for user admin with password {SECRET} (tried {SECRET})"
    cleaned = redact(text)
    assert SECRET not in cleaned
    assert cleaned.count(REDACTED) == 2
    assert "user admin" in cleaned


def test_an_empty_secret_is_skipped_rather_than_substituted() -> None:
    """Otherwise every string would come back with the marker between its letters.

    A deployment with no password set — key authentication — builds a redactor
    from `None`, and `("")` is what that becomes. Replacing the empty string is
    a replacement at every position.
    """
    redact = Redactor(("",))
    assert redact("nothing to hide") == "nothing to hide"


def test_a_redactor_with_no_secrets_changes_nothing() -> None:
    assert Redactor()("a plain sentence") == "a plain sentence"


def test_a_mapping_is_redacted_value_by_value_and_leaves_numbers_alone() -> None:
    """`completion_metadata` is a dictionary, and half of it is not text."""
    redact = Redactor((SECRET,))
    cleaned = redact.mapping(
        {"note": f"the password is {SECRET}", "exit_code": 137, "job": "7001"}
    )
    assert cleaned["note"] == f"the password is {REDACTED}"
    # A number passes through unchanged: redaction is a text operation, and an
    # exit status that came back as a string would break every reader of it.
    assert cleaned["exit_code"] == 137
    assert cleaned["job"] == "7001"


def test_a_mapping_is_a_new_dictionary() -> None:
    """It is called on the way into a record; mutating the caller's would be a
    second place the unredacted values still exist."""
    original = {"note": SECRET}
    redact = Redactor((SECRET,))
    cleaned = redact.mapping(original)
    assert original["note"] == SECRET
    assert cleaned["note"] == REDACTED


def test_several_secrets_are_all_removed() -> None:
    redact = Redactor(("first-secret", "second-secret"))
    cleaned = redact("first-secret then second-secret")
    assert "first-secret" not in cleaned
    assert "second-secret" not in cleaned
    assert cleaned == f"{REDACTED} then {REDACTED}"


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("sshpass: password authentication failed", "password authentication"),
        ("", "no output"),
    ],
)
def test_a_command_result_describes_itself_without_being_asked(text: str, needle: str) -> None:
    result = CommandResult(command="sbatch job.slurm", exit_status=1, stderr=text)
    described = result.describe()
    assert "sbatch job.slurm" in described
    assert "exited 1" in described
    assert needle in described


def test_a_failed_command_is_not_ok_and_a_successful_one_is() -> None:
    assert CommandResult(command="true", exit_status=0).ok
    assert not CommandResult(command="false", exit_status=1).ok
    # `output` strips the trailing newline, because a value read out of a
    # command's stdout is the value and not the line ending after it.
    assert CommandResult(command="echo x", exit_status=0, stdout="x\n").output == "x"


def test_key_material_echoed_by_the_far_side_never_becomes_a_message() -> None:
    """A banner is a string an untrusted host controls.

    `_clean` is the last point at which every string built from an exception is
    in one place. A host that answers a handshake with a private key block — by
    accident or to get it into a log — does not get it there.
    """
    cleaned = _clean(
        "SSH error: -----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END"
    )
    assert "PRIVATE KEY" not in cleaned
    assert "b3BlbnNzaC1rZXk" not in cleaned


def test_an_ordinary_message_passes_through_unchanged() -> None:
    assert _clean("connection refused") == "connection refused"
