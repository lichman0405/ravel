# 06 — Execution, Contracts & Review

## 1. Execution Contract

Every COMPUTATION/EXPERIMENT node executed by a Worker has a frozen Execution Contract.

Typical fields:
- objective
- procedure
- inputs
- allowed_actions
- parameter targets
- allowed ranges
- allowed retries
- allowed substitutions
- required outputs
- stop conditions
- escalation conditions
- resource limits
- acceptance contract ref

Worker never invents authority.

### Preparation: the workspace a contract runs in

A contract may name the environment its work happens in —
`execution_requirements`, a one-entry mapping such as `{"software": "raspa"}` or
`{"lab": "bench-chemistry"}` — and when it does, the run happens in a workspace
RAVEL builds from the contract or it does not happen. `requires_preparation` is
whether a contract names one, so preparation is opt-in per contract: a contract
that names nothing runs exactly as it did before this layer existed.

A `MaterializerRegistry` resolves the environment to a materializer, and the
deployment registers exactly two — `RaspaMaterializer` and `LabMaterializer`.
The materializer resolves the contract's inputs to artifact versions and reads
them out of the object store, writes the files with their hashes, records a
manifest of what was built from what, and names the entry point a job starts
from. **It decides nothing.** Everything in a workspace is a rendering of
something the contract states; where the contract does not say enough, it refuses
and names the term, because choosing a method, filling in a concentration or
picking a solvent is science and no role has delegated that to software. The
acceptance criteria in a bench package are the node's own frozen binding, read
out of the DAG by the activity and handed in — a materializer that looked them up
itself would be free to build a package against criteria the delivery is not
measured by.

A refusal is recorded, not raised, and its class is what routes it:

- `MISSING_SCIENTIFIC_PARAMETER` — a gap in the terms, and Master's to fill.
- `INCONSISTENT_CONTRACT` — terms that disagree with themselves.
- `ENVIRONMENT_UNAVAILABLE` — this machine: RASPA code with no RASPA installed,
  or a resolved input with no object store to read it from. The sentence names
  the setting that would fix it.
- `UNSUPPORTED_ENVIRONMENT` — RAVEL: no materializer for that environment at
  all, which is a different sentence for Master than a broken host.

Every refusal parks the node at `WAITING_DECISION`, writes no Execution Record,
starts no job and ends the run with no termination status: nothing was tried, so
there is nothing to fail, and the contract is what is wrong. `read_project_state`
reports it under `stopped` as `preparation_refusal`, alongside the verdict a seat
gave and the `run_reconciliation` a lost run left — the three reasons a node
stops, and the one the Master turn names as RAVEL's own.

## 2. Experimental deviation rule

Worker does not judge "execution vs scientific".

It checks:
> Is the requested action explicitly permitted?

If yes: execute.
If no:
- pause
- create deviation
- wait for Master

Allowed Worker communication:
- CONFIRM
- INFORM from contract
- REQUEST_MISSING_INFORMATION
- ESCALATE

## 3. Delivery Completeness vs Scientific Acceptance

Worker:
> Did we receive everything required?

Review:
> Does the complete result scientifically pass frozen criteria?

Missing artifact is `INCOMPLETE_DELIVERY`, not automatically scientific FAIL.

## 4. Compute Backend

Deterministic RAVEL interface, not an Agent.

See `schemas/compute_backend_contract.yaml`.

V0:
- MockComputeBackend — plays a named scenario; everything it writes is marked
  simulated and the Evidence Ledger refuses it.
- SlurmComputeBackend — a real cluster over SSH. Selected with
  `--compute-backend slurm` and configured by the `RAVEL_SLURM_*` settings; with
  no host or username the worker refuses to start rather than stalling on the
  first node that reaches the queue. It submits the job script preparation
  wrote, polls `squeue`/`sacct`, and collects the outputs the contract required
  as artifacts under the project the run's own submission record names.

Future:
- local/cloud compute

Compute Worker never directly hardcodes sbatch/SSH in its core logic.

### Cluster credentials

The Slurm password is read by the worker process from its own environment and by
the SSH transport. It is listed in the settings' launcher-only prefix set, so it
is stripped from the environment of every DSH runtime and tool server RAVEL
starts — the model never has it to leak — and it is redacted out of every
`detail`, progress fact, exception message and `completion_metadata` the backend
produces, because those end up in PostgreSQL and in logs that outlive the
process that held the secret. Paramiko rather than the `ssh`/`scp` clients for
the same reason: the OpenSSH client takes a password only from a terminal or
from `sshpass`, and `sshpass -p <secret>` puts it in a command line every
account on the host can read from `ps`.

