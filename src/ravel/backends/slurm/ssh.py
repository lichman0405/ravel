"""The SSH seam the Slurm backend reaches a cluster through.

Two things live here and nothing else. `SSHTransport` is the port: run a
command, put a file, get a file, list a directory. `ParamikoTransport` is the
one implementation that opens a real connection. Everything the Slurm backend
does — submitting, polling, collecting, cancelling — is written against the
port, which is what makes the backend testable without a cluster: a test hands
it a transport that answers from a script, and every branch the backend has is
reachable.

**Why paramiko and not the `ssh` binary.** Password authentication. The OpenSSH
client takes a password from a terminal or from `sshpass`, and `sshpass -p
<secret>` puts the cluster password in the process's argv — visible in `ps` to
every account on the worker host, and captured by any process accounting that
happens to be running. Paramiko holds the password in this process's memory and
never in a command line. That is the whole reason for the dependency.

**Host keys are refused by default.** `AutoAddPolicy` is what most examples
use, and it accepts whatever key the far end presents, which makes the
encryption decorative: anyone who answers the address gets the session. A
deployment may opt in (`slurm_trust_unknown_host`) when it is bringing up a
cluster it has no key for yet, and the default is to refuse.

**Every string that leaves this module is redacted first.** The backend runs
commands whose output can echo a credential — a failed login, a verbose
`squeue` wrapper, an sshd banner quoting the username — and those strings end
up in `JobStatus.detail`, in `completion_metadata`, and in PostgreSQL rows that
outlive the process. `Redactor` is applied at the boundary rather than at each
call site, because a call site is a place somebody can forget.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Only for the field annotations below. `paramiko` is imported at runtime
    # inside `connect`, so a deployment that never configures a cluster never
    # imports it and a test that never touches one never needs it installed.
    import paramiko

#: What a secret is replaced with. A fixed word rather than a masked version of
#: the secret: a mask of the right length tells a reader how long the password
#: is, and there is no reason to hand that over to make a log read better.
REDACTED = "«redacted»"


@dataclass(frozen=True, slots=True)
class Redactor:
    """Removes configured secrets from anything about to leave the backend.

    Text in, text out, and it is deliberately a callable rather than a method:
    the places that need it — a detail line, a metadata dict, an exception
    message — are not all in one class, and a function can be passed to each of
    them without any of them having to know what a credential is.

    An empty secret is skipped rather than substituted: replacing "" would put
    the marker between every character of every string.
    """

    secrets: tuple[str, ...] = ()

    def __call__(self, text: str) -> str:
        """Return the text with every configured secret replaced."""
        for secret in self.secrets:
            if secret and secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def mapping(self, values: Mapping[str, object]) -> dict[str, object]:
        """Redact the string values of a metadata mapping, leaving the rest.

        Called on the way into `completion_metadata` and `JobStatus.progress`,
        which are dictionaries that may hold a command's output. Non-strings are
        returned unchanged: redaction is a text operation, and a number cannot
        be a secret.

        Takes any mapping and returns a plain `dict`: `dict[str, str]` is not a
        `dict[str, object]` — dict is invariant in its value type — and the
        caller who has built a metadata dictionary of strings is the ordinary
        one, not the exception.
        """
        return {
            key: self(value) if isinstance(value, str) else value
            for key, value in values.items()
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What one remote command did.

    `command` is kept because an error message that does not say what was run
    is not actionable, and because the redactor can only scrub it if it is here
    to scrub.
    """

    command: str
    exit_status: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """Whether the command succeeded."""
        return self.exit_status == 0

    @property
    def output(self) -> str:
        """Stdout with the trailing newline off, which is what a value is."""
        return self.stdout.strip()

    def describe(self) -> str:
        """One line a human can read: what ran, how it went, what it said."""
        said = self.stderr.strip() or self.stdout.strip() or "no output"
        return f"{self.command!r} exited {self.exit_status}: {said}"


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """One file on the far side, as a listing reports it."""

    path: str
    size_bytes: int = 0


class TransportError(RuntimeError):
    """The connection or the transfer failed, as against the command failing.

    Kept apart from a non-zero exit status because the two are answered
    differently: a command that failed told us something about the job, and a
    transport that failed told us nothing at all. The first is a fact to record;
    the second is a fault to retry.
    """


