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

## P11-04 execution preparation

A contract that names an environment must be turned into that environment before
anything runs, or the run must not happen. That is the item, and what makes it
Phase 11's rather than Phase 6's is where the answer goes when it cannot be
built: the contract is what is wrong, Master is the only role that may revise
it, and a refusal has to arrive at Master as a question rather than as a failure
of work nobody attempted.

`ExecutionContract.execution_requirements` is a one-entry mapping — `{"software":
"raspa"}`, `{"lab": "bench-chemistry"}` — and `requires_preparation` is whether a
contract names one. Preparation is therefore opt-in per contract, and a contract
that names nothing runs exactly as it did before the layer existed. A
`MaterializerRegistry` resolves the environment to a materializer and builds a
workspace from the frozen contract: the inputs resolved to artifact versions and
read out of the object store, the files written with their hashes, a manifest
recording what was built from what, and an entrypoint a job starts from. The
deployment registers exactly two — `RaspaMaterializer` and `LabMaterializer` —
and a contract naming anything else is refused rather than guessed at.

The refusals are the item's spine, and the class is what routes them.
`MISSING_SCIENTIFIC_PARAMETER` is a gap in the terms — a bench contract with no
procedure — and is Master's to fill. `INCONSISTENT_CONTRACT` is terms that
disagree with themselves. `ENVIRONMENT_UNAVAILABLE` is this machine: RASPA code
with no RASPA installed, or a resolved input with no object store to read it
from, and the sentence names the setting that would fix it. `UNSUPPORTED_ENVIRONMENT`
is RAVEL: no materializer for that environment at all, which is a different
sentence for Master than a broken host. Every one of them parks the node at
`WAITING_DECISION`, writes no Execution Record, starts no job, and ends the run
without a termination status — nothing was tried, so there is nothing to fail.

The materializer is not allowed to decide anything. It renders what the contract
states — the procedure as written, the conditions it fixes, the samples it
names, the outputs it requires — and where the contract does not say enough it
refuses and names the term. It does not choose a method from an objective, fill
in a concentration, or read the DAG for itself: the acceptance criteria in a
bench package are the node's *frozen binding*, handed in by the activity, so a
package cannot be built against criteria the delivery will not be measured by.

| Requirement | Demonstrated by |
|---|---|
| A contract that names an environment runs in the workspace built for it | `tests/integration/temporal/test_preparation.py::test_a_run_that_needs_an_environment_runs_in_the_workspace_built_for_it` — contract → inputs → files → manifest → the `JobRequest` the backend was actually handed |
| A refusal parks the node, writes no record and starts nothing | the three refusal cases in the same file, plus `test_a_lab_contract_that_states_no_procedure_parks_the_node` |
| A refusal reaches the seat that has to answer it | `test_p11_04_a_contract_this_deployment_cannot_build_reaches_master` — the refusal read back through Master's own tool, field by field against the row, and the turn that hands Master the node |
| A refusal is about this deployment, not about the work | `test_p11_04_the_same_contract_prepares_where_the_environment_exists` — the same tool call on a deployment that can build it |
| A contract naming no environment is unchanged | `tests/integration/temporal/test_preparation.py::test_a_contract_that_names_no_environment_runs_exactly_as_before` |
| Preparation is keyed to the terms, not to the call | `test_the_same_run_prepares_one_workspace_however_often_it_is_read`, `tests/integration/state/test_preparations.py::test_each_contract_version_is_its_own_history` |

**What Master is told, and why the case exists.** The first version of this read
reported what stopped a node from a review verdict and a lost run, and a
preparation refusal was neither — so a live five-agent run handed Master a node
that had refused, Master found nothing in the record that named a reason, and it
cancelled the work. Its own cancel rationale is the finding: "the state records
no verdict from a seat, no run reconciliation from RAVEL, and no unexecutability
reason". The refusal had been written the whole time. `read_project_state`'s
`stopped` block now carries `preparation_refusal`, and the Master turn says
which of the three fields to look in — because a reason in a record no prompt
mentions is a reason a session will not go and read. The field is reported only
when the refusal is the *newest* thing RAVEL did for the node: one that a later
preparation answered is history, and reporting it would answer "why has this
stopped" with a reason something else stopped earlier
(`tests/integration/state/test_preparations.py::test_the_refusal_reported_as_stopping_a_node_is_the_newest_one`).

