"""What RAVEL built for a run before it started, and what it could not build.

Preparation is the step between a frozen contract and a Worker starting work:
Master decides what is to be done, the contract says what that is, and
*something* has to turn it into the files a machine or a bench needs. That
something is not an agent. It reads the contract, checks it, writes files,
hashes them, and either hands back a workspace or refuses — and the difference
between it and the five roles is that it makes no decisions at all. A
materializer that invented a temperature, filled in a missing concentration, or
chose a method would be doing science, and no role has delegated that to it. So
when the contract does not say enough, it refuses and says why, and the node
goes to `WAITING_DECISION` where Master — the one role that may decide — reads
the refusal and decides.

Everything here is about *recording* that. The machinery that actually writes
workspaces lives in `ravel.preparation`, and this module is the vocabulary it
answers in plus the immutable row that survives it:

- `PreparationOutcome` — prepared, or refused. There is no "partly prepared".
  A workspace with a missing input is a run that fails five retries deep in an
  activity nobody can see; a refusal is a sentence Master can act on.
- `PreparationRefusal` — *why* it could not be built, from a closed list. The
  members are four different next steps for Master, and the closed list is what
  makes the choice structural rather than a reading of prose: a missing
  parameter is answered by a research task or a contract revision, an
  unsupported package is answered by a different environment or by not running
  this node at all, and an unavailable one is answered by fixing a host.
- `PreparationCheck` — one thing that was verified, kept so that a reader can
  see what was checked rather than only that it passed.
- `PreparationRecord` — the immutable row. It is written in the same
  transaction as the node's move, so a node waiting on Master with no
  explanation of what was wrong with its contract is not a state RAVEL can
  reach.

**Why a refusal is a record and not an escalation.** A deviation is a Worker
asking to do something its contract does not permit; the answer is a revised
contract, and `resolve_deviation` requires the revision to permit the action
that was requested. Nothing was requested here — the contract was read and
found unmaterializable — so there is no action for a revision to permit and the
deviation vocabulary does not fit. What Master gets is the node at
`WAITING_DECISION` and this row, which says which contract version was read and
what it lacked.

**Why the record is per contract version and not per attempt.** A run may be
attempted more than once, and the workspace a contract materializes to is the
same workspace every time: the inputs are the contract's, the parameters are
the contract's, and hashing them again produces the same manifest. What
differs between attempts is the job, which `backend_jobs` already records per
attempt. Preparation is a fact about the terms, so that is what it is keyed to.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.ids import new_id


class PreparationOutcome(StrEnum):
    """What preparation did with a contract, from the closed list.

    Two members, and the second is not a failure of the software. A contract
    RAVEL cannot materialize is a statement about the contract — it does not
    name enough, or it names an environment this deployment does not have —
    and calling it an error would put it in a log instead of in front of the
    role that can fix it.
    """

    PREPARED = "PREPARED"
    REFUSED = "REFUSED"


class PreparationRefusal(StrEnum):
    """Why a contract could not be materialized, from the closed list.

    Four members, four different next steps, and the reason they are enumerated
    is that a free-text refusal puts the classification back on a model. Master
    reads one of these and knows which of its own tools the situation calls
    for; the sentence beside it says what specifically was missing.

    - `MISSING_SCIENTIFIC_PARAMETER` — the contract does not say enough to run.
      A temperature with no value, a method with no procedure, a molecule with
      no structure. **This is the member that exists to stop a materializer
      from guessing**, and it is the one Master most often answers with a
      research task: the parameter is missing because it is not known yet, and
      looking it up is work rather than a decision.
    - `INCONSISTENT_CONTRACT` — the contract says enough and contradicts
      itself: a parameter target outside the range the same contract permits,
      a required output that names nothing the run produces, a substitution
      against a reagent the contract never mentions. Nothing is missing here;
      the terms disagree.
    - `UNSUPPORTED_ENVIRONMENT` — the contract names a kind of environment that
      is materializable and a package this deployment has no materializer for.
      Not the same thing as the kind being unknown, which the contract model
      refuses before a contract can exist.
    - `ENVIRONMENT_UNAVAILABLE` — the materializer exists and the host cannot
      do its job: the toolchain is absent, the filesystem is not writable, the
      workspace root cannot be created. RAVEL's own machinery, and the fourth
      member exists so that it can be told apart from the other three rather
      than reported as a missing parameter.
    """

    MISSING_SCIENTIFIC_PARAMETER = "MISSING_SCIENTIFIC_PARAMETER"
    INCONSISTENT_CONTRACT = "INCONSISTENT_CONTRACT"
    UNSUPPORTED_ENVIRONMENT = "UNSUPPORTED_ENVIRONMENT"
    ENVIRONMENT_UNAVAILABLE = "ENVIRONMENT_UNAVAILABLE"


class PreparationCheck(Record):
    """One thing preparation verified, and what it found.

    The report rather than the verdict. `PreparationRecord.refusal` says why a
    contract was not materialized; these say what was looked at on the way
    there, which is what a reader needs to see that the check that refused was
    run against the contract rather than assumed.
    """

    name: str = Field(min_length=1)
    passed: bool
    detail: str = ""


class PreparationRecord(Record):
    """One contract materialized, or one refusal to materialize it.

    Immutable and append-only, like every other record of something that
    happened. Written in the same transaction as the node's move to
    `WAITING_DECISION`, so a node waiting on Master and the reason it is
    waiting are never two facts that can disagree.

    `workspace_path` is stored as the materializer wrote it — an absolute path
    under the runtime directory — because that is the path the Worker is handed
    and the one a person opens. Deriving it in each reader is how a reader and
    a worker come to disagree about which directory a run happened in.

    `manifest` is the same document that was written into the workspace as its
    manifest file, kept here because the two have different lifetimes: the
    workspace is a directory a person or an operating system may remove, and
    what RAVEL built for a run is Project State. The manifest is not a summary
    of the record — it is the record's own account of the files, their hashes,
    the parameters they were built from, and the version of the code that built
    them, which is what makes a result traceable to the inputs it came from.
    """

    preparation_id: str = Field(default_factory=new_id)
    project_id: str
    node_id: str
    #: The contract that was read. Both halves are kept: the reference names
    #: the exact document, and the version is what the run is keyed to.
    execution_contract_ref: str = Field(min_length=1)
    execution_contract_version: int = Field(ge=1)
    outcome: PreparationOutcome
    #: Which materializer built it — `"raspa"`, `"bench-chemistry"` — and the
    #: version of the code that did. Empty on a refusal that never reached a
    #: materializer, which is itself information: it says the contract was
    #: refused before anything was built.
    materializer: str = ""
    materializer_version: str = ""
    #: Where the workspace is — and, on a refusal, where it would have been
    #: built. Empty only when neither is known, which is the case for a
    #: contract refused before a directory was ever named.
    #:
    #: **A path here is not a promise that a directory exists.** On a refusal
    #: nothing was built, and the path is kept because it is where a host fault
    #: is usually found: a workspace root RAVEL may not write to is the
    #: `ENVIRONMENT_UNAVAILABLE` case, and a reader diagnosing it needs to know
    #: which directory was refused rather than only that one was. Every reader
    #: that hands this to something that runs work asks `is_prepared` first —
    #: `PreparationRepository.prepared_for_run` is that question, and it is why
    #: the difference is a lookup rather than a convention.
    workspace_path: str = ""
    #: What the run must deliver, copied from the contract so that a reader of
    #: this record does not have to open the contract to know what is expected.
    required_outputs: tuple[str, ...] = ()
    manifest: dict[str, Any] = Field(default_factory=dict)
    checks: tuple[PreparationCheck, ...] = ()
    #: What the Worker needs to know about the environment it is about to run
    #: in, in its own vocabulary: the software and its version, the entry point
    #: it is started through, the files the run is expected to produce.
    execution_metadata: dict[str, str] = Field(default_factory=dict)
    refusal: PreparationRefusal | None = None
    #: Why, in a sentence, when the outcome is a refusal.
    reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_prepared(self) -> bool:
        """Whether there is a workspace a run can be started in."""
        return self.outcome is PreparationOutcome.PREPARED

    @model_validator(mode="after")
    def _the_outcome_is_carried_by_the_record(self) -> PreparationRecord:
        """Refuse a record whose outcome nothing in it supports.

        The outcome is a single word, and a reader that trusted it alone would
        have no way to tell a refusal that named its reason from one written by
        a bug. Each branch of this check is the minimum the other fields have
        to say for the word to be true.
        """
        if self.outcome is PreparationOutcome.REFUSED:
            if self.refusal is None:
                raise ValueError(
                    "a refused preparation must say which kind of refusal it was; "
                    "a refusal with no class cannot be routed to the right next step"
                )
            if not self.reason.strip():
                raise ValueError(
                    "a refused preparation must say why in words: Master reads "
                    "this to decide, and the class alone does not say what was "
                    "missing from the contract"
                )
            return self
        if self.refusal is not None:
            raise ValueError(
                f"a {self.outcome.value} preparation cannot carry the refusal "
                f"{self.refusal.value}: the two say different things about the "
                "same contract"
            )
        if not self.workspace_path.strip():
            raise ValueError(
                "a prepared execution must name the workspace it was prepared "
                "in; without it there is nothing for a Worker to run in"
            )
        if not self.materializer.strip():
            raise ValueError(
                "a prepared execution must name the materializer that built it, "
                "so that what it wrote can be told apart from what another one "
                "would have written"
            )
        return self
