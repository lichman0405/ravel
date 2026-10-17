"""One system-wide lock for every process that truncates the test database.

The premium task runner, the legacy release gate, and a direct ``pytest`` run
all reach the same PostgreSQL test database, and every integration test starts
by truncating it. They must therefore share one lock, not merely one naming
convention.

The default directory is derived from Git's common directory, not from a
worktree's path: every worktree of one repository resolves to the same
``<repo-parent>/.ravel-test-locks``. The container sees that directory at
``/workspace/.ravel-test-locks`` when the host checkout is mounted at
``/workspace``. ``RAVEL_PREMIUM_LOCK_DIR`` overrides it for a deliberate
deployment. A shared directory that cannot be written is a hard error: falling
back to a per-process temporary directory would silently remove the exclusion
this module exists to provide.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path

__all__ = ["DatabaseLock", "database_lock_path", "lock_dir"]

#: A process that composes several session fixtures may construct two
#: ``DatabaseLock`` objects for the same path. The OS lock is not reentrant, so
#: without this table the second one waits on its own process forever. The
#: table only makes the lock reentrant *within one process*; the file lock
#: still excludes every other process.
_PROCESS_LOCKS: dict[Path, int] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _repo_root() -> Path:
    # src/ravel/state/db_lock.py -> src -> repo root.
    return Path(__file__).resolve().parents[3]


def _git_common_dir() -> Path:
    """The repository's common Git directory, shared by every worktree."""
    entry = _repo_root() / ".git"
    if entry.is_dir():
        return entry.resolve()
    if not entry.is_file():
        raise RuntimeError(
            f"cannot locate the Git directory for the database test lock: {entry}"
        )
    line = entry.read_text(encoding="utf-8").strip()
    if not line.startswith("gitdir:"):
        raise RuntimeError(f"{entry} does not name a Git directory")
    gitdir = Path(line.partition(":")[2].strip()).expanduser()
    if not gitdir.is_absolute():
        gitdir = entry.parent / gitdir
    gitdir = gitdir.resolve()
    # A linked worktree's gitdir is <common>/worktrees/<name>.
    if gitdir.parent.name != "worktrees":
        raise RuntimeError(f"unexpected linked-worktree Git directory: {gitdir}")
    return gitdir.parent.parent


def _shared_parent() -> Path:
    # The common directory is <repo>/.git for the main checkout and for every
    # linked worktree, so its grandparent is the host directory the container
    # mounts at /workspace.
    return _git_common_dir().parent.parent


def lock_dir(env: Mapping[str, str] | None = None) -> Path:
    """The one shared lock directory for this repository's test databases.

    The directory is created on demand. A non-writable shared directory is an
    error rather than a fallback: a process that quietly locks a private
    directory has no exclusion at all.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    configured = source.get("RAVEL_PREMIUM_LOCK_DIR")
    if configured:
        directory = Path(configured).expanduser().resolve()
    else:
        directory = (_shared_parent() / ".ravel-test-locks").resolve()

    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".probe-{os.getpid()}-{time.time_ns()}"
        try:
            probe.write_text("", encoding="utf-8")
        finally:
            with contextlib.suppress(FileNotFoundError):
                probe.unlink()
    except OSError as error:
        raise RuntimeError(
            f"database test lock directory {directory} is not writable; refusing "
            "to fall back to a non-shared directory"
        ) from error
    return directory


def database_lock_path(env: Mapping[str, str] | None = None) -> Path:
    source: Mapping[str, str] = os.environ if env is None else env
    host = source.get("RAVEL_POSTGRES_HOST", "127.0.0.1").strip().lower().strip("[]")
    if host in {"localhost", "::1"}:
        host = "127.0.0.1"
    # The integration fixture selects the test database from RAVEL_TEST_DB and
    # refuses any name without the _test suffix. RAVEL_POSTGRES_DB is the
    # deployment database when .env exists, so using it here would give the
    # release gate and a direct pytest different locks for the same database.
    identity = "{host}:{port}/{db}".format(
        host=host,
        port=int(source.get("RAVEL_POSTGRES_PORT", "55432")),
        db=source.get("RAVEL_TEST_DB", "ravel_test").strip().lower(),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return lock_dir(source) / f"database-{digest}.lock"


class DatabaseLock:
    """A blocking, cross-process exclusive lock for database test suites.

    A second process waits (the default) or raises ``TimeoutError`` when its
    bounded wait expires. It never begins a TRUNCATE without the lock: the
    integration ``database`` fixture acquires this before it connects.

    ``RAVEL_PREMIUM_LOCK_TIMEOUT`` bounds the wait for callers that construct
    the lock without an explicit timeout. It exists for the competition tests
    that must prove a second entry point is refused, not for ordinary runs.
    """

    DEFAULT_TIMEOUT = 1800.0

    def __init__(
        self, path: Path | None = None, timeout: float | None = None
    ) -> None:
        self.path = path or database_lock_path()
        if timeout is None:
            timeout = float(
                os.environ.get(
                    "RAVEL_PREMIUM_LOCK_TIMEOUT", str(self.DEFAULT_TIMEOUT)
                )
            )
        self.timeout = timeout
        self._handle: object | None = None
        self._entered = False

    @staticmethod
    def _contention(error: OSError) -> bool:
        return error.errno in {
            errno.EACCES,
            errno.EAGAIN,
            errno.EWOULDBLOCK,
            errno.EDEADLK,
        }

    def __enter__(self) -> DatabaseLock:
        with _PROCESS_LOCKS_GUARD:
            if self.path in _PROCESS_LOCKS:
                _PROCESS_LOCKS[self.path] += 1
                self._entered = True
                return self

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = open(self.path, "a+", encoding="utf-8")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self._acquire()
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"timed out waiting for database test lock {self.path}"
                        ) from None
                    time.sleep(0.25)
                else:
                    with _PROCESS_LOCKS_GUARD:
                        _PROCESS_LOCKS[self.path] = 1
                    self._entered = True
                    return self
        except BaseException:
            self._close()
            raise

    def _acquire(self) -> None:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows development only
            import msvcrt

            assert self._handle is not None
            self._handle.seek(0)  # type: ignore[union-attr]
            try:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[union-attr]
            except OSError as error:
                if self._contention(error):
                    raise BlockingIOError(str(error)) from error
                raise
            return

        assert self._handle is not None
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[union-attr]
        except OSError as error:
            if self._contention(error):
                raise BlockingIOError(str(error)) from error
            raise

    def _close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows development only
            import msvcrt

            handle.seek(0)  # type: ignore[union-attr]
            with contextlib.suppress(OSError):
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
        else:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
        handle.close()  # type: ignore[union-attr]

    def __exit__(self, *exc: object) -> None:
        if not self._entered:
            return
        self._entered = False
        with _PROCESS_LOCKS_GUARD:
            depth = _PROCESS_LOCKS.get(self.path, 0)
            if depth > 1:
                _PROCESS_LOCKS[self.path] = depth - 1
                return
            _PROCESS_LOCKS.pop(self.path, None)
        self._close()
