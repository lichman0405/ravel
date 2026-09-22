# RAVEL Phase 11 Acceptance

Phase 11 closes the gap between an architecture that can run a project and one
that can be trusted with a real one. Its tasks are a sequence — a later one
assumes the earlier one holds — so this file grows as they land, one item per
task, and the items that have not landed yet are simply not here.

Run it with:

```bash
make phase11-acceptance
```

That command runs `tests/acceptance -m phase11` and prints a pass/fail row for
every item below. An item's cases are the functions named for it —
`test_p11_01_...` for `P11-01` — and an item nobody wrote a case for prints as
`MISSING` rather than being left out. The number of items in this file is
asserted against the matrix script, so an item that is deleted from here is a
failure rather than a quietly shorter table.

Phase 11 does not relax Phase 9's or Phase 10's acceptance. A01–A20, the seven
extra gates in `V0_ACCEPTANCE.md`, and P10-01..P10-W20 still stand, and they are
still run by `make acceptance` and `make phase10-acceptance`.

## P11-01 execution reconciliation

A node whose `NodeRunWorkflow` is gone must not stay `RUNNING` forever. That is
`KNOWN_LIMITATIONS.md` L-24, and it is the failure that makes every other kind of
autonomy unsafe: a project that can strand a node is a project that can be left
holding a run that will never report, and a Worker waiting on it will wait
forever. Phase 11 fixes it first because the backends that follow — a real Slurm
cluster, a real laboratory — make a lost run more likely, not less.

`ExecutionReconciler` sweeps each project's live nodes on every supervisor tick.
For each one it asks Temporal about the single run that node is supposed to have,
by the same workflow id the start path builds, and for a run that is gone it ends
the job, moves the node to `WAITING_DECISION` where Master is asked, and writes an
immutable `RunReconciliation` recording what it observed — in one transaction,
behind a write that re-reads the node so a decision that landed mid-sweep wins.

What it refuses to do is the substance of the item. A lost run is **not** a
scientific failure: it ends no hypothesis, concludes no project, and writes no
Execution Record, because a run that never reported did nothing RAVEL can attest
to. It also writes no verdict — the node is parked as a question for Master, not
answered in Master's place. The table below is the five requirements of §2.4 in
`RAVEL_PHASE11_IMPLEMENTATION_PLAN_v2.md`, in its order, and each row names the
tests that demonstrate it.

| Requirement | Demonstrated by |
|---|---|
| Activity retry exhaustion does not leave a node `RUNNING` | `test_p11_01_a_run_that_died_writing_its_result_does_not_strand_its_node` — a real backend whose `collect` raises, five real retries, a real `FAILED` workflow |
| Recovery after a supervisor restart | `test_p11_01_a_supervisor_recovers_a_run_that_died_before_it_started` and `test_p11_01_a_node_whose_workflow_was_never_started_is_recovered` — a node `RUNNING` with no workflow at all, which is the state a restart leaves behind |
| Two sweeps in a row agree | `test_p11_01_sweeping_twice_finds_the_loss_once`, `tests/integration/reconcile/test_execution_reconcile.py::test_two_sweeps_at_once_still_write_one_record` |
| A workflow that completed and was never consumed is recovered | `tests/integration/reconcile/test_execution_reconcile.py::test_a_run_that_finished_unconsumed_is_recovered_too` — Temporal says COMPLETED, PostgreSQL says RUNNING, and the pair is RAVEL's own failure rather than a result |
| A lost workflow does not pollute the scientific conclusion | `test_p11_01_a_lost_run_leaves_a_question_and_no_scientific_verdict` — no Execution Record, no PASS, no ending |

The classification is part of the claim rather than a label on it: a run that
disappeared is `INFRASTRUCTURE` or `CANCELLED`, never `SCIENTIFIC`, and the job
it held is failed for infrastructure rather than for the science. A termination
is recorded as a cancellation, because somebody stopped it and calling that a
failure would be a false accusation.

The reconciler only ever reads from Temporal, with a client it holds no other
purpose for, and it never starts, signals, or terminates a run. Nine cases in
`tests/acceptance/test_phase11_reconcile.py` run against the real cluster; the
twenty-two in `tests/integration/reconcile/` drive the same code against
PostgreSQL with a scripted probe, which is what makes the races reachable.

**Which nodes it asks about is part of the claim.** Only the node types a Worker
executes as a durable run — COMPUTATION and EXPERIMENT, `WORKER_RUN_NODE_TYPES`
— have a workflow that can be lost. The other four are performed by the agent of
their seat inside its own turn, so no run exists for them and Temporal's
`NOT_FOUND` about one is true and means nothing. The first version of the sweep
asked about every node in a live status and the live five-agent certification
failed on it: three research nodes were parked in `WAITING_DECISION` mid-search,
each with a reconciliation saying its run had been lost. That is recorded in
`TEST_REPORT.md` §6 and `KNOWN_LIMITATIONS.md` L-24, and pinned by
`tests/integration/reconcile/test_execution_reconcile.py::test_a_node_no_worker_runs_is_never_asked_about`,
which asserts both that nothing was written and that the probe was never asked.