**A live Master writes contracts the deployment has not registered.** In the
same run, contracts naming `{"software": "python"}` and `{"lab":
"four-point-probe"}` were written and refused as `UNSUPPORTED_ENVIRONMENT`. That
is not a defect: the tool documentation names the two environments RAVEL builds,
and a Master that asks for another gets a refusal that says so rather than a
workspace that pretends. `tests/integration/temporal/test_preparation.py::test_an_input_nothing_answers_does_not_need_an_object_store`
is the other half of a related fix — `inputs` is a list of *names*, resolving one
is a database query, and a plan naming the research contract or a file the run
produces itself resolves to nothing rather than to bytes. A run whose inputs are
all absent needs no object store, and refusing it would have blamed the host for
a fact about the project.

## P11-05 the real Slurm backend

Computation can run on a real cluster, over SSH, through a real `sbatch`. That
is the whole of it, and both halves of the item are about what "real" costs:
something outside RAVEL has to be configured, and something outside RAVEL has to
be true.

The configuration half is `SlurmComputeBackend`, selected with
`--compute-backend slurm` and pointed at a cluster by the `RAVEL_SLURM_*`
settings. A worker told to use a cluster it was not given **refuses to start**,
naming the variable to set. The alternative is the failure this item was written
against: a worker that comes up, takes work, and stalls on the first node that
reaches the queue — an hour later, on a node somebody was watching, with the
reason in a traceback rather than in front of the person who started it.

The other half is the credential, which is the one secret in RAVEL that is not
RAVEL's own. It is read by the worker process from its own environment and
handed to the transport; it is in the settings' launcher-only prefix set, so it
is stripped from the environment of every DSH runtime and tool server RAVEL
starts, and the model never has it to leak; and the backend redacts it out of
every `detail`, progress fact, exception message and `completion_metadata` it
produces, because those end up in PostgreSQL and in logs that outlive the
process that held the secret. Paramiko rather than the `ssh`/`scp` clients for
the same reason: the OpenSSH client takes a password only from a terminal or
from `sshpass`, and `sshpass -p <secret>` puts it in a command line every
account on the host can read from `ps`.

What a Slurm result *is* is stated in `docs/06`. An artifact collected off a
cluster is not marked simulated — it came off a real machine — and it is not
marked as evidence either: whether it satisfies a node's acceptance criteria is
Review's judgement against criteria frozen before the run, and a backend that
asserted its own result was admissible would be deciding its own case.

| Requirement | Demonstrated by |
|---|---|
| A worker told to use a cluster it was not given refuses to start, and says what to set | `test_p11_05_a_worker_told_to_use_a_cluster_it_was_not_given_refuses_to_start` — `build_registry` under `--compute-backend slurm`, with neither coordinate, with a host and no account, and with both |
| The password reaches the worker and nothing else | `test_p11_05_the_cluster_password_reaches_the_worker_and_nothing_else` — asserted by *value* against a deployment that really has a password, in the tool-server environment, in the start-up banner, and through the redactor |
| Submission, polling, collection and the state vocabulary | `tests/integration/backends/test_slurm_collection.py` (nine cases, against a scripted cluster) and `tests/unit/backends/slurm/` |
| The credential is not in what collection records | `tests/integration/backends/test_slurm_collection.py::test_the_credential_is_not_in_what_collection_records` — a cluster that echoes the password on every command |
| **Live certification against a real cluster** | `test_p11_05_a_real_cluster_runs_the_workspace_preparation_built` — **`BLOCKED_EXTERNAL`**: no Slurm endpoint or credentials have been supplied, so the case skips naming `RAVEL_SLURM_HOST`, `RAVEL_SLURM_USERNAME`, a credential and `RAVEL_RASPA_DATA_DIR` |

