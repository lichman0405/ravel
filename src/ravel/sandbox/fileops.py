"""The filesystem half of a sandbox, running inside the sandbox.

The supervisor never mounts a project's volume into itself. It runs this helper
in the sandbox container with `docker exec`, and the helper reads or writes one
path relative to `/workspace`. That keeps the project's files in one place
owned by the sandbox's uid and keeps the supervisor (which holds the Docker
socket) from also holding every project's data.

The path policy has two layers. `ravel.sandbox.paths` rejects the lexical
attacks before a request is sent. This module assumes that check happened and
repeats it, then applies the checks only a filesystem can make:

- every existing component from the workspace root down is `lstat`ed and a
  symlink is refused;
- the final open uses `O_NOFOLLOW`, so a symlink created between the component
  walk and the open fails the open rather than being followed;
- a regular file with `st_nlink > 1` is refused: a hard link is a second name
  for bytes somewhere else, and reading through one would read a file the
  project's workspace does not contain;
- reads are bounded by the requested maximum before any bytes are returned;
- a write is a temporary file in the destination directory followed by
  `os.replace`, so a reader never sees a half-written artifact, and the
  temporary file is opened with `O_EXCL | O_NOFOLLOW`.

Operations are `list`, `read`, `write`, and `collect`. Writes require the
caller to name the hash it expects the bytes to have; the helper computes the
hash independently and refuses a mismatch. `collect` returns only a manifest:
the supervisor reads the bytes through `read`, which is the same path that
enforces every check above.

Output is one JSON object on stdout. Errors are JSON with `ok: false` and exit
status 1; the supervisor turns them into typed sandbox errors. The helper
never prints a traceback, because a traceback would include container paths and
possibly command arguments.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import mimetypes
import os
import stat
import sys
from pathlib import Path
from typing import Any

from ravel.sandbox.models import SandboxValidationError
from ravel.sandbox.paths import validate_relative_path

WORKSPACE = Path(os.environ.get("RAVEL_SANDBOX_WORKSPACE", "/workspace"))
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024


class FileOpError(RuntimeError):
    """A file operation the sandbox will not perform."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _root() -> str:
    root = os.path.realpath(WORKSPACE)
    if not os.path.isdir(root):
        raise FileOpError("no_workspace", "the sandbox workspace does not exist")
    return root


def _relative(relative: str, *, allow_root: bool) -> tuple[str, str]:
    """Validate a request path and resolve it under the workspace root.

    Returns the resolved root and the candidate path. The candidate is *not*
    resolved: following it is exactly what `O_NOFOLLOW` exists to prevent.
    """
    try:
        cleaned = validate_relative_path(relative, allow_empty=allow_root)
    except SandboxValidationError as exc:
        raise FileOpError("invalid_path", str(exc)) from exc
    root = _root()
    candidate = os.path.join(root, cleaned) if cleaned else root
    if not _under(root, candidate):
        raise FileOpError("escape", "the path is outside the project workspace")
    _check_components(root, cleaned)
    return root, candidate


def _under(root: str, candidate: str) -> bool:
    return candidate == root or candidate.startswith(root + os.sep)


def _check_components(root: str, relative: str) -> None:
    """Refuse a symlink anywhere on the path that already exists."""
    if not relative:
        return
    current = root
    for part in relative.split("/"):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise FileOpError("unreadable_path", f"cannot inspect a path component: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise FileOpError(
                "symlink",
                "the path contains a symbolic link; a sandbox path may not be a "
                "second name for somewhere else",
            )


