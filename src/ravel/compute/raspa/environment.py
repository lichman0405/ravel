"""Whether this host may start a RASPA calculation at all.

A package can be perfect and still not be runnable, and the difference is a
host fact: the binary is absent, it is a different version with different input
syntax, the MIT license file that makes the pinned data redistributable is
missing, or the bundled reference data has been edited. Each of those is a
refusal *before* a run, returned to Master, rather than a job that fails five
retries into an allocation.

**Why the license is probed.** The package copies the official RASPA2 data, and
the permission to do that is the license text pinned beside it. A deployment
whose copy of that text is absent cannot claim the redistribution its manifest
describes, and a scientific package is not allowed to be silent about the terms
its inputs arrive under.

**What a successful probe records.** The exact command, the reported version,
the binary's SHA-256 when a path is available, every bundled file's digest, and
the license digest. Those values go into the result document's environment
section when a real run happens, so a result names the binary that produced it
rather than only the version string it printed.

T10 does not run anything. The probe exists so that T25's run has a gate to
pass, and so that the gate itself is testable without a cluster.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ravel.compute.raspa.provenance import (
    BUNDLED_DATA_ROOT,
    BundledDataError,
    read_bundled,
)
from ravel.compute.raspa.route import SUPPORTED_VERSION_SERIES
from ravel.domain.preparation import PreparationCheck

__all__ = [
    "CommandResult",
    "EnvironmentProbe",
    "ProbeRefusal",
    "RaspaStartRefused",
    "probe_environment",
]

PROBE_TIMEOUT_SECONDS = 60.0

#: The RASPA invocation that prints a version line and exits. `simulate -v`
#: prints `RASPA 2.0.41 (2021)` on stderr and exits 0 in the official build.
DEFAULT_COMMAND: tuple[str, ...] = ("simulate", "-v")

_VERSION = re.compile(r"\bRASPA\s+(\d+)\.(\d+)(?:\.(\d+))?\b")

#: The two phrases that establish a file is the MIT license text rather than,
#: say, an empty file or a different license with the same name.
_LICENSE_MARKERS: tuple[str, ...] = (
    "The MIT License",
    "Permission is hereby granted",
)

#: The files the package copies, by pinned source key. The license is checked
#: separately because it is not copied into the workspace.
_DATA_KEYS: tuple[str, ...] = (
    "official-simulation-input",
    "framework-mfi-si",
    "force-field-example-zeolites",
    "pseudo-atoms-example-zeolites",
    "molecule-methane",
)


class ProbeRefusal(StrEnum):
    """Why a host cannot start the route, from the closed list.

    The members are distinct next steps: install software, install a supported
    release, restore the license record, repair the bundled data, or restore
    the binary the manifest was built against.
    """

    SOFTWARE_MISSING = "SOFTWARE_MISSING"
    PROBE_FAILED = "PROBE_FAILED"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    LICENSE_UNAVAILABLE = "LICENSE_UNAVAILABLE"
    DATA_MISSING = "DATA_MISSING"
    DATA_MISMATCH = "DATA_MISMATCH"
    BINARY_DIGEST_MISMATCH = "BINARY_DIGEST_MISMATCH"


class RaspaStartRefused(Exception):
    """A host that may not start the run, and what it lacks.

    `return_to` is always Master: a missing binary is not a Worker's fault, a
    retry will not install one, and the decision — wait, fix the host, or run
    elsewhere — is Master's, not the activity's.
    """

    return_to = "MASTER"

    def __init__(self, code: ProbeRefusal, reason: str) -> None:
        if not isinstance(code, ProbeRefusal):
            raise TypeError(
                f"a start refusal needs one of {[member.value for member in ProbeRefusal]}, "
                f"got {code!r}; the class decides which host repair Master asks for"
            )
        if not reason.strip():
            raise ValueError("a start refusal must say what the host lacks")
        super().__init__(reason)
        self.code = code
        self.reason = reason

    def document(self) -> dict[str, str]:
        return {
            "code": self.code.value,
            "reason": self.reason,
            "return_to": self.return_to,
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What a version probe wrote, and how it exited."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class EnvironmentProbe:
    """A host that passed every check, with the evidence of having passed."""

    command: tuple[str, ...]
    version: str
    version_series: str
    binary_path: str
    binary_sha256: str | None
    license_sha256: str
    data_sha256: dict[str, str] = field(default_factory=dict)
    checks: tuple[PreparationCheck, ...] = ()

    def to_document(self) -> dict[str, object]:
        """The probe as a result document carries it."""
        return {
            "command": list(self.command),
            "version": self.version,
            "version_series": self.version_series,
            "binary_path": self.binary_path,
            "binary_sha256": self.binary_sha256,
            "license_sha256": self.license_sha256,
            "data_sha256": dict(sorted(self.data_sha256.items())),
        }


def _run_command(command: tuple[str, ...]) -> CommandResult:
    completed = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    return CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _license_bytes(license_path: Path | None) -> bytes:
    if license_path is None:
        return read_bundled("raspa2-license")
    path = Path(license_path)
    if not path.is_file():
        raise RaspaStartRefused(
            ProbeRefusal.LICENSE_UNAVAILABLE,
            f"the license record {path} does not exist; the package copies "
            "MIT-licensed official data and names the terms it arrives under",
        )
    return path.read_bytes()


def _check_license(license_path: Path | None) -> str:
    try:
        data = _license_bytes(license_path)
    except BundledDataError as error:
        raise RaspaStartRefused(
            ProbeRefusal.LICENSE_UNAVAILABLE,
            f"the pinned RASPA2 license record is unavailable: {error.reason}",
        ) from error
    text = data.decode("utf-8", errors="replace")
    absent = [marker for marker in _LICENSE_MARKERS if marker not in text]
    if absent:
        raise RaspaStartRefused(
            ProbeRefusal.LICENSE_UNAVAILABLE,
            f"the license record at {license_path or 'the bundled COPYING'} does "
            f"not contain {', '.join(absent)}; it is not the MIT text the package "
            "redistributes its inputs under",
        )
    return hashlib.sha256(data).hexdigest()


def _check_data(data_root: Path | None) -> dict[str, str]:
    root = Path(data_root) if data_root is not None else BUNDLED_DATA_ROOT
    digests: dict[str, str] = {}
    for key in _DATA_KEYS:
        try:
            data = read_bundled(key, root=root)
        except BundledDataError as error:
            code = (
                ProbeRefusal.DATA_MISMATCH
                if error.kind == "MISMATCH"
                else ProbeRefusal.DATA_MISSING
            )
            raise RaspaStartRefused(
                code, f"the reference data is not usable: {error.reason}"
            ) from error
        digests[key] = hashlib.sha256(data).hexdigest()
    return digests


def _binary_path(binary: str | Path | None, command: tuple[str, ...]) -> str:
    if binary is not None:
        return str(Path(binary))
    found = shutil.which(command[0])
    return found or command[0]


def _digest_binary(path: str) -> str | None:
    candidate = Path(path)
    if not candidate.is_file():
        return None
    return hashlib.sha256(candidate.read_bytes()).hexdigest()


def probe_environment(
    *,
    binary: str | Path | None = None,
    command: tuple[str, ...] | None = None,
    runner: Callable[[tuple[str, ...]], CommandResult] | None = None,
    data_root: Path | None = None,
    license_path: Path | None = None,
    expected_binary_sha256: str | None = None,
) -> EnvironmentProbe:
    """Prove this host can run the pinned route, or refuse with the reason.

    Raises:
        RaspaStartRefused: The license, the bundled data, the binary, or its
            version is not what the package requires.
    """
    license_sha256 = _check_license(license_path)
    data_sha256 = _check_data(data_root)

    invocation = command or (
        (str(binary), "-v") if binary is not None else DEFAULT_COMMAND
    )
    path = _binary_path(binary, invocation)
    run = runner or _run_command
    try:
        result = run(invocation)
    except FileNotFoundError as error:
        raise RaspaStartRefused(
            ProbeRefusal.SOFTWARE_MISSING,
            f"the RASPA executable {invocation[0]!r} is not present on this host; "
            "a package is never started against a binary nobody can name",
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RaspaStartRefused(
            ProbeRefusal.PROBE_FAILED,
            f"{' '.join(invocation)!r} did not answer within "
            f"{PROBE_TIMEOUT_SECONDS:g} s; a probe that hangs is a host fault",
        ) from error
    except OSError as error:
        raise RaspaStartRefused(
            ProbeRefusal.SOFTWARE_MISSING,
            f"the RASPA executable {invocation[0]!r} could not be executed: {error}",
        ) from error
    if result.exit_code != 0:
        raise RaspaStartRefused(
            ProbeRefusal.PROBE_FAILED,
            f"{' '.join(invocation)!r} exited {result.exit_code}; the version "
            "probe is how the run proves which software will execute it",
        )
    match = _VERSION.search(result.stdout + "\n" + result.stderr)
    if match is None:
        raise RaspaStartRefused(
            ProbeRefusal.VERSION_MISMATCH,
            f"{' '.join(invocation)!r} printed no RASPA version; the route is "
            f"pinned to {SUPPORTED_VERSION_SERIES}.x and cannot run on a binary "
            "whose identity it cannot read",
        )
    series = f"{match.group(1)}.{match.group(2)}"
    version = match.group(0).split()[-1]
    if series != SUPPORTED_VERSION_SERIES:
        raise RaspaStartRefused(
            ProbeRefusal.VERSION_MISMATCH,
            f"this host runs RASPA {version}, and the package's input syntax is "
            f"verified only against {SUPPORTED_VERSION_SERIES}.x; a 2.1 binary is "
            "a different program whose keywords this package has not checked",
        )

    binary_sha256 = _digest_binary(path)
    if expected_binary_sha256 is not None:
        if binary_sha256 is None:
            raise RaspaStartRefused(
                ProbeRefusal.BINARY_DIGEST_MISMATCH,
                f"the manifest names binary digest {expected_binary_sha256}, and "
                f"the executable at {path!r} is not a readable file; the run "
                "cannot be proven to use the binary the package was validated on",
            )
        if binary_sha256 != expected_binary_sha256:
            raise RaspaStartRefused(
                ProbeRefusal.BINARY_DIGEST_MISMATCH,
                f"the executable at {path!r} hashes to {binary_sha256}, not the "
                f"recorded {expected_binary_sha256}; a different binary is a "
                "different calculation",
            )

    return EnvironmentProbe(
        command=invocation,
        version=version,
        version_series=series,
        binary_path=path,
        binary_sha256=binary_sha256,
        license_sha256=license_sha256,
        data_sha256=data_sha256,
        checks=(
            PreparationCheck(
                name="license_available",
                passed=True,
                detail=f"MIT text sha256 {license_sha256}",
            ),
            PreparationCheck(
                name="reference_data_pinned",
                passed=True,
                detail=", ".join(f"{key} {digest}" for key, digest in sorted(data_sha256.items())),
            ),
            PreparationCheck(
                name="software_version_supported",
                passed=True,
                detail=f"RASPA {version}; series {series}",
            ),
            PreparationCheck(
                name="binary_digest_recorded",
                passed=True,
                detail=binary_sha256 or f"no readable file at {path}",
            ),
        ),
    )
