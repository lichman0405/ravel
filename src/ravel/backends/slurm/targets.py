"""The allowlist of HPC targets, and the references that reach their secrets.

A deployment does not hand a model a hostname, a username and a password. It
publishes a fixed list of targets — `schemas/premium/hpc_target.schema.json` —
and a run names one by `target_id`. Everything in this module is the
operator's side of that decision: which targets exist, which of them is
enabled, which software each is allowed to run, and how many jobs may be in
flight at once.

**References, not secrets.** An `HpcTarget` holds `credential_ref` and
`known_hosts_ref`, which are names of places a secret or a host-key file lives
(`env:NAME`, `file:/path`, `key:/path`). The secret itself is resolved by
`resolve_target`, and a deployment calls that in the execution process, at the
moment it builds the backend. Nothing here reads the environment at import
time, so a document can be validated, logged, serialized into an evidence
manifest, or shown in a configuration listing without a credential ever being
in hand.

**A fixed list, not a lookup.** `TargetRegistry` is constructed from documents
and answers only for ids it was given. An id that is not in the list is an
error, not a directive to go and find a host: a model that could name an
arbitrary target id would be choosing which cluster to spend, and the
allowlist is what makes that choice the operator's.

The validation here mirrors the JSON schema field for field. It is written by
hand rather than by importing `jsonschema` because the schema is small and the
messages are what an operator reads when a target file is wrong; a generic
validator's message names a JSON pointer, and this one names the field and the
reason.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

from ravel.backends.slurm.backend import SlurmConfigurationError, SlurmTarget

#: The only target kind V0 understands. Spelled as the schema spells it, so a
#: document copied from the schema needs no translation.
SLURM_SSH = "SLURM_SSH"

#: Keys the schema allows. A document with any other key is refused rather
#: than ignored: a misspelled `max_job` would otherwise silently leave the
#: default in place, which is the kind of configuration error that only shows
#: up as too much money spent.
_REQUIRED = (
    "target_id",
    "kind",
    "host",
    "port",
    "username",
    "known_hosts_ref",
    "credential_ref",
    "remote_root",
    "allowed_software",
    "max_jobs",
    "enabled",
)
_OPTIONAL = (
    "partition",
    "account",
    "cpu_limit",
    "gpu_limit",
    "hour_limit",
)
_KNOWN = frozenset((*_REQUIRED, *_OPTIONAL))

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: Slurm's partition, account and other scheduler-facing names are single
#: tokens. Restricting them is not cosmetic: they are interpolated into an
#: `sbatch` command line, and a value with a newline would be a second command.
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")
_SOFTWARE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._@/-]*$")
_CREDENTIAL_SCHEMES = ("env:", "file:", "key:", "keyfile:")
_KNOWN_HOSTS_SCHEMES = ("file:",)


class TargetConfigurationError(SlurmConfigurationError):
    """A target document is missing a field, has a field it may not, or is wrong.

    A subclass of `SlurmConfigurationError` because the consequence is the
    same: the deployment cannot reach the cluster it was told to reach, and
    the failure belongs at start-up rather than in the middle of a project.
    """


class UnknownHpcTargetError(TargetConfigurationError):
    """A run named a target id this deployment does not have."""


class DisabledHpcTargetError(TargetConfigurationError):
    """A configured target was named but is switched off."""


class CredentialResolutionError(TargetConfigurationError):
    """A credential or known-hosts reference could not be resolved.

    The message names the *reference* and never a value: an error is a string
    that ends up in a log file, and the one string that must not go there is
    the secret it was trying to read.
    """


@dataclass(frozen=True, slots=True)
class HpcTarget:
    """One operator-provisioned Slurm-over-SSH target.

    The fields are the schema's, and the two reference fields are opaque here.
    `resolve_target` is the only function that turns them into a password or a
    key path, and it is called in the execution process.
    """

    target_id: str
    host: str
    username: str
    known_hosts_ref: str
    credential_ref: str
    remote_root: str = "/ravel/jobs"
    port: int = 22
    partition: str | None = None
    account: str | None = None
    allowed_software: tuple[str, ...] = ()
    max_jobs: int = 1
    enabled: bool = True
    cpu_limit: int | None = None
    gpu_limit: int | None = None
    hour_limit: float | None = None
    kind: str = SLURM_SSH

    def __post_init__(self) -> None:
        _uuid(self.target_id, "target_id")
        if self.kind != SLURM_SSH:
            raise TargetConfigurationError(
                f"target {self.target_id} has kind {self.kind!r}; V0 understands "
                f"only {SLURM_SSH!r}"
            )
        _text(self.host, "host", maximum=253)
        _text(self.username, "username", maximum=128)
        _ref(self.known_hosts_ref, "known_hosts_ref", _KNOWN_HOSTS_SCHEMES)
        _ref(self.credential_ref, "credential_ref", _CREDENTIAL_SCHEMES)
        _absolute_root(self.remote_root)
        _integer(self.port, "port", minimum=1, maximum=65535)
        _optional_token(self.partition, "partition", maximum=64)
        _optional_token(self.account, "account", maximum=128)
        for name in self.allowed_software:
            if not isinstance(name, str) or not _SOFTWARE.fullmatch(name):
                raise TargetConfigurationError(
                    f"target {self.target_id} names software {name!r}; an "
                    "allowed_software entry is one token"
                )
        _integer(self.max_jobs, "max_jobs", minimum=1)
        if not isinstance(self.enabled, bool):
            raise TargetConfigurationError(
                f"target {self.target_id} has a non-boolean enabled flag"
            )
        if self.cpu_limit is not None:
            _integer(self.cpu_limit, "cpu_limit", minimum=1)
        if self.gpu_limit is not None:
            _integer(self.gpu_limit, "gpu_limit", minimum=0)
        if self.hour_limit is not None:
            _number(self.hour_limit, "hour_limit", minimum=0)

    def allows_software(self, software: str) -> bool:
        """Whether this target permits the named software.

        An empty catalog permits nothing, which is the conservative reading of
        a deployment that did not say. A target that runs whatever it is given
        is a target whose account can be spent by any contract.
        """
        return software in self.allowed_software

    def document(self) -> dict[str, object]:
        """The target as the schema's document, without resolving anything."""
        return {
            "target_id": self.target_id,
            "kind": self.kind,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "known_hosts_ref": self.known_hosts_ref,
            "credential_ref": self.credential_ref,
            "partition": self.partition,
            "account": self.account,
            "remote_root": self.remote_root,
            "allowed_software": list(self.allowed_software),
            "max_jobs": self.max_jobs,
            "cpu_limit": self.cpu_limit,
            "gpu_limit": self.gpu_limit,
            "hour_limit": self.hour_limit,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """What a credential reference turned out to mean.

    Kept separate from `HpcTarget` so the resolved value is as short-lived as
    possible: it is built by `resolve_target`, passed to `SlurmTarget`, and
    never stored on the registry. Its `repr` is redacted so an accidental
    `print` or a pytest assertion failure cannot show it.
    """

    password: str | None = None
    key_filename: str | None = None

    def __repr__(self) -> str:
        password = "«redacted»" if self.password else None
        return (
            f"ResolvedCredential(password={password!r}, "
            f"key_filename={self.key_filename!r})"
        )


def resolve_target(
    target: HpcTarget,
    *,
    environ: Mapping[str, str] | None = None,
) -> SlurmTarget:
    """Turn a configured target into the runtime target a backend uses.

    **Call this in the execution process.** It is the only function in the
    package that reads the environment or a credential file, and it returns an
    object that holds a secret. The target registry itself never does.
    """
    credential = resolve_credential(target.credential_ref, environ=environ)
    known_hosts = resolve_known_hosts(target.known_hosts_ref)
    return SlurmTarget(
        host=target.host,
        username=target.username,
        port=target.port,
        password=credential.password,
        key_filename=credential.key_filename,
        known_hosts_filename=known_hosts,
        target_id=target.target_id,
        partition=target.partition,
        account=target.account,
        remote_root=target.remote_root,
        allowed_software=target.allowed_software,
        max_jobs=target.max_jobs,
    )


def resolve_credential(
    reference: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedCredential:
    """Read the secret a reference names, or refuse without quoting it.

    Three shapes are understood:

    - `env:NAME` — a password from the environment. The variable is read only
      here, which is why the whole `slurm_` prefix is launcher-only in
      `Settings`.
    - `file:/path` — a password from the first line of a file readable only by
      the worker account.
    - `key:/path` and `keyfile:/path` — an SSH private key, named rather than
      read. Nothing in this process needs its bytes; paramiko reads the file
      when it connects.
    """
    env = os.environ if environ is None else environ
    if reference.startswith("env:"):
        name = reference[len("env:") :]
        if not name:
            raise CredentialResolutionError("credential_ref names an empty environment variable")
        value = env.get(name)
        if not value:
            raise CredentialResolutionError(
                f"credential_ref env:{name} is not set in the execution process"
            )
        return ResolvedCredential(password=value)
    if reference.startswith("file:"):
        path = Path(reference[len("file:") :])
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as error:
            raise CredentialResolutionError(
                f"credential_ref file:{path} could not be read ({error.__class__.__name__})"
            ) from None
        password = body.splitlines()[0] if body.splitlines() else ""
        if not password:
            raise CredentialResolutionError(
                f"credential_ref file:{path} is empty, so there is no password in it"
            )
        return ResolvedCredential(password=password)
    if reference.startswith(("key:", "keyfile:")):
        path = Path(reference.split(":", 1)[1])
        if not path.is_file():
            raise CredentialResolutionError(
                f"credential_ref {reference.split(':', 1)[0]}:{path} does not "
                "name a readable key file"
            )
        return ResolvedCredential(key_filename=str(path))
    raise CredentialResolutionError(
        f"credential_ref {reference!r} is not one of env:, file:, key: or keyfile:"
    )


def resolve_known_hosts(reference: str) -> str:
    """The host-key file a target names, refusing a file that is not there.

    Strict verification is only strict if the file exists. A missing
    known_hosts file would leave paramiko with no key for the host, and the
    refusal that followed would look like a host-key mismatch rather than the
    operator's configuration mistake.
    """
    prefix = "file:"
    if not reference.startswith(prefix):
        raise CredentialResolutionError(
            f"known_hosts_ref {reference!r} must be file:/path; a reference "
            "this backend cannot resolve is not a host-key check"
        )
    path = Path(reference[len(prefix) :])
    if not path.is_file():
        raise CredentialResolutionError(
            f"known_hosts_ref file:{path} does not name a readable file, so "
            "there are no host keys to verify against"
        )
    return str(path)


@dataclass(frozen=True, slots=True)
class TargetRegistry:
    """The fixed set of targets this deployment may be asked for."""

    by_id: Mapping[str, HpcTarget] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for key, target in self.by_id.items():
            if key != target.target_id:
                raise TargetConfigurationError(
                    f"target registry key {key!r} disagrees with target_id {target.target_id!r}"
                )
        object.__setattr__(self, "by_id", MappingProxyType(dict(self.by_id)))

    @classmethod
    def from_documents(cls, documents: Iterable[Mapping[str, object]]) -> TargetRegistry:
        """Build a registry from schema-shaped documents, refusing duplicates."""
        by_id: dict[str, HpcTarget] = {}
        for index, document in enumerate(documents):
            target = target_from_document(document, index=index)
            if target.target_id in by_id:
                raise TargetConfigurationError(
                    f"target_id {target.target_id} appears more than once; an "
                    "allowlist with two entries for one id does not say which one runs"
                )
            by_id[target.target_id] = target
        return cls(by_id=by_id)

    @classmethod
    def from_path(cls, path: str | Path) -> TargetRegistry:
        """Read a target file: a JSON list, or an object with a `targets` list."""
        import json

        source = Path(path)
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except OSError as error:
            raise TargetConfigurationError(
                f"the HPC target file {source} could not be read ({error.__class__.__name__})"
            ) from None
        except ValueError as error:
            raise TargetConfigurationError(
                f"the HPC target file {source} is not JSON: {error}"
            ) from None
        entries = document.get("targets") if isinstance(document, dict) else document
        if not isinstance(entries, list):
            raise TargetConfigurationError(
                f"the HPC target file {source} must be a list of targets or an "
                "object with a 'targets' list"
            )
        return cls.from_documents(entries)

    def __contains__(self, target_id: object) -> bool:
        return isinstance(target_id, str) and target_id in self.by_id

    def __iter__(self):
        return iter(self.by_id.values())

    def __len__(self) -> int:
        return len(self.by_id)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.by_id))

    def target(self, target_id: str) -> HpcTarget:
        """The named target.

        Raises:
            UnknownHpcTargetError: The id is not in the allowlist.
            DisabledHpcTargetError: It is in the allowlist but switched off.
        """
        target = self.by_id.get(target_id)
        if target is None:
            raise UnknownHpcTargetError(
                f"target_id {target_id!r} is not in this deployment's HPC "
                f"allowlist; configured: {', '.join(self.ids) or 'none'}"
            )
        if not target.enabled:
            raise DisabledHpcTargetError(
                f"target_id {target_id!r} is configured but disabled"
            )
        return target

    def resolve(self, target_id: str, *, environ: Mapping[str, str] | None = None):
        """Resolve one target, for the execution process only."""
        return resolve_target(self.target(target_id), environ=environ)

    def resolve_enabled(
        self, *, environ: Mapping[str, str] | None = None
    ) -> dict[str, SlurmTarget]:
        """Resolve every enabled target, keyed by id, for a worker's backend."""
        return {
            target.target_id: resolve_target(target, environ=environ)
            for target in self.by_id.values()
            if target.enabled
        }


