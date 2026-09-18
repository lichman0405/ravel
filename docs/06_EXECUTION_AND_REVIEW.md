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
- MockComputeBackend

Future:
- Slurm/HPC adapter
- local/cloud compute

Compute Worker never directly hardcodes sbatch/SSH in its core logic.

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