def _open_regular(path: str) -> tuple[int, os.stat_result]:
    """Open a regular file without following a final symlink or a hard link."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise FileOpError("not_readable", f"cannot open the file: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise FileOpError("not_a_file", "the path is not a regular file")
        if info.st_nlink > 1:
            raise FileOpError(
                "hardlink",
                "the file has more than one hard link; reading through a second "
                "name is not reading a file the workspace contains",
            )
    except Exception:
        os.close(fd)
        raise
    return fd, info


def list_entries(relative: str) -> dict[str, Any]:
    _root, path = _relative(relative, allow_root=True)
    try:
        directory_fd = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise FileOpError("not_a_directory", f"cannot open the directory: {exc}") from exc
    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(directory_fd) as scan:
            for entry in scan:
                try:
                    inode = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(inode.st_mode):
                    kind = "symlink"
                elif stat.S_ISDIR(inode.st_mode):
                    kind = "directory"
                elif stat.S_ISREG(inode.st_mode):
                    kind = "file"
                else:
                    kind = "other"
                prefix = relative.rstrip("/")
                entry_path = f"{prefix}/{entry.name}" if prefix else entry.name
                entries.append(
                    {
                        "name": entry.name,
                        "path": entry_path,
                        "kind": kind,
                        "size_bytes": int(inode.st_size) if kind == "file" else 0,
                    }
                )
    finally:
        os.close(directory_fd)
    entries.sort(key=lambda item: (item["kind"] != "directory", item["name"]))
    return {"ok": True, "root": str(WORKSPACE), "entries": entries}


def read_file(relative: str, max_bytes: int) -> dict[str, Any]:
    if max_bytes <= 0 or max_bytes > MAX_FILE_BYTES:
        raise FileOpError("invalid_limit", f"max_bytes must be in 1..{MAX_FILE_BYTES}")
    _, path = _relative(relative, allow_root=False)
    fd, info = _open_regular(path)
    try:
        if info.st_size > max_bytes:
            raise FileOpError(
                "too_large",
                f"the file is {info.st_size} bytes and the request allows {max_bytes}",
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise FileOpError("too_large", "the file grew past the request's limit while reading it")
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    return {
        "ok": True,
        "path": relative,
        "size_bytes": len(data),
        "content_hash": digest,
        "data_b64": base64.b64encode(data).decode("ascii"),
    }


def _ensure_parents(root: str, relative: str) -> str:
    """Create missing parent directories, refusing to reuse a symlink."""
    parent_relative = os.path.dirname(relative)
    if not parent_relative:
        return root
    # Re-check after each creation: `makedirs` follows a symlink in a component
    # that appeared after the first walk, so the resolved parent is compared to
    # the root again immediately before the write.
    parent = os.path.join(root, parent_relative)
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise FileOpError("not_writable", f"cannot create the parent directory: {exc}") from exc
    real_parent = os.path.realpath(parent)
    if not (real_parent == root or real_parent.startswith(root + os.sep)):
        raise FileOpError("escape", "the parent directory resolves outside the project workspace")
    _check_components(root, parent_relative)
    return parent


def write_file(relative: str, max_bytes: int, envelope: dict[str, Any]) -> dict[str, Any]:
    if max_bytes <= 0 or max_bytes > MAX_FILE_BYTES:
        raise FileOpError("invalid_limit", f"max_bytes must be in 1..{MAX_FILE_BYTES}")
    encoded = envelope.get("data_b64")
    if not isinstance(encoded, str):
        raise FileOpError("invalid_request", "write requests carry data_b64")
    try:
        data = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise FileOpError("invalid_request", "data_b64 is not base64") from exc
    if len(data) > max_bytes:
        raise FileOpError(
            "too_large", f"the write is {len(data)} bytes and the request allows {max_bytes}"
        )
    expected = envelope.get("expected_hash")
    if not isinstance(expected, str) or not expected.startswith("sha256:"):
        raise FileOpError("invalid_request", "a write must name the sha256 it expects")
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    if digest != expected:
        raise FileOpError(
            "hash_mismatch",
            f"the supplied bytes hash to {digest}, not the expected {expected}",
        )

    root, path = _relative(relative, allow_root=False)
    _ensure_parents(root, relative)
    if os.path.lexists(path):
        existing = os.lstat(path)
        if stat.S_ISLNK(existing.st_mode):
            raise FileOpError(
                "symlink", "the destination is a symbolic link; refusing to replace it"
            )
        if stat.S_ISDIR(existing.st_mode):
            raise FileOpError("not_a_file", "the destination is a directory")
        if stat.S_ISREG(existing.st_mode) and existing.st_nlink > 1:
            raise FileOpError(
                "hardlink",
                "the destination has more than one hard link; replacing it would "
                "replace a name for bytes outside the workspace",
            )
    directory = os.path.dirname(path)
    temporary = os.path.join(directory, f".{os.path.basename(path)}.ravel-{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temporary, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return {
        "ok": True,
        "path": relative,
        "size_bytes": len(data),
        "content_hash": digest,
        "media_type": mimetypes.guess_type(relative)[0] or "application/octet-stream",
    }


def collect_artifacts(
    relative_dir: str, *, max_files: int, max_total_bytes: int, max_file_bytes: int
) -> dict[str, Any]:
    """A manifest of regular files under one workspace directory.

    The directory and every file on the way down are checked by `read_file`
    when the supervisor fetches each one, so this walk only decides what to
    offer. Symlinks and hard-linked files are excluded from the manifest here
    as well, so a refusal is visible in the manifest rather than only in a later
    read.
    """
    root, base = _relative(relative_dir, allow_root=True)
    files: list[dict[str, Any]] = []
    total = 0
    for current_root, directories, names in os.walk(base, followlinks=False):
        directories.sort()
        names.sort()
        for name in names:
            path = os.path.join(current_root, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                continue
            if info.st_size > max_file_bytes:
                continue
            total += int(info.st_size)
            if total > max_total_bytes or len(files) >= max_files:
                raise FileOpError(
                    "too_many_artifacts",
                    f"artifacts exceed the collection limits ({max_files} files, "
                    f"{max_total_bytes} bytes)",
                )
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            files.append(
                {
                    "path": rel,
                    "name": os.path.basename(path),
                    "size_bytes": int(info.st_size),
                    "content_hash": _file_hash(path),
                }
            )
    return {"ok": True, "entries": files, "total_bytes": total}


def _file_hash(path: str) -> str:
    fd, _ = _open_regular(path)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(fd)
    return f"sha256:{digest.hexdigest()}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if not args:
            raise FileOpError("invalid_request", "an operation is required")
        operation = args[0]
        if operation == "list":
            if len(args) != 2:
                raise FileOpError("invalid_request", "list takes one relative path")
            result = list_entries(args[1])
        elif operation == "read":
            if len(args) != 3:
                raise FileOpError("invalid_request", "read takes a path and a byte limit")
            result = read_file(args[1], int(args[2]))
        elif operation == "write":
            if len(args) != 3:
                raise FileOpError("invalid_request", "write takes a path and a byte limit")
            raw = sys.stdin.buffer.read(MAX_FILE_BYTES * 2)
            try:
                envelope = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise FileOpError("invalid_request", "write's stdin is not JSON") from exc
            if not isinstance(envelope, dict):
                raise FileOpError("invalid_request", "write's stdin is not a JSON object")
            result = write_file(args[1], int(args[2]), envelope)
        elif operation == "collect":
            if len(args) != 5:
                raise FileOpError(
                    "invalid_request",
                    "collect takes a directory, max files, max total bytes, max file bytes",
                )
            result = collect_artifacts(
                args[1],
                max_files=int(args[2]),
                max_total_bytes=int(args[3]),
                max_file_bytes=int(args[4]),
            )
        else:
            raise FileOpError("invalid_request", f"unknown operation {operation!r}")
    except FileOpError as exc:
        print(json.dumps({"ok": False, "error_code": exc.code, "error": exc.message}))
        return 1
    except SandboxValidationError as exc:
        print(json.dumps({"ok": False, "error_code": "invalid_path", "error": str(exc)}))
        return 1
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "fileop_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