def target_from_document(document: Mapping[str, object], *, index: int = 0) -> HpcTarget:
    """Validate one schema-shaped document and build its target.

    Raises:
        TargetConfigurationError: A required field is missing, an unknown one
            is present, a type is wrong, or a value is outside the schema.
    """
    if not isinstance(document, Mapping):
        raise TargetConfigurationError(f"target #{index} is not an object")
    keys = set(document)
    unknown = keys - _KNOWN
    missing = set(_REQUIRED) - keys
    if unknown:
        raise TargetConfigurationError(
            f"target #{index} has fields the schema does not allow: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise TargetConfigurationError(
            f"target #{index} is missing required fields: {', '.join(sorted(missing))}"
        )
    allowed = document["allowed_software"]
    if not isinstance(allowed, (list, tuple)):
        raise TargetConfigurationError(
            f"target #{index} has a non-list allowed_software"
        )
    return HpcTarget(
        target_id=_uuid(document["target_id"], "target_id"),
        kind=str(document["kind"]),
        host=_text(document["host"], "host", maximum=253),
        port=_integer(document["port"], "port", minimum=1, maximum=65535),
        username=_text(document["username"], "username", maximum=128),
        known_hosts_ref=_ref(document["known_hosts_ref"], "known_hosts_ref", _KNOWN_HOSTS_SCHEMES),
        credential_ref=_ref(document["credential_ref"], "credential_ref", _CREDENTIAL_SCHEMES),
        remote_root=_absolute_root(document["remote_root"]),
        partition=_optional_token(document.get("partition"), "partition", maximum=64),
        account=_optional_token(document.get("account"), "account", maximum=128),
        allowed_software=tuple(str(name) for name in allowed),
        max_jobs=_integer(document["max_jobs"], "max_jobs", minimum=1),
        enabled=_boolean(document["enabled"], "enabled"),
        cpu_limit=_optional_integer(document.get("cpu_limit"), "cpu_limit", minimum=1),
        gpu_limit=_optional_integer(document.get("gpu_limit"), "gpu_limit", minimum=0),
        hour_limit=_optional_number(document.get("hour_limit"), "hour_limit", minimum=0),
    )


