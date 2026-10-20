"""The Docker CLI, invoked as a subprocess by the supervisor and nowhere else.

Only `ravel.sandbox.supervisor` imports this module, and only the supervisor
process holds the Docker socket. The RAVEL API, the DSH host, and every agent
container reach sandboxes through `ravel.sandbox.provider`, which speaks HTTP
to the supervisor and contains no Docker client at all. That split is the point
of the service: a compromised agent can be refused a socket by a compose file,
but it cannot be refused one that was never mounted anywhere but the supervisor.

Why the CLI rather than the Python SDK: the supervisor image then needs only
the Docker CLI and no second client library whose version must track the
daemon, and every Docker action is an argv list that a test can read. The
arguments are built by `supervisor.py`; this module performs them, bounds them
with a timeout, and redacts their diagnostics before they travel further.

`subprocess` here is not a security boundary by itself. The socket is. The
argv-list construction (never `shell=True`) is what keeps an argument that came
from a request from becoming a second command.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO, Any

from ravel.sandbox.redaction import SecretRedactor


class DockerError(RuntimeError):
    """A Docker command failed.

    The message carries the redacted tail of stderr, which is what makes a
    failed provisioning diagnosable without carrying a credential into a run
    record.
    """

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.args_list = tuple(args)
        self.returncode = returncode
        self.stderr = stderr
        # `args_list` includes the binary, so the first element is always
        # "docker". Naming the subcommand ("volume", "run", ...) is what makes
        # a timeout diagnosable from the provider's error message.
        parts = [part for part in args if not part.startswith("-")]
        label = " ".join(parts[:2])
        super().__init__(f"docker {label} failed ({returncode}): {stderr}")


class DockerTimeout(DockerError):
    """A Docker command did not finish inside its bound."""


class DockerUnavailable(DockerError):
    """The Docker daemon or CLI is not reachable from the supervisor."""

    def __init__(self, message: str) -> None:
        self.args_list = ()
        self.returncode = 127
        self.stderr = message
        RuntimeError.__init__(self, message)


@dataclass(frozen=True, slots=True)
class DockerResult:
    """One completed Docker CLI call."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class DockerEngine:
    """A bounded, non-shell invocation of the Docker CLI."""

    def __init__(
        self,
        binary: str = "docker",
        *,
        default_timeout: float = 60.0,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.binary = binary
        self.default_timeout = default_timeout
        self.redactor = redactor or SecretRedactor()

    # ── primitives ────────────────────────────────────────────────────────

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: float | None = None,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> DockerResult:
        """Run one Docker CLI command and return its decoded output.

        Raises:
            DockerTimeout: The bound expired. The child is killed; a Docker CLI
                killed mid-exec does not necessarily kill a container process,
                which is why the supervisor kills by pid as well.
            DockerError: The command exited non-zero and `check` is set.
            DockerUnavailable: The binary is missing.
        """
        argv = [self.binary, *args]
        bound = self.default_timeout if timeout is None else timeout
        try:
            completed = subprocess.run(
                argv,
                input=input_bytes,
                capture_output=True,
                timeout=bound,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DockerUnavailable(
                f"the Docker CLI {self.binary!r} is not installed in the supervisor "
                "container; the supervisor owns the only Docker socket and cannot "
                "run sandboxes without it"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            stderr = _decode(exc.stderr)[-2000:]
            raise DockerTimeout(argv, 124, self.redactor.redact(stderr or "timed out")) from exc
        stdout = _decode(completed.stdout)
        stderr = self.redactor.redact(_decode(completed.stderr))
        if check and completed.returncode != 0:
            raise DockerError(
                argv, completed.returncode, self.redactor.redact(stderr[-4000:] or stdout[-4000:])
            )
        return DockerResult(tuple(argv), completed.returncode, stdout, stderr)

    def popen(
        self,
        args: Sequence[str],
        *,
        stdout: IO[bytes],
        stderr: IO[bytes],
        input_bytes: bytes | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start a Docker CLI command with output going to files.

        Used for a sandbox container's `docker exec`, where the output may be
        large and the supervisor must enforce its own wall clock while retaining
        a bounded prefix for the record. `communicate` is the caller's job.
        """
        argv = [self.binary, *args]
        try:
            return subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
            )
        except FileNotFoundError as exc:
            raise DockerUnavailable(
                f"the Docker CLI {self.binary!r} is not installed in the supervisor container"
            ) from exc

    def available(self) -> bool:
        """Whether the daemon answers, without raising on a missing socket."""
        try:
            result = self.run(["version", "--format", "{{.Server.Version}}"], timeout=10)
        except DockerError:
            return False
        return result.returncode == 0 and bool(result.stdout.strip())

    def server_version(self) -> str:
        return self.run(["version", "--format", "{{.Server.Version}}"], timeout=10).stdout.strip()

    # ── images ────────────────────────────────────────────────────────────

    def image_present(self, image: str) -> bool:
        result = self.run(["image", "inspect", image], timeout=15, check=False)
        return result.returncode == 0

    def pull(self, image: str, *, timeout: float = 600.0) -> None:
        self.run(["pull", image], timeout=timeout)

    def build(self, context: str, dockerfile: str, tag: str, *, timeout: float = 900.0) -> None:
        self.run(
            ["build", "--file", dockerfile, "--tag", tag, context],
            timeout=timeout,
        )

    # ── volumes ───────────────────────────────────────────────────────────

    def volume_create(self, name: str, *, label: str = "ravel.sandbox=1") -> None:
        # Setup commands are bounded, but not tightly: they run while another
        # build or test may be using the same daemon, and a 20-second bound
        # turns daemon contention into a spurious provisioning failure.
        self.run(["volume", "create", "--label", label, name], timeout=60)

    def volume_remove(self, name: str) -> None:
        self.run(["volume", "rm", "--force", name], timeout=20, check=False)

    def volume_present(self, name: str) -> bool:
        return self.run(["volume", "inspect", name], timeout=15, check=False).returncode == 0

    def prepare_volume(self, image: str, volume: str, *, owner: str = "65534:65534") -> None:
        """Make a fresh volume writable by the sandbox uid.

        A Docker volume is created root-owned. The sandbox runs as 65534 and
        cannot chown the mount point itself. This one-shot trusted helper is
        the only container the supervisor starts as root; it has no network, no
        capabilities beyond `CHOWN`, no project data beyond the empty volume,
        and exits immediately.
        """
        self.run(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--security-opt",
                "no-new-privileges",
                "--mount",
                f"type=volume,source={volume},target=/workspace",
                "--entrypoint",
                "/bin/chown",
                image,
                "-R",
                owner,
                "/workspace",
            ],
            timeout=120,
        )

    # ── networks ──────────────────────────────────────────────────────────

    def network_present(self, name: str) -> bool:
        return self.run(["network", "inspect", name], timeout=15, check=False).returncode == 0

    def network_create(
        self, name: str, *, internal: bool, subnet: str | None = None
    ) -> None:
        args = ["network", "create"]
        if internal:
            args.append("--internal")
        if subnet:
            args.extend(["--subnet", subnet])
        args.append(name)
        self.run(args, timeout=60)

    def network_connect(self, network: str, container: str) -> None:
        self.run(["network", "connect", network, container], timeout=60)

    def network_remove(self, name: str) -> None:
        self.run(["network", "rm", name], timeout=20, check=False)

    def network_subnet(self, name: str) -> str | None:
        """The IPv4 subnet Docker assigned to a network, for the proxy's client check."""
        result = self.run(
            [
                "network",
                "inspect",
                "--format",
                "{{range .IPAM.Config}}{{.Subnet}} {{end}}",
                name,
            ],
            timeout=15,
        )
        for subnet in result.stdout.split():
            if ":" not in subnet:
                return subnet
        return None

    # ── containers ────────────────────────────────────────────────────────

    def container_present(self, name: str) -> bool:
        return self.run(["container", "inspect", name], timeout=15, check=False).returncode == 0

    def container_running(self, name: str) -> bool:
        result = self.run(
            ["container", "inspect", "--format", "{{.State.Running}}", name],
            timeout=15,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def inspect(self, name: str) -> dict[str, Any]:
        """The container's inspection document as JSON.

        Read only by diagnostics and by the isolation tests, which assert the
        flags the container actually has rather than the flags the argv meant to
        set.
        """
        import json

        result = self.run(["container", "inspect", name], timeout=15)
        try:
            document = json.loads(result.stdout)
        except ValueError as exc:
            raise DockerError(result.args, 0, "docker inspect returned non-JSON") from exc
        if not isinstance(document, list) or not document:
            raise DockerError(result.args, 0, "docker inspect returned no container")
        return document[0]

    def create(self, args: Sequence[str]) -> str:
        """Create a container and return its id."""
        return self.run(["create", *args], timeout=120).stdout.strip()

    def start(self, name: str) -> None:
        self.run(["start", name], timeout=120)

    def stop(self, name: str, *, timeout: int = 5) -> None:
        self.run(["stop", "--time", str(timeout), name], timeout=timeout + 20, check=False)

    def remove(self, name: str, *, force: bool = True) -> None:
        args = ["rm"]
        if force:
            args.append("--force")
        args.append(name)
        self.run(args, timeout=60, check=False)

    def logs(self, name: str, *, tail: int = 200) -> str:
        """The container's log tail, redacted. Ownership is the supervisor's."""
        result = self.run(["logs", "--tail", str(tail), name], timeout=20, check=False)
        return self.redactor.redact(result.stdout + result.stderr)

    def exec_capture(
        self,
        container: str,
        argv: Sequence[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        input_bytes: bytes | None = None,
        user: str | None = None,
    ) -> DockerResult:
        """Run one command in a container and capture bounded output.

        `argv` is passed as separate arguments after the container name. Docker
        exec runs them without a shell, so a command argument that contains a
        semicolon is a filename, not a second command.
        """
        args = ["exec"]
        if input_bytes is not None:
            args.append("--interactive")
        if user:
            args.extend(["--user", user])
        if workdir:
            args.extend(["--workdir", workdir])
        for name, value in (env or {}).items():
            args.extend(["--env", f"{name}={value}"])
        args.append(container)
        args.extend(argv)
        result = self.run(args, timeout=timeout, input_bytes=input_bytes, check=False)
        if result.returncode != 0:
            # Callers of fileops parse the JSON error; callers of a command
            # care about the exit status, which is the command's own.
            pass
        return result


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def docker_binary_available(binary: str = "docker") -> bool:
    """Whether the CLI exists on PATH. Used by preflight and tests."""
    return shutil.which(binary) is not None
