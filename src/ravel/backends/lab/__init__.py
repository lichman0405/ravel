"""The human laboratory backend: the second half of Phase 11's real work.

`SlurmComputeBackend` runs work on a cluster. This runs work on a bench, which
is to say it hands a prepared package to a person and waits. The port is the
same one the mocks implement and the same one the cluster implements; what is
different is what each call has to get right, and the three things worth
knowing before reading the code:

**Nothing here ever reports `RUNNING`.** RAVEL cannot see a bench. A state that
meant "somebody is probably working on it" would be an observation RAVEL never
made, and `WAITING_EXTERNAL` is the state that says what is actually true.

**The handover is the durable record.** A cluster job is found again by reading
a file the backend uploaded next to it; a bench has nowhere to upload a file
to, so the record is a row — `ravel.domain.lab`, `lab_handovers` — and it is
what makes this backend survive a worker restart without remembering anything
in memory.

**What arrived is a fact and what a delivery claims is not.** Uploads are
artifact versions with an author, a time, a media type and a hash; `deliver`
compares the *recorded* names against the handover's `required_outputs` and
never against `ExternalDelivery.delivered_outputs`. A delivery that says it
brought everything cannot complete a run.

The pieces:

- `HumanLabBackend` — the port, against a person.
- `BACKEND_NAME` — how this backend is named in every record it produces, and
  the string its references begin with.
- `BACKEND_WORD` — the bench's own word for "in somebody's hands", kept beside
  RAVEL's state so a reader can see the two are about different things.
- `LabConfigurationError` — a laboratory run handed over with no package to
  hand anybody, which is a deployment fault rather than a failure of the work.

**Live certification is not in this package and cannot be.** Everything here is
exercised against a real PostgreSQL, real object storage, and a scripted lab
user; whether a given bench follows the protocol RAVEL wrote, whether the
reagents named are the ones at hand and whether the measurement plan is
physically possible are facts about a laboratory, and `acceptance/V0_ACCEPTANCE.md`
records them as blocked until somebody runs one.
"""

from ravel.backends.lab.backend import (
    BACKEND_NAME,
    BACKEND_WORD,
    HumanLabBackend,
    LabConfigurationError,
)

__all__ = [
    "BACKEND_NAME",
    "BACKEND_WORD",
    "HumanLabBackend",
    "LabConfigurationError",
]