def parallel_quota(project_quota: int, target_max_jobs: int | None = None) -> int:
    """How many jobs one project may have in flight against one target.

    The smaller of T06's per-project compute allowance and the target's own
    `max_jobs`. Both are limits somebody chose and neither may be exceeded:
    the budget is about how much a project is allowed to spend, and `max_jobs`
    is about how much of a shared cluster one deployment may occupy. A target
    that names no `max_jobs` leaves the budget as the only cap.
    """
    if project_quota < 1:
        raise ValueError("a project quota must be at least one")
    if target_max_jobs is None:
        return project_quota
    if target_max_jobs < 1:
        raise ValueError("a target max_jobs must be at least one")
    return min(project_quota, target_max_jobs)


# ── Field validation ────────────────────────────────────────────────────────


def _uuid(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TargetConfigurationError(f"{field_name} must be a UUID string")
    try:
        return str(UUID(value))
    except ValueError:
        raise TargetConfigurationError(
            f"{field_name} must be a UUID, and {value!r} is not one"
        ) from None


def _text(value: object, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TargetConfigurationError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise TargetConfigurationError(
            f"{field_name} is {len(value)} characters, longer than {maximum}"
        )
    if _CONTROL.search(value) or any(character.isspace() for character in value):
        raise TargetConfigurationError(
            f"{field_name} contains whitespace or control characters"
        )
    return value


def _ref(value: object, field_name: str, schemes: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not value:
        raise TargetConfigurationError(f"{field_name} must be a non-empty reference")
    if _CONTROL.search(value):
        raise TargetConfigurationError(f"{field_name} contains control characters")
    if not value.startswith(schemes):
        raise TargetConfigurationError(
            f"{field_name} {value!r} must start with one of {', '.join(schemes)}"
        )
    return value


def _absolute_root(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise TargetConfigurationError("remote_root must be an absolute POSIX path")
    if len(value) > 1024 or _CONTROL.search(value) or "\\" in value:
        raise TargetConfigurationError(
            "remote_root contains a control character, a backslash, or is too long"
        )
    parts = value.split("/")
    if any(part == ".." for part in parts):
        raise TargetConfigurationError(
            "remote_root contains '..', so a workspace built under it could "
            "escape the directory the operator named"
        )
    if any(part in (".", "") for part in parts[1:-1]) and value != "/":
        raise TargetConfigurationError(
            "remote_root has an empty or '.' segment; a path with two spellings "
            "is two directories to compare"
        )
    return value.rstrip("/") or "/"


def _optional_token(value: object, field_name: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum or not _TOKEN.fullmatch(value):
        raise TargetConfigurationError(
            f"{field_name} must be a single scheduler token of at most {maximum} characters"
        )
    return value


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TargetConfigurationError(f"{field_name} must be true or false")
    return value


def _integer(value: object, field_name: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TargetConfigurationError(f"{field_name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise TargetConfigurationError(f"{field_name} must be at least {minimum}")
        raise TargetConfigurationError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return value


def _number(value: object, field_name: str, *, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TargetConfigurationError(f"{field_name} must be a number")
    if value < minimum:
        raise TargetConfigurationError(f"{field_name} must be at least {minimum}")
    return float(value)


def _optional_integer(value: object, field_name: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    return _integer(value, field_name, minimum=minimum)


def _optional_number(value: object, field_name: str, *, minimum: float) -> float | None:
    if value is None:
        return None
    return _number(value, field_name, minimum=minimum)


__all__ = [
    "SLURM_SSH",
    "CredentialResolutionError",
    "DisabledHpcTargetError",
    "HpcTarget",
    "ResolvedCredential",
    "TargetConfigurationError",
    "TargetRegistry",
    "UnknownHpcTargetError",
    "parallel_quota",
    "resolve_credential",
    "resolve_known_hosts",
    "resolve_target",
    "target_from_document",
]
