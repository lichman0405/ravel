"""One system-wide lock for every process that truncates the test database.

The premium task runner, the legacy release gate, and a direct ``pytest`` run
all reach the same PostgreSQL test database, and every integration test starts
by truncating it. They must therefore share one lock, not merely one naming
convention.

The default directory is chosen from the repository layout so that a host
``pytest`` run, a WSL run and a container run of the same checkout meet at the
same file:

* a task worktree is ``<shared>/ravel-worktrees/Txx``, so ``<shared>`` is its
  grandparent;
* the main checkout is ``<shared>/ravel``, so ``<shared>`` is its parent.

Both resolve to the same ``<shared>/.ravel-test-locks``. The container sees
the same directory at ``/workspace/.ravel-test-locks``. ``RAVEL_PREMIUM_LOCK_DIR``
overrides the directory; a non-writable shared directory falls back to the
platform temporary directory.
"""

from __future__ import annotations

import errno
import hashlib
import os
import time
from pathlib import Path

__all__ = ["DatabaseLock", "database_lock_path", "lock_dir"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _shared_parent() -> Path:
    root = _repo_root()
    if root.parent.name == "ravel-worktrees":
        return root.parent.parent
    return root.parent


def lock_dir(env: dict[str, str] | None = None) -> Path:
    """The shared lock directory, or a writable fallback.

    The directory is created on demand by ``DatabaseLock``. The function is
    deterministic for a given checkout so two processes compute the same path.
    """
    env = os.environ if env is None else env
    configured = env.get("RAVEL_PREMIUM_LOCK_DIR")
    if configured:
        return Path(configured).expanduser().resolve()

    shared = (_shared_parent() / ".ravel-test-locks").resolve()
    try:
        shared.mkdir(parents=True, exist_ok=True)
        probe = shared / ".probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return shared
    except OSError:
        return Path(os.path.abspath(os.sep + "tmp")) / "ravel-premium-locks"


def database_lock_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    host = env.get("RAVEL_POSTGRES_HOST", "127.0.0.1").strip().lower().strip("[]")
    if host in {"localhost", "::1"}:
        host = "127.0.0.1"
    identity = "{host}:{port}/{db}".format(
        host=host,
        port=int(env.get("RAVEL_POSTGRES_PORT", "55432")),
        db=env.get("RAVEL_POSTGRES_DB", "ravel_test").strip().lower(),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return lock_dir(env) / f"database-{digest}.lock"


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

    @staticmethod
    def _contention(error: OSError) -> bool:
        return error.errno in {
            errno.EACCES,
            errno.EAGAIN,
            errno.EWOULDBLOCK,
            errno.EDEADLK,
        }

    def __enter__(self) -> DatabaseLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._acquire()
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._close()
                    raise TimeoutError(
                        f"timed out waiting for database test lock {self.path}"
                    ) from None
                time.sleep(0.25)
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
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
            except OSError:
                pass
        else:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
            except OSError:
                pass
        handle.close()  # type: ignore[union-attr]

    def __exit__(self, *exc: object) -> None:
        self._close()
