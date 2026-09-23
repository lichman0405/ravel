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

## P11-02 research deep read

A source RAVEL stored must be readable by the seat that found it. That is
`KNOWN_LIMITATIONS.md` L-25: the snapshot was written on every registration and
nothing could read it back, so the most a Research session ever saw of a paper
it had obtained was a six-hundred-character excerpt — and for a PDF, nothing at
all. Every later reader of the ledger checks a claim against the snapshot, which
is worth doing only if somebody can read the snapshot.

Three tools, all of them reads, all of them Research's: `source_metadata` says
what the ledger holds and whether it can be read; `read_source` returns a bounded
region of the text, addressed by character for prose and by page for a PDF;
`search_source` searches inside a source already in hand and reports offsets
into the same text a read returns. They are three rather than one per format
because the format is a property of the bytes: a roster that grew with every
format RAVEL learned to read is a roster nobody can hold in mind.

What the surface refuses is the substance of the item. It reads the *snapshot*
and never re-fetches, so what comes back is the document that was registered
rather than whatever the URL serves now, and the bytes are hashed again against
the row's `content_hash` before anybody sees them — a mismatch is a refusal that
names both hashes, not a reading. It writes nothing: no row, no counter, no
artifact, so a look is not an act and the ledger records no sessions. A scanned
PDF is reported as having no text layer, an encrypted one as encrypted, a format
with no words as a format with no words, and none of them is guessed at. And the
provenance block is the ledger row itself — `source_id`, `content_hash`,
`retrieved_at`, `snapshot_ref`, tier, access status — so there is no second
account of where a passage came from.

The table below is the five requirements of §3.3 and §3.5 in
`RAVEL_PHASE11_IMPLEMENTATION_PLAN_v2.md`, and each row names the tests that
demonstrate it.

| Requirement | Demonstrated by |
|---|---|
| Bounded, page/range-addressed reading with a maximum | `test_p11_02_a_stored_paper_is_read_again_by_the_page_it_is_on` — one page asked for, one page returned, and the arguments for the next region; `tests/integration/research/test_deep_read.py::test_a_read_is_a_bounded_region_and_says_where_the_next_one_is`, where two regions concatenate into the document |
| Snapshot hash, retrieved_at and provenance linkage | `test_p11_02_a_methods_parameter_is_read_out_of_the_body_and_cited` — the chain record → claim → source → stored object, verified against its own hash |
| A concrete fact out of a real paper's body, not its abstract | `test_p11_02_a_real_papers_body_is_read_and_quoted` — a live arXiv PDF, read by page and quoted, with the page extracted independently here from the stored bytes |
| Scanned PDFs and formats with no text reported, never invented | `test_p11_02_a_scanned_paper_is_reported_and_never_invented` — pages, no words, and all three tools saying so |
| Reading is not writing | `test_p11_02_reading_a_source_changes_nothing_about_the_project`, and `tests/integration/research/test_deep_read.py::test_reading_writes_nothing` |

The provenance check is the one that makes a reading citable, so it is
demonstrated as a refusal as well as a result:
`tests/integration/research/test_deep_read.py::test_bytes_that_disagree_with_the_row_are_refused_rather_than_read`
replaces the object under a snapshot's key and requires the read to stop. The
same file pins the refusals that keep the surface honest — a source registered
without a snapshot, a paywalled row, a reference this project never issued, a
character region asked of a PDF — and
`test_p11_02_the_reading_surface_belongs_to_the_ledgers_author` asks every role
over the real transport and requires the three tools to be registered for
Research alone. What a seat may read out of a snapshot is the counterpart of
what it may write into the ledger, and the ledger has one author.

Five cases run offline against the real database, the real object store and the
real tool server; the sixth is live, because a document this repository wrote
cannot show that a publisher's PDF yields its body text. Seventeen more in
`tests/integration/research/test_deep_read.py` drive the same path over MCP
stdio, and fifty-six in `tests/unit/test_deepread.py` settle the reader itself:
the media-type table, every extraction, the offset arithmetic, and the scans,
encryptions and unparseable files that have no text in them. Those fixtures are
real PDF files, built byte by byte in `tests/documents.py`, so the scanned and
encrypted cases are tested on documents that really are those things.

## P11-03 research → Master result readback

The chain the architecture claims is `Master → Research Agent → Evidence /
ResearchRecord → Master → Decision`, and the last arrow had no tool behind it.
Master could read the whole project state and could not read one research task's
result, so what a task found reached the next decision only if the session that
received it was still the session making it. Across a crash, a rebuild, or a
supervisor restart — the situations a long-lived project is made of — it is not,
and the record that would have answered the question was in PostgreSQL the whole
time with nothing pointing at it.

Four tools close it. All four are reads, all four are Master's alone, and none
of them writes: `list_research_results` names each research task and what it
delivered — record, completion status, sufficiency, claim/source/conflict
counts, verdict — and `read_research_result` returns one task's record, the
claims it was assembled from, their sources, the conflicts, a sufficiency
assessment measured live off the ledger, and the verdicts given about the node.
`read_evidence` and `read_source_metadata` are the narrower reads a decision
that turns on one claim needs, and the second returns no text at all: what a
document says is read by the seat whose task it answers.

`read_project_state` carries `completed_research`, the research tasks that have
handed a result over, one flat summary each in the same shape the listing
returns. It is a pointer rather than the evidence — a task still being worked on
is deliberately absent, and no claim text reaches the state read, which is the
read every turn begins with.

| Requirement | Demonstrated by |
|---|---|
| Master can read Research's actual output | `test_p11_03_master_reads_the_result_a_research_seat_handed_over` — the seat's server begins the task and hands the record over, Review's server judges it, Master's server reads the record, its claims, a claim's sources and a source's metadata, all compared against the rows |
| Authorship stays separated: MASTER reads, RESEARCH writes | `test_p11_03_the_readback_belongs_to_master_and_to_nobody_else` — every role over the real transport, plus `tests/integration/master/test_research_readback.py::test_the_readback_tools_are_masters_and_write_nothing` |
| The next turn sees that a result is available | `test_p11_03_master_reads_the_result_a_research_seat_handed_over` (the state read names the node, its verdict and its record, and carries no claim text), `test_p11_03_the_masters_turn_points_at_the_result_it_can_read` (the turn itself names the node and the tool, and carries no claim text either) and `test_p11_03_a_task_that_has_not_handed_over_is_not_a_result_yet` (a task in flight is not a result, and is still readable) |
| What is read is the row, not a session's memory | `tests/integration/master/test_research_readback.py::test_the_ledger_holds_what_the_readback_reports` — the same fields read straight out of PostgreSQL |

The refusals are part of the item rather than an edge of it. A node that is not a
research task has no ledger to return, and asking for one is refused in a
sentence that names the tool which lists the nodes that do. A tool Master does
not hold cannot be called at all, and the ledger's four writing tools are refused
to every other role including Master — pinned by
`tests/integration/roles/test_research_tools.py::test_only_research_may_write_to_the_ledger`,
which asserts nothing was written as well as that the call failed.

Four cases run offline against the real database and the real tool servers, one
per role that the chain passes through. Six more in
`tests/integration/master/test_research_readback.py` drive the same path directly
and read the storage back out of PostgreSQL, which is where the claim that the
readback is the row rather than a summary of it is settled.
