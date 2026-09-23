"""The Slurm backend: the one environment in V0 that runs work somewhere real.

Until this package, every backend RAVEL had either simulated a result or waited
for a person to supply one. That is enough to run a project end to end and not
enough to run a project: `acceptance/V0_ACCEPTANCE.md` asks for real computation
through a real scheduler, and `docs/06` says a mock's output is not evidence.
This is what answers that, and it is also the first place in RAVEL where a
mistake costs something an operator can see — an allocation, a queue wait, a
node held for two days.

The pieces:

- `SlurmTarget` — where the cluster is and how to reach it. Holds the one
  credential in the system that is not RAVEL's own, and holds it only here.
- `SlurmComputeBackend` — the `WorkBackend` port, against a real `sbatch`.
- `SSHTransport`, `ParamikoTransport` — the seam a test substitutes, and the one
  implementation that opens a connection.
- `Redactor` — applied to every string leaving the backend, because a `detail`
  line becomes a PostgreSQL column and a traceback becomes a log line.
- `read_state`, `SLURM_STATES` — Slurm's vocabulary in RAVEL's, one row per
  state, with the reasoning for each.

What is *not* here is as deliberate. There is no retry policy — `decide_retry`
in `ravel.execution.policies` owns that, and a backend that retried would be
spending a second allocation on its own authority. There is no notion of a
scientific parameter — the contract has those, preparation writes them into the
job, and this package moves bytes. And there is no adapter from Slurm's
`Comment` or `--gres` to a contract term: a backend that could add one would be
a second place a run's method is decided.

**Live certification is not in this package and cannot be.** Everything here is
exercised against a transport a test controls; whether a given cluster accepts
the job script, mounts the filesystem RAVEL writes into, and has the software
the contract names is a fact about that cluster, and the acceptance suite
records it as blocked until somebody supplies an endpoint to point it at.
"""

from ravel.backends.slurm.backend import (
    BACKEND_NAME,
    JOB_NAME_PREFIX,
    JOB_SCRIPT_SUFFIX,
    REF_DIGEST_CHARS,
    SUBMISSION_FILE,
    SlurmComputeBackend,
    SlurmConfigurationError,
    SlurmSubmissionError,
    SlurmTarget,
)
from ravel.backends.slurm.ssh import (
    REDACTED,
    CommandResult,
    ParamikoTransport,
    Redactor,
    RemoteFile,
    SSHTransport,
    TransportError,
    connected,
)
from ravel.backends.slurm.states import (
    SLURM_STATES,
    SlurmReading,
    normalize_state,
    read_exit_code,
    read_state,
)

__all__ = [
    "BACKEND_NAME",
    "JOB_NAME_PREFIX",
    "JOB_SCRIPT_SUFFIX",
    "REDACTED",
    "REF_DIGEST_CHARS",
    "SLURM_STATES",
    "SUBMISSION_FILE",
    "CommandResult",
    "ParamikoTransport",
    "Redactor",
    "RemoteFile",
    "SSHTransport",
    "SlurmComputeBackend",
    "SlurmConfigurationError",
    "SlurmReading",
    "SlurmSubmissionError",
    "SlurmTarget",
    "TransportError",
    "connected",
    "normalize_state",
    "read_exit_code",
    "read_state",
]