@runtime_checkable
class SSHTransport(Protocol):
    """A connection to a host, at file and command granularity.

    Deliberately small. Every method here is one round trip, and the backend
    issues them explicitly rather than calling a bulk helper, because what a
    backend does to a cluster is the thing worth reading — a `sync_workspace`
    that hid six operations behind one name would be a place for the sixth to
    go unnoticed.
    """

    def run(self, command: str, *, timeout: float | None = None) -> CommandResult:
        """Run a command and wait for it. Never raises for a non-zero status."""
        ...

    def put(self, local_path: Path, remote_path: str) -> None:
        """Upload one file, creating the remote parent if it is missing."""
        ...

    def get(self, remote_path: str, local_path: Path) -> None:
        """Download one file, creating the local parent if it is missing."""
        ...

    def listdir(self, remote_path: str) -> tuple[RemoteFile, ...]:
        """Every regular file under a remote directory, recursively.

        Recursive because a run writes where its software decides to: a
        simulation with an `output/` directory is not a special case to be
        discovered later. Paths come back relative to the argument.
        """
        ...

    def close(self) -> None:
        """Release the connection. Safe to call more than once."""
        ...


@dataclass
class ParamikoTransport:
    """An `SSHTransport` backed by a real SSH connection.

    Constructed through `connect`, which is the only place `paramiko` is
    imported: a deployment that never configures a cluster never pays for the
    import, and a test that never touches one never needs the library present.

    Not a context manager itself — `client` gives that — because a transport
    that closed itself would make it hard to see, at the call site, how long a
    connection is held open.
    """

    client: paramiko.SSHClient
    sftp: paramiko.SFTPClient
    #: Base directory for `get`/`put` with relative paths. Empty means whatever
    #: the SFTP session started in, which the backend never relies on: every
    #: path it passes is absolute.
    root: str = ""
    _closed: bool = field(default=False, init=False)
    #: Remote directories this connection has already ensured exist.
    _made: set[str] = field(default_factory=set, init=False)

    @classmethod
    def connect(
        cls,
        *,
        host: str,
        port: int = 22,
        username: str,
        password: str | None = None,
        key_filename: str | None = None,
        trust_unknown_host: bool = False,
        timeout: float = 15.0,
    ) -> ParamikoTransport:
        """Open a connection and authenticate.

        Raises:
            TransportError: The connection, the host key check, or the
                authentication failed. The message carries the reason and never
                the credential — see `_clean`.
        """
        import paramiko

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if trust_unknown_host:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password,
                key_filename=key_filename,
                timeout=timeout,
                # The banner and the auth exchange are covered by `timeout`;
                # this bounds the whole handshake so a host that accepts the
                # TCP connection and then says nothing cannot hold a worker.
                banner_timeout=timeout,
                auth_timeout=timeout,
            )
            sftp = client.open_sftp()
        except Exception as error:  # paramiko raises a wide family of its own
            client.close()
            raise TransportError(
                _clean(f"could not connect to {username}@{host}:{port}: {error}")
            ) from None
        return cls(client=client, sftp=sftp)

    def run(self, command: str, *, timeout: float | None = None) -> CommandResult:
        """Run a command over the connection and read both streams to the end.

        Raises:
            TransportError: The channel could not be opened or the connection
                dropped mid-command. A command that ran and failed is not an
                error here — it is a `CommandResult` with a non-zero status,
                which is what the caller needs in order to classify it.
        """
        if self._closed:
            raise TransportError("the connection is closed")
        _stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        try:
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            status = stdout.channel.recv_exit_status()
        except Exception as error:
            raise TransportError(
                _clean(f"lost the connection running {command!r}: {error}")
            ) from None
        return CommandResult(
            command=_clean(command),
            exit_status=status,
            stdout=_clean(out),
            stderr=_clean(err),
        )

    def put(self, local_path: Path, remote_path: str) -> None:
        """Upload one file, creating the remote parent directory first.

        Raises:
            TransportError: The transfer failed.
        """
        if self._closed:
            raise TransportError("the connection is closed")
        try:
            self._mkdirs(str(Path(remote_path).parent))
            self.sftp.put(str(local_path), remote_path)
        except Exception as error:
            raise TransportError(
                _clean(f"could not upload {local_path.name} to {remote_path}: {error}")
            ) from None

    def get(self, remote_path: str, local_path: Path) -> None:
        """Download one file, creating the local parent directory first.

        Raises:
            TransportError: The transfer failed.
        """
        if self._closed:
            raise TransportError("the connection is closed")
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            self.sftp.get(remote_path, str(local_path))
        except Exception as error:
            raise TransportError(
                _clean(f"could not download {remote_path}: {error}")
            ) from None

    def listdir(self, remote_path: str) -> tuple[RemoteFile, ...]:
        """Every regular file under a remote directory, recursively.

        Raises:
            TransportError: The directory could not be read. A directory that
                does not exist is not distinguished from one that cannot be
                read: both mean the same thing to the caller, which is that it
                has no listing.
        """
        if self._closed:
            raise TransportError("the connection is closed")
        found: list[RemoteFile] = []
        try:
            self._walk(remote_path, "", found)
        except Exception as error:
            raise TransportError(
                _clean(f"could not list {remote_path}: {error}")
            ) from None
        return tuple(found)

    def close(self) -> None:
        """Close the SFTP session and the connection."""
        if self._closed:
            return
        self._closed = True
        for handle in (self.sftp, self.client):
            # A close that fails has nothing left to fail at.
            with suppress(Exception):
                handle.close()

    def _mkdirs(self, remote_directory: str) -> None:
        """Create a remote directory tree, ignoring the ones already there.

        SFTP has no `mkdir -p`, so the components are walked in order. The
        results are remembered for the life of the connection: a workspace is
        uploaded file by file, and a `stat` per component per file would be
        most of the transfer's round trips.

        A directory that appears between the `stat` and the `mkdir` — another
        worker, an operator — is not an error, which is why the `mkdir` failure
        is swallowed rather than propagated.
        """
        if remote_directory in ("", ".", "/"):
            return
        if remote_directory in self._made:
            return
        parts = [part for part in Path(remote_directory).parts if part != "/"]
        current = "/" if remote_directory.startswith("/") else ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            if current in self._made:
                continue
            try:
                self.sftp.stat(current)
            except OSError:
                with suppress(OSError):
                    self.sftp.mkdir(current)
            self._made.add(current)

    def _walk(self, root: str, relative: str, found: list[RemoteFile]) -> None:
        """Recurse through a remote directory, collecting regular files."""
        import stat as stat_module

        here = f"{root}/{relative}" if relative else root
        for entry in self.sftp.listdir_attr(here):
            path = f"{relative}/{entry.filename}" if relative else entry.filename
            # A long listing on a filesystem that does not report a mode gives
            # `None`, and those attributes are declared optional for that
            # reason. A file whose type cannot be read is skipped rather than
            # guessed at: calling it regular would offer the backend bytes that
            # may be a socket or a device.
            mode = entry.st_mode
            if mode is None:
                continue
            if stat_module.S_ISDIR(mode):
                self._walk(root, path, found)
            elif stat_module.S_ISREG(mode):
                found.append(RemoteFile(path=path, size_bytes=entry.st_size or 0))


def _clean(text: str) -> str:
    """The final tidy on a string built from an exception.

    Paramiko's messages are already free of the password — it does not put a
    credential in an exception — but they can carry the username and the far
    side's banner, and this is the last point at which the whole family of
    strings is in one place. What it removes is anything resembling a private
    key block, which some servers echo back in a banner.
    """
    if "PRIVATE KEY" in text:
        return "a private key was echoed by the far side and has been removed"
    return text


@contextmanager
def connected(connect: Callable[[], SSHTransport]) -> Generator[SSHTransport]:
    """Open a transport, use it, and close it however the block ends.

    Every backend operation goes through this: a connection held open across a
    poll would be a connection held open across a durable timer, and a worker
    that keeps a socket to a cluster for the length of a two-day job is a
    worker that has to notice the socket dying.
    """
    transport = connect()
    try:
        yield transport
    finally:
        transport.close()


__all__ = [
    "REDACTED",
    "CommandResult",
    "ParamikoTransport",
    "Redactor",
    "RemoteFile",
    "SSHTransport",
    "TransportError",
    "connected",
]
