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
- MockLabBackend

Future:
- human lab channel
- LIMS
- robotic lab

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