The item is therefore **PARTIAL** and not done, and the honest statement of what
is missing is one sentence: whether a given cluster accepts RAVEL's job script
is a fact about that cluster — whether the account may submit, whether the
filesystem RAVEL writes into is mounted, whether the software the contract names
is installed — and no test in this repository can establish it. The release gate
reports the row as skipped rather than as certified, which is the reason its two
verdicts are different words. Supplying an endpoint and running
`RAVEL_REQUIRE_SLURM=1 make phase11-acceptance` is what closes it.

## P11-06 the human laboratory channel

A bench reached through a person, and the three things that follow from RAVEL
not being able to see one.

**Nothing here ever reports `RUNNING`.** A state meaning "somebody is probably
working on it" would be an observation RAVEL never made, and `WAITING_EXTERNAL`
is the state that says what is actually true — with its own clock, so an
unanswered wait ends in `TIMED_OUT` rather than in a run that looks busy
forever.

**The handover is a row, not a file.** The Slurm backend finds a job again by
reading a file it uploaded beside it; a bench has nowhere to upload a file to,
so the durable record is `lab_handovers`, keyed by `(project_id, node_id,
execution_contract_version, attempt)`. A revised contract is different work and
gets its own handover, so files sent under the old terms cannot count towards
the new run.

**What arrived is a fact and what a delivery claims is not.** A delivery
completes a run when the *recorded* names cover every name in the handover's
frozen `required_outputs` — never when the delivery says it brought everything,
and never on a file a mock produced. The upload door refuses a name the contract
never required *before a byte is stored*, and the backend's comparison is what
finishes a run, so an arbitrary file cannot complete one at either end. That is
the directive's sentence, and it is asserted here at both places it could be
false: the door's own refusals in `tests/integration/gateway/test_lab_handover.py`,
and the run's in the case below.

The person's own steps are the Gateway's. Any project member may upload — the
person who ran the experiment is the one holding the file, and requiring the
owner to relay it would mean recording that somebody uploaded a file they never
saw — and the version carries `created_by`, so the record says who it was. A lab
user's deviation is *recorded*, not adjudicated: it is written against the
node's frozen contract with `permitted` false, and it travels to the run on the
same signal a delivery does, because a run waiting on a bench is blocked in a
durable wait and is never polled.

| Requirement | Demonstrated by |
|---|---|
| The chain from Master's contract to the record Master reads | `test_p11_06_masters_contract_reaches_a_bench_and_comes_back_as_a_record` — a real workflow, a real materialized package, a real durable wait, the person's upload through the route's own function, the real signal, and the Execution Record afterwards |
| A bench is never reported as running | the same case: the node and its job are both `WAITING_EXTERNAL` while the bench has the work, and no Execution Record exists |
| Uploading does not complete a run | `tests/integration/gateway/test_lab_handover.py::test_every_owed_output_recorded_still_leaves_the_run_waiting` |
| A delivery that does not cover what is owed does not finish the run | `test_p11_06_a_delivery_that_does_not_cover_what_is_owed_does_not_finish_it` — one output uploaded, both claimed, the run still waiting with the missing name in the record; the second upload is what ends it. `tests/integration/backends/test_lab_backend.py::test_a_delivery_that_claims_what_it_never_uploaded_cannot_finish_the_run` is the same claim at the backend's port |
| A name the contract never required is refused before a byte is stored | `tests/integration/gateway/test_lab_handover.py::test_an_upload_that_answers_nothing_is_refused_before_a_byte_is_stored` — the owed list in the message, and nothing written |
| A bench's deviation stops the run for Master | `test_p11_06_a_bench_that_reports_a_deviation_stops_the_run_for_master` — `WAITING_DECISION`, the deviation named on the Execution Record, and no retry |
| The package a bench is handed is one RAVEL built | `tests/integration/backends/test_lab_backend.py` and `tests/unit/preparation/test_lab.py` |
| **Live certification against a real bench** | **`BLOCKED_EXTERNAL`: missing human input.** No person has been handed one of these packages and done the experiment, and there is no case to skip for it — a live bench certification is somebody doing work, not code that runs. L-29 states what that leaves unexercised |