### What a Slurm result is

An artifact collected off a cluster is **not** marked simulated — it came off a
real machine — and it is **not** marked as evidence either. Whether it satisfies
a node's acceptance criteria is Review's judgement against the frozen criteria;
a backend that asserted its own result was admissible would be deciding its own
case. A run that ends without producing everything the contract required is
reported as a missing output rather than raised: the run ended and produced what
it produced, and `INCOMPLETE_DELIVERY` is the layer that decides what that
means.

## 5. Experiment Backend

Deterministic RAVEL interface, not an Agent.

See `schemas/experiment_backend_contract.yaml`.

V0:
- MockLabBackend — plays a named scenario; everything it writes is marked
  simulated and the Evidence Ledger refuses it.
- HumanLabBackend — a real bench, reached through a person. Selected with
  `--lab-backend human-lab`; it needs no credential and no host, because what it
  needs is somebody standing at a bench, and a deployment that selects it with
  nobody there simply has runs that wait.

Future:
- LIMS
- robotic lab

### What the human lab channel is

`HumanLabBackend` hands the prepared package to a person and waits. The three
things worth knowing before reading the code:

**Nothing here ever reports `RUNNING`.** RAVEL cannot see a bench. A state
meaning "somebody is probably working on it" would be an observation RAVEL never
made, and `WAITING_EXTERNAL` is the state that says what is actually true — with
its own clock, so an unanswered wait ends in `TIMED_OUT` rather than in a run
that looks busy forever.

**The handover is a row, not a file.** The Slurm backend finds a job again by
reading a file it uploaded beside it; a bench has nowhere to upload a file to, so
the durable record is `lab_handovers`, keyed by
`(project_id, node_id, execution_contract_version, attempt)`. A revised contract
is different work and gets its own handover, so files sent under the old terms
cannot count towards the new run.

**What arrived is a fact and what a delivery claims is not.** Uploads are
artifact versions with an author, a time, a media type and a hash. A delivery
completes a run when the *recorded* names cover every name in the handover's
frozen `required_outputs` — never when the delivery says it brought everything,
and never on a file a mock produced.

### Who may upload, and what an upload proves

The person at the bench uploads through the Gateway, against a named output. Any
project member may: the person who ran the experiment is the one holding the
file, and requiring the owner to relay it would mean recording that somebody
uploaded a file they never saw. The version carries `created_by`, so the record
says who it was.

An upload answers a name the contract required or it is refused, with the owed
list in the message — and refused *before* a byte is stored, so an arbitrary file
never reaches the record at all. Uploading does not complete anything: what
finishes a run is the backend's comparison of what is recorded against what was
owed, which is not a claim a person's upload can make.

A lab user's deviation is *recorded*, not adjudicated. It is written against the
node's frozen contract with `permitted` false — the reporter did not permit it —
and then travels to the run on the same signal a delivery does, because a run
waiting on a bench is blocked in a durable wait and is never polled. Whether the
contract permits what was asked is the Worker's question and goes to Master from
there.

**RAVEL cannot stop a person.** Withdrawing a handover ends the record so that a
later delivery cannot complete a run that was given up on, and says out loud that
this is RAVEL giving up rather than anybody being interrupted. Whether the bench
stopped is not something RAVEL can see.

## 6. Mock Compute scenarios

Must simulate:
- success
- long running
- deterministic retryable infra failure
- scientific/non-retryable failure
- timeout
- missing output
- cancellation

Mock output must be visibly marked simulated and can never enter Evidence Ledger as real scientific evidence.

## 7. Mock Lab scenarios

Must simulate:
- normal completion
- long wait
- missing artifacts
- operator question
- parameter out-of-bounds deviation
- equipment unavailable
- cancellation
- Master decision wait/resume

Mock lab data must be tagged simulated.

## 8. Review checkpoints

A node may define:
- PRE_RUN review
- RUNTIME review
- FINAL review

Any change caused by Review recommendation still requires Master Decision Record.

## 9. Review outcome

- PASS
- FAIL
- PARTIAL

Review Record includes:
- frozen criteria version
- criterion-by-criterion result
- diagnosis
- evidence/artifact refs
- recommendations
- timestamp
- review session binding

## 10. Execution history

Worker produces immutable ExecutionRecord:
- task/node
- contract version
- backend
- backend job/task id
- attempts
- timeline
- output refs
- logs
- completeness
- deviations
- termination status
