"""Paths a sandbox request may name, and the ones it may not.

A sandbox is confined to one project's workspace. That statement is only true
if relative paths cannot walk out of it, so every path that crosses the
provider boundary is checked here before it reaches a shell, a Docker API, or
the filesystem helper inside the container:

- no absolute path and no drive letter;
- no backslash, because a Linux container and a Windows caller disagree about
  what a separator is and only one of them is in force at the boundary;
- no NUL, which truncates a path in enough APIs to be a bypass;
- no `.` or `..` component, not even one that resolves back inside — a
  traversal that happens to land in bounds today is a traversal that leaves
  when the layout changes;
- no empty component from a doubled separator.

The checks here are lexical. The container-side helper repeats them and then
uses `realpath` plus `O_NOFOLLOW`, because a lexical check cannot see a symlink
that was created after it ran. Both layers are required: the lexical one keeps
malformed requests from becoming work, and the filesystem one is what actually
contains a running command that makes its own symlinks.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from ravel.sandbox.models import SandboxValidationError

#: Long enough for any real path, short enough that a supervisor record cannot
#: be made enormous by one request.
MAX_PATH_LENGTH = 1024

#: Windows drive-letter form, refused on every platform. A caller that sends
#: `C:/...` is naming a host path, and the supervisor is not the host.
_DRIVE = re.compile(r"^[A-Za-z]:")

#: Control characters other than none: tabs and newlines in a path are almost
#: always a sign that a caller is trying to split a record or an argument.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def validate_relative_path(path: str, *, allow_empty: bool = True) -> str:
    """Return `path` if it is a safe project-relative path.

    Args:
        path: The path as the caller wrote it. A trailing slash is accepted and
            preserved only on an empty path; elsewhere it is an empty final
            component and is refused.
        allow_empty: Whether the workspace root itself may be named. Listing
            the root is legitimate; reading it as a file is not.

    Raises:
        SandboxValidationError: The path is absolute, traversing, contains a
            separator the container does not use, or is otherwise unusable.
    """
    if not isinstance(path, str):
        raise SandboxValidationError("a sandbox path must be a string")
    if not path:
        if allow_empty:
            return ""
        raise SandboxValidationError("this operation needs a file path, not the workspace root")
    if len(path) > MAX_PATH_LENGTH:
        raise SandboxValidationError(f"a sandbox path may be at most {MAX_PATH_LENGTH} characters")
    if "\x00" in path:
        raise SandboxValidationError("a sandbox path may not contain NUL")
    if _CONTROL.search(path):
        raise SandboxValidationError("a sandbox path may not contain control characters")
    if path.startswith("/") or _DRIVE.match(path):
        raise SandboxValidationError(
            f"{path!r} is absolute; a sandbox path is relative to the project workspace"
        )
    if "\\" in path:
        raise SandboxValidationError(
            f"{path!r} contains a backslash; the sandbox filesystem is POSIX and a "
            "backslash there is an ordinary filename character, not a separator"
        )

    parts = PurePosixPath(path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SandboxValidationError(
            f"{path!r} contains an empty, current-directory, or parent component"
        )
    if path.endswith("/"):
        raise SandboxValidationError(f"{path!r} ends with a separator")
    return path


def contained(root: Path, path: Path) -> bool:
    """Whether `path` resolves to somewhere under `root`.

    `resolve()` follows symlinks, including ones that already exist, so a
    symlink pointing outside the workspace is reported as outside. It cannot
    protect a target created between this call and the use; the container-side
    helper's `O_NOFOLLOW` is what closes that interval.
    """
    try:
        root_resolved = root.resolve()
        candidate = path.resolve()
    except (OSError, RuntimeError):
        return False
    return candidate == root_resolved or candidate.is_relative_to(root_resolved)


def validate_artifact_name(name: str) -> str:
    """An artifact name is one path component.

    Artifact names become registration keys in the Gateway. A name with a
    separator is not a name, and a name that is `.` or `..` is a bug in the
    caller that must not become a stored object.
    """
    if not isinstance(name, str) or not name.strip():
        raise SandboxValidationError("an artifact needs a non-empty name")
    if len(name) > 255:
        raise SandboxValidationError("an artifact name may be at most 255 characters")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise SandboxValidationError(f"{name!r} is not a single artifact name")
    if "\x00" in name or _CONTROL.search(name):
        raise SandboxValidationError("an artifact name may not contain control characters")
    return name