The channel's own claim is complete: every step above is asserted against the
real stack, with the test playing the lab user through the same function the
Gateway's route calls. What is missing is not a test but a bench, and L-29
enumerates what a real one would exercise that no test can.

## P11-07 the release gate

The matrix above answers "does every item have a case, and did the cases pass".
A release asks a different question — is the *system* certifiable — and the
difference is where this item's risk lives. A matrix can be all green while lint
is red, while a suite nobody runs is broken, or while a live certification was
never attempted, and none of those is visible in a table that was never asked
about them. `scripts/release_gate.py` is the answer: seventeen rows, each one an
area Phase 11 promises, run in the order a reader would fix them — static
analysis first, then the suites cheapest to most expensive, then the four that
need something RAVEL does not own.

**A row that did not run is not a row that passed.** The verdict is one word
from three, and the middle one is not a polite `CERTIFIED`:
`CERTIFIED` is every row ran and passed; `PARTIALLY_CERTIFIED` is every row that
*could* run passed and at least one could not, for a reason outside this
repository, printed with the dependency it named; `NOT_CERTIFIED` is a row that
ran and failed. A skip is matched against a table of dependencies this repository
knows — a model credential, a research contact address, a Slurm host — and a skip
that matches nothing is reported as **unattributed** rather than filed under
"external", because a skip nobody can explain is a row where nothing is known and
nobody has said why. The exit code is the verdict, so a release cannot be
declared green by a run that skipped what it could not do.

**The test/deployment queue split is part of the item.** Every integration suite
ran on the deployment's Temporal task queue until this item, which meant a test
could put work in front of a deployment's Execution Worker and have it answered
against a database the test does not own. Both halves are now refused rather than
trusted to configuration: `integration_settings` overrides the queue to
`ravel-v0-test-<uuid>`, per process, so two suites never take each other's
workflows either; and it *checks* the override against what `Settings()` reads
from `.env` and raises if they match, so deleting the line fails rather than
passing quietly. The database interlock above it — the `_test` suffix rule — is
the same refusal one layer down.

| Requirement | Demonstrated by |
|---|---|
| Every area Phase 11 promises is a row | `test_p11_07_every_area_phase_11_promises_is_a_row` — the seventeen named rather than counted, because a count passes if a row is swapped for a different one |
| Every path a row names exists | `test_p11_07_a_row_that_names_a_file_that_does_not_exist_is_caught` — `compute-preparation` named a module that did not exist, and pytest exits 4 on that, so the row would have failed for a reason with nothing to do with what it was evidence for |
| A row that did not run is not a row that passed | `test_p11_07_a_row_that_did_not_run_is_not_a_row_that_passed` — a skip and a row that never ran are both `PARTIALLY_CERTIFIED`, and the exit code is asserted with the word |
| One failed row is not certified whatever the others say | `test_p11_07_one_failed_row_is_not_certified_whatever_the_others_say` — a failure outranks a gap, because it describes a known defect rather than an unknown one |
| A skip nobody can attribute is reported as one | `test_p11_07_a_skip_nobody_can_attribute_is_reported_as_one` — both directions: a known credential is recognised and a sentence naming none is not, since a rule that matched everything would pass on the first half alone |
| Tests do not share a queue with a deployment | `tests/integration/conftest.py`'s refusal, asserted by the suites themselves — every integration case now runs on its own queue, and one configured with the deployment's is refused at fixture time |
