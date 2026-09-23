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
about them. `scripts/release_gate.py` is the answer: eighteen rows, each one an
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
| Every area Phase 11 promises is a row | `test_p11_07_every_area_phase_11_promises_is_a_row` — the eighteen named rather than counted, because a count passes if a row is swapped for a different one |
| Every path a row names exists | `test_p11_07_a_row_that_names_a_file_that_does_not_exist_is_caught` — `compute-preparation` named a module that did not exist, and pytest exits 4 on that, so the row would have failed for a reason with nothing to do with what it was evidence for |
| A row that did not run is not a row that passed | `test_p11_07_a_row_that_did_not_run_is_not_a_row_that_passed` — a skip and a row that never ran are both `PARTIALLY_CERTIFIED`, and the exit code is asserted with the word |
| One failed row is not certified whatever the others say | `test_p11_07_one_failed_row_is_not_certified_whatever_the_others_say` — a failure outranks a gap, because it describes a known defect rather than an unknown one |
| A skip nobody can attribute is reported as one | `test_p11_07_a_skip_nobody_can_attribute_is_reported_as_one` — both directions: a known credential is recognised and a sentence naming none is not, since a rule that matched everything would pass on the first half alone |
| Tests do not share a queue with a deployment | `tests/integration/conftest.py`'s refusal, asserted by the suites themselves — every integration case now runs on its own queue, and one configured with the deployment's is refused at fixture time |

## P11-08 users, projects and membership

The three Phase 11 items before this one gave RAVEL a real cluster and a real
bench; this one is about who may point them at something. It is the item that
decides what "a member" means, and the answer is deliberately narrow: **every
authority in the system is a row in `project_memberships`, re-read on every
request, and the only thing that creates a person is an operator at a terminal.**

**Opening a project is the one membership nobody confers.** A project's first
membership is granted to nobody by nobody, so the person who opens a project
owns it; every later membership names the account that conferred it, and a
granter cannot confer more authority than they hold. The alternative — a
separate "create the owner" step — would be a second thing that has to happen
for a project to be directable, and a project whose owner was never granted is
a state nobody can repair, since owners are the only ones who may grant.

That makes `POST /projects` the one membership route open to any authenticated
caller, and it is worth being explicit about why that is not an escalation: a
lab user who opens a project owns a project that did not exist a moment ago and
holds exactly the standing in it that they had in nobody's project before. What
the route cannot do is put them into somebody else's work — that needs an owner
of *that* project — and what it needs first is an account, which the Gateway
still cannot make.

**Withdrawal is history, never deletion and never an edit.** A withdrawn
membership keeps its row, with `revoked_at` and `revoked_by`; a role change is
a withdrawal followed by a grant, so what a row says is what was true while it
was live. The uniqueness constraint that used to be `(project, user)` is a
*partial* unique index over live rows, which is what lets the same person be
granted again later without the two grants being confused for one. And a
project always keeps an owner: the last one cannot step down, refused in the
repository so the script, the routes and any future screen obey the same rule.

**The authority is read from the row, not carried in the token.** A token is a
fact about who is asking. That is what makes a withdrawal take effect on the
next request rather than when a token expires, and the case below asserts it
with a token minted *before* the withdrawal — a case that signed in again
afterwards would pass whether the role came from the row or from a claim frozen
at login. Two reads decide what a caller may open, and both now mean *live*:
`for_user`, which every authorization decision goes through, and
`memberships_of`, which builds the project list and `/auth/me`. The second
filtered on the user and not on the revocation, which was correct only for as
long as no membership could be withdrawn — the defect this item's case found.

**An administrator is an operational role in a project, not a superuser.** An
owner may confer `ADMIN`, and the ranking exists so that authority cannot be
*escalated* — a lab user promoting itself to owner — rather than to make an
administrator more powerful than an owner: `may_direct_project` is false for
`ADMIN`, so an administrator cannot decide the research route, and an
administrator who is not a member of a project is answered exactly as a
stranger is. There is no platform-wide role, and no route lists projects a
caller is not in.

| Requirement | Demonstrated by |
|---|---|
| Server Operator → create User | `test_p11_08_a_person_is_made_at_a_terminal_and_not_over_http` — the operator's own script run against the Gateway's database, then that account logging in over a real socket |
| PROJECT_OWNER → create Project | `test_p11_08_an_owner_opens_a_project_and_the_ownership_is_a_row` — the creator is the first membership, granted to nobody by nobody |
| PROJECT_OWNER → add an existing LAB_USER / ADMIN | `test_p11_08_an_owner_adds_accounts_that_already_exist` — both roles, with the granter on the record |
| 不要增加 open registration | `test_p11_08_a_person_is_made_at_a_terminal_and_not_over_http` probes **every** `POST` route with an owner's token and with nobody's, and reads the accounts back out of PostgreSQL; `tests/integration/gateway/test_members.py::test_no_route_creates_an_account` is the same claim through `TestClient` |
| Adding a member is not making a person | the same case: an unknown username is a 404 and the account count does not move |
| PROJECT_OWNER 管理 membership | `tests/integration/gateway/test_members.py` — the grant, the withdrawal and the member list, all owner-only |
| LAB_USER 不能管理成员 | `test_p11_08_a_lab_user_and_an_administrator_cannot_manage_members` — 403 on the list, on the grant and on the withdrawal, while the same token still reads the project |
| ADMIN 不能管理成员 | the same case, second role |
| ADMIN 是 runtime/admin role | `test_p11_08_an_administrator_is_not_a_platform_superuser` — a project the administrator holds no membership in answers 404, in the same words as a project that never existed, and the membership row itself reports `may_direct_project` false |
| 不能删除最后一个 PROJECT_OWNER | `test_p11_08_a_project_never_loses_its_owner` — refused while the owner is the last one, allowed once a second owner is in place, and refused again for that one |
| membership mutation 必须 audit | `test_p11_08_every_membership_change_is_a_fact_on_the_record` — `MEMBER_ADDED` / `MEMBER_REVOKED` with the actor and the membership identifier, and the withdrawn row still in the table with `revoked_at` and `revoked_by` |
| role 必须从 DB re-read, 不依赖旧 token 中缓存权限 | `test_p11_08_authority_comes_from_the_row_and_not_the_token` — one token through three states of the same membership: refused after withdrawal, and reading again as `ADMIN` after a re-grant, with no second login |
| The project list means live memberships | `tests/integration/state/test_identity.py::test_a_withdrawn_membership_is_not_a_project_the_user_belongs_to` — the read the list and `/auth/me` are built from, and the one that did not filter on the revocation |
| A withdrawal reaches an account that is still active | `tests/integration/gateway/test_projects.py` and `test_members.py` — the membership is the authority; the account is untouched |

## P11-09 role-specific surfaces

`docs/08` §5 gives three people three views, and until this item the console
had one screen and a role check in front of it. This item is the claim that the
surfaces are *genuinely different*: not one screen with fields hidden, but three
screens whose actions differ because the authority behind them differs. The
difference is not cosmetic — an administrator's console has no approval control
because an administrator has no approval authority, and the absence is the
honest rendering of that rather than a disabled button.

**The mapping is data, not three branches.** `ROLE_SCREENS` is a dictionary from
role to screen class and `screen_for_role` reads it, which is what makes "which
role gets which screen" a thing a test can check without starting anything. A
role the program does not know draws a sentence saying so instead of a default,
because the only defensible default would be the *most* restricted screen and
getting that wrong in the other direction shows a lab user the whole DAG.

**The operator's screen is the one that had to be built.** The owner's and the
bench's grew panels; the administrator's is new, and it exists because
everything an operator needs — is the supervisor alive, is Temporal reachable,
what has the work actually run on, which jobs are stuck, what had to be
recovered — is scattered across the runtime rather than present in a project.
It reads a *project-scoped* view of all of it: `projects_held` is a count and
the supervisor's own report of the other projects is filtered out, because a
membership in this project is what admits a caller and other people's research
is not the price of an uptime check.

**One panel reads a process's account of itself, and says so.**
`runtime_services` is the one place a screen trusts something other than a
record, for the reason that there is nothing else to read: whether a supervisor
is running is not derivable from any row, because a project it stopped driving
and a project with nothing left to do are the same state. What is *not* taken on
trust is the service's word for its own health — the age is computed by the
Gateway from a clock it owns, against a cadence the service promised in its own
report — and a service that has never reported is drawn as one that has never
reported rather than as one that died, because those send an operator to two
different places.

| Requirement | Demonstrated by |
|---|---|
| OwnerScreen ≥ Master 对话 | `test_p11_09_the_owners_screen_carries_every_thing_the_item_lists` — a line typed into the composer reaches a `MasterPort` and the answer comes back onto the panel |
| OwnerScreen ≥ project state / Scientific DAG | the same case: the status line, `your role: PROJECT_OWNER`, and the DAG table's row count |
| OwnerScreen ≥ research results / Evidence | the same case, plus `test_an_owner_reads_research_evidence_and_approvals` in `tests/e2e/test_tui.py` |
| OwnerScreen ≥ execution / reviews / decisions / approvals | the same case: each panel is non-empty, and the two that are *empty* in this world say so in words rather than rendering blank |
| OwnerScreen ≥ pause / resume | the same case, pressed as keys and read back from the project row in PostgreSQL; `test_the_owner_can_stop_and_start_the_project` is the same claim one layer down |
| OwnerScreen ≥ membership | the same case reads the member table and who conferred each row; `test_the_owner_adds_and_withdraws_a_member_from_the_console` drives both writes and finds the withdrawn row still in the table |
| OwnerScreen ≥ project switch / create | the same case opens a project from the console, then reaches it with `ctrl+n` and finds the creator is its first membership |
| LabScreen ≥ assigned experiment tasks | `test_p11_09_the_benchs_screen_hands_the_work_back_through_the_console` — one row, the experiment, and the contract's objective and required outputs in the instruction panel |
| LabScreen ≥ prepared protocol | the same case: the package's own manifest, named by the materializer that wrote it |
| LabScreen ≥ required outputs, and what is owed | the same case, in both states — `still owed` for both before anything arrives, `✓ … — sent` and one still owed after the first file, and neither after the second |
| LabScreen ≥ upload | the same case: two files typed into the path field, each attached to an output chosen from the contract's own list, and the run **finishes because of what was recorded** — not because the screen said so |
| LabScreen ≥ deviation | `test_a18_a_lab_user_sees_their_task_uploads_and_reports_a_deviation` in `tests/acceptance/test_surfaces.py`, which raises one through the screen and checks the node did not move |
| LabScreen ≥ messages / status | `test_p11_09_the_benchs_screen_hands_the_work_back_through_the_console` asserts both panels in words, including that RAVEL reports the bench as `WAITING_EXTERNAL` rather than as running — which is the P11-06 rule, read off the screen this item added |
| AdminScreen ≥ DSH runtime / Temporal | `test_p11_09_the_operators_screen_carries_every_thing_the_item_lists` — the harness panel names the pin's own tag and provider, and the Temporal panel names the task queue |
| AdminScreen ≥ Supervisor | the same case, with a report written the way the supervisor writes one: `alive`, how long ago it spoke and against what budget, and the instance that is answering |
| AdminScreen ≥ backend health | the same case: what the work has actually been handed to, whether Slurm is configured, and the route's own sentence that reachability is **not probed from the Gateway** |
| AdminScreen ≥ Slurm configuration | the same case, and `tests/integration/gateway/test_admin.py::test_the_slurm_panel_names_the_setting_that_holds_the_secret` — the secret is represented by the *name* of the setting that holds it, and the value never reaches the response |
| AdminScreen ≥ running / failed jobs | the same case on a project nothing has run in, which is where the absence is a sentence: `No backend job has ever been submitted for this project.` |
| AdminScreen ≥ reconciliation | the same case, `No run has had to be recovered in this project.`, and `test_a_recovery_is_reported_with_what_it_was_measured_against` for the state that is not an absence |
| AdminScreen ≥ logs | the same case: where the harness home and the log root are on this deployment |
| Admin 不得 approve science | `test_p11_09_an_administrator_can_neither_approve_nor_mutate_anything` — no such action exists on the screen, **and** the administrator's own token is refused at the approval door |
| Admin 不得 modify DAG | the same case: six write routes refused with the administrator's token, and the DAG read back and compared whole — so a node that was cancelled or re-bound counts as a mutation too |
| Admin 不得 modify experimental scientific conditions | the same case: the pause, the envelope and the message routes are all refused, so there is no path from the operator's screen to the terms a bench works under |
| 三类 surface 必须真正不同 | `test_p11_09_no_two_roles_are_drawn_by_the_same_screen` — three roles, three distinct classes, each with its own bindings; and the owner/admin prohibitions above, which are what "different" means past the class list |

## P11-10 managed services

Three long-running processes, and the item is about what a deployment does when
one of them is not there. `infra/systemd/ravel-{gateway,supervisor,temporal-worker}.service`
are the three, `scripts/install_services.sh` installs them, and
`scripts/run_{gateway,supervisor,temporal_worker}.py` are what they start.

**The restart policy is a file, so it is read as a file.** `Restart=always`
rather than `on-failure`, because this system's failures are not all crashes: a
supervisor whose loop raises and exits cleanly is a project that has stopped
moving, and `on-failure` would leave it there. `KillSignal=SIGTERM` is written
out though it is the default, because it is load bearing — the application's
lifespan and the supervisor's shutdown path run on that signal and write the row
that says the stop was deliberate.

**A stopped service and a killed one are told apart in the record.** A process
that receives `SIGTERM` records `stopped_at` and stays quiet; a process that is
killed says nothing and its last beat recedes. Both end in silence, and for two
poll intervals they are indistinguishable from the screen — so the field is what
makes a deploy legible as a deploy. A beating process clears it, because a
process that came back is running. The row is never deleted: the table is in
`UPDATABLE_TABLES`, so a `DELETE` is refused by a statement-level trigger, and a
service able to erase its own row could erase one belonging to a service that
never said anything.

**The acceptance chain is proved in two halves, because it is two claims.** The
restart is systemd's and is demonstrated with a real transient user unit running
the real `scripts/run_supervisor.py`, killed with `SIGKILL`, with the restart
policy taken from the checked-in unit rather than restated in the test. The
recovery is RAVEL's and is demonstrated in process, where a scripted Master is
affordable. Neither stands in for the other, and both name the same entry point.
The systemd case skips on a host with no user-level systemd, which is a
statement about the host rather than about the code.

**The credential is not on a command line.** The units take their environment
from `EnvironmentFile`, which systemd reads and does not report back;
`RAVEL_SLURM_PASSWORD` is stripped from every tool server's environment by
`Settings._LAUNCHER_ONLY`, and `systemctl show` would print anything in
`ExecStart` to anyone who asked.

| Requirement | Demonstrated by |
|---|---|
| systemd 自动重启 | `test_p11_10_systemd_restarts_a_killed_supervisor_and_a_stopped_one_says_so` — a real transient unit, `SIGKILL`, then a different `MainPID` in the row and `NRestarts ≥ 1` |
| 该行描述的是 systemd 正在运行的那个进程 | the same case: the pid in `runtime_services.instance` is compared against `systemctl show -p MainPID` |
| graceful shutdown 被记录，且与崩溃可区分 | the same case, both halves — the killed process leaves `stopped_at` null, and `systemctl stop` (which sends the unit's own `KillSignal`) leaves it set |
| structured logs | the same case: the unit runs with `RAVEL_LOG_FORMAT=json` and the journal lines parse as one object each carrying `ts`, `level`, `service`, `logger`, `message` |
| 心跳/健康指示 | `test_p11_10_the_services_endpoint_separates_the_four_states` — never-reported, running, and deliberately stopped, read from `/projects/{id}/runtime/services`; and `test_a_service_that_never_reported_is_not_a_service_that_died` one layer down |
| active project recovery | `test_p11_10_a_project_survives_the_supervisor_that_was_driving_it` — a node `RUNNING` in PostgreSQL on both sides of the handover, and a second, independently constructed supervisor carries the project to `COMPLETED` |
| 不重复 backend submission | the same case, counted at the port: every `(project, node, attempt)` was answered with exactly one `backend_job_ref`, which is the contract `WorkBackend.submit` is written to |
| Restart 策略、账号、目录、EnvironmentFile | `tests/unit/test_service_units.py` — read out of the three files, including the mapping from unit to entry point and that the entry point exists |
| systemd 真的接受这三个 unit | the same module: `systemd-analyze verify` on the checked-in files, and again on what a default install would write with a prefix that exists, where the exit status is asserted too |
| 安装器只替换前缀和账号 | the same module: the dry run's output is compared for equality against the two substitutions computed in Python |
| 结构化日志的字段与拒绝未知格式 | `tests/unit/test_service_logs.py` — one object per line, `extra` fields flat beside the reserved ones, a traceback inside its own record, and an unknown format refused at start-up rather than read as text |

Two limits are stated rather than hidden. The systemd case creates a project so
that the restarted supervisor has something to log about, and it blanks the API
key so that no live model turn is spent on a liveness question — the *planning*
half of recovery is therefore the scripted case's, not a live one's. And
`tests/unit/test_service_units.py` reads the unit files rather than starting
them: a deployment that edits its installed copy has units this repository has
not verified, which is why `install_services.sh` rewrites only the two
coordinates it is given.

## P11-11 the whole chain, certified end to end

The ten items above each certify a part. This one is the claim that they join:
that on one project, in one run, what each leg produces is what the next leg was
planned from, and that the project's ending rests on the row the first leg
wrote. A chain of parts that each pass their own suite can still fail to join,
and until this item nothing said that this one does not.

**The join is stated four ways, and all four are recorded rather than
remembered.** The order the work is planned in is the first: the measuring stage
cannot exist before the reading has ended and left a claim, because its criteria
are written *from* that claim. The second is the DAG's own record —
`dependencies` on both measuring nodes naming the reading node, with
`JoinPolicy.ALL` — which is immutable once written and is the only statement
about where work came from that survives the session that planned it. The third
is the criteria: each frozen `AcceptanceCriterion` is `LITERATURE_DERIVED` with
the evidence row's identifier in `provenance_ref`, a pairing the contract's own
validator requires, so a citation cannot be left off by omission. The fourth is
provenance at the decision level: the `DecisionDraft` that creates the measuring
stage carries the claim in `evidence_refs`, and so does the ending.

**What a dependency is, and is not, here.** It is the DAG's record of where work
came from. It is not a readiness gate: V0 records dependencies and does not wait
on them, and only `replan_after_failure` reads them. The case is written to
assert what the system does and not what it would be convenient for it to do.

**The two legs software cannot supply, and how the case supplies them.** No
Slurm host is configured on this machine, so the computation leg runs
`MockComputeBackend` — and the case asserts, on a whole joined run, the thing
the marking exists for: every artifact that mock produced carries
`kind="simulated"` with its scenario in `provenance`, and the set of simulated
artifact identifiers is disjoint from every claim's `source_refs`. A person at a
bench is likewise not something a test can schedule, so in both cases the person
*is* the test: it files one body against each name the handover's frozen
`required_outputs` holds — read off the handover at run time, because a live
Master writes its own output names and a person who filed a fixed pair at a
package that asked for others would be answering nothing — through the same
function the Gateway's upload route calls, and delivers through the real
`deliver_external_result` signal. Each of those bodies says in its first line
that it is a harness stub and not a measurement, so the one leg a person would
have to supply is legible as not supplied.

**What the deployment can prepare decides what a live computation can be.** The
live case asserts the shape of a run, and one part of that shape belongs to the
deployment rather than to the model. This host registers two materializers, the
RASPA one and the bench one; the bench one will refuse a contract that states no
procedure, and the RASPA one will refuse a contract that names no temperature,
pressure, cycles or framework rather than choosing them itself. A live Master
asked to compute something that is not a molecular simulation therefore has
three correct ways forward — name an environment nothing here can prepare and be
refused `UNSUPPORTED_ENVIRONMENT`, name RASPA and be refused
`MISSING_SCIENTIFIC_PARAMETER`, or re-commit the deliverable in a form that can
run — and the item's first live run took all three in order, each with its
rationale on the record, ending with the computation re-committed as a RESEARCH
node. So the live case does not require a computation to have run: it requires
that the plan holds all three kinds of work, and that nothing simulated is cited
as evidence, and the marking of a mock's output is asserted on the scripted
case, where the mock really did the work — as well as on every computation a
live plan did run, which the run that certified the item supplied.

**The run that passed then did contain computations, and Review threw them out.**
Its plan reached the mock, and every computation that ran was reviewed at FINAL
and failed on the marking — `Mock/simulated/inadmissible flag present on both
artifacts — explicit failure condition met` — under a criterion the Master had
itself written into the contract: that the delivered files carry nothing
formatted or labelled as simulated. The bench leg was refused in the same terms,
on the first line of the file the person filed (`certification harness stub —
not a measurement`), and the reading leg was accepted in the same run on ten
sources with retrievable DOIs and fourteen attributed claims. So both halves of
the mock guarantee have been exercised by seats that were not told about it —
the marking where a computation ran, the disjointness always — and the ending
was `CONCLUDE_INCONCLUSIVE`, with the two blocked lines reported as blocked and
the one shortcut that would have produced numbers refused in writing.

**The case had a defect of its own, and the matrix is what found it.** The
scripted case read the project's records inside one `read_only()` block and then
asked two of those repositories a question after the block closed; a repository
used past its session opens a second transaction on a connection nothing
returns, which sits `idle in transaction` holding a read lock. The next test's
`TRUNCATE` then times out — the failure landing on the test *after* the one that
caused it, which is what the harness's message says. Two green runs had ended
before the next one began, so nothing else had seen it; the matrix runs the
module in the same process as the rest and surfaced it as five failing rows and
seventeen failed set-ups that were not about the product at all. Both leaks are
closed and the case leaves no connection checked out — measured with a pool
probe, not assumed — and the note in `tests/acceptance/test_review.py` on the
same hazard is the precedent the case now follows.

**The item also put this module on the release path.** `scripts/release_gate.py`
gained an eighteenth row, `full-chain-e2e` — this module under `-k whole_chain`
with `RAVEL_REQUIRE_DSH=1`, so that both cases run and the live one cannot skip
— because a gate whose seventeen other rows each certify a suite can return
`CERTIFIED` about a phase whose central claim, that the legs join, nothing has
ever run. Its
live half is twenty-four minutes of a real Master planning, which is why it is
the last row. `test_p11_07_every_area_phase_11_promises_is_a_row` names it with
the other seventeen, so deleting the row fails a test rather than quietly
leaving the gate one row shorter.

**Two cases, because there are two questions.** The first is scripted and
complete — Master, Review and the three seats are scripts, everything under them
is the deployment — so that the join is asserted on a known plan: a stage that
reads, and a stage that measures, with the second planned from the first. The
second is live: five real agents drive one project from its work order to an
ending, with the Internet real and the person played by the test. What it
asserts is the *shape* of the run — one of A20's four endings with Master's
Decision Record behind it, five seats that each took a live turn, all three
kinds of work in the plan, a ledger whose sources were hashed and snapshotted,
and nothing simulated admitted as evidence — because which ending a live Master
reaches is a scientific result and not this file's to fix. Its work order also
says outright that none of the three pieces depends on another and none may be
gated on what another turns up, so a plan with a derived stage in it would be
this case failing rather than passing: the *derived* join is the scripted case's
claim, and the live case's is that a project of real agents and real evidence
runs every leg and ends.

| Requirement | Demonstrated by |
|---|---|
| Master 是唯一修改 DAG 的角色 | the same case: every node's `created_by` names Master, every Decision Record's `authority_check` names Master and records `permitted`, and every node in the plan is named by the `CREATE_NODE` decision that created it — so no work exists that nothing authorized |
| 用户 objective → Master 的 ResearchContract、success contract 与第一段 roadmap | `test_p11_11_one_project_runs_the_whole_chain_to_a_conclusion` — the two contracts are frozen and the reading stage expanded before any node has run |
| Research 真实搜索/深读 → Evidence 与 ResearchRecord | the same case: a source registered with real bytes, the seat's own `record_evidence` and `submit_research_record` handlers, one claim in the ledger citing it, one Research Record on the node; and in the live case, every readable source carrying a `content_hash` and a `snapshot_ref` after a real fetch |
| Master 读回研究结果并以它规划下一段 | the same case: Master reads the reading leg back through its own `read_research_result` tool — the record it is handed is the one the seat filed, and the claim it cites is the row the ledger holds — and only then creates the measuring nodes, whose `DecisionDraft` carries that claim and which record it as a dependency |
| **Review** 对每一段做出裁决，且裁决不进入 DAG | the same case: the reading node is judged against its Execution Contract — a RESEARCH node freezes no criteria, so its definition of done is the record it filed — and the measured nodes against the criteria frozen before they ran; each node has a verdict, and no verdict in the run changed the plan |
| Evidence/ResearchRecord 进入 criteria provenance | the same case: every frozen criterion on the measuring nodes is `LITERATURE_DERIVED` with `provenance_ref` equal to the claim's identifier |
| COMPUTATION 节点 → 运行 → Execution Record | the scripted case: a real `NodeRunWorkflow` through Temporal to an Execution Record whose artifacts are all marked simulated. In the live case a computation runs only if its contract passes preparation, which this deployment's materializers decide (see above) — on the item's first live run none did, and the Master re-committed the deliverable; on the run that certified it they did, and the executions came back `COMPLETED` with a `COMPLETE` delivery verdict. The materialized package that is real in both cases is the bench's, built by the real `LabMaterializer` from the contract; the solver workspace's own preparation is P11-04's item |
| EXPERIMENT 节点 → HumanLabBackend → 人交付 → Execution Record → Review | the same case: the handover's `required_outputs` are the contract's, the person's upload is recorded `created_by` them, the delivery is the real signal, and the execution comes back with `backend == HumanLabBackend.name` |
| 最终 SUCCESS/FAILED/INCONCLUSIVE | both cases: the project reaches a terminal status and the Decision Record that status means (`ACCEPT_RESULT`, `REJECT_RESULT`, `CONCLUDE_INCONCLUSIVE`, `TERMINATE_PROJECT`) exists, with the claim cited on the scripted ending |
| Mock 的 artifact 不能被当作科学证据 | the scripted case, positively: every mock artifact is simulated and the simulated set is disjoint from every claim's `source_refs`. The live case asserts the disjointness unconditionally and the marking over every computation it actually ran — which is the honest pairing, because a run that computed nothing has nothing to mark — and on the run that certified the item the marking was tested end to end: the mock's output reached a Review seat, and the seat failed it for being marked |
| 全链在真实 agent 下跑通一次 | `test_p11_11_live_agents_drive_the_whole_chain_to_a_conclusion` — five seats, real model turns, real Internet, a person at the bench, one ending |
| **Live Slurm 上的 compute leg** | **`BLOCKED_EXTERNAL`: missing cluster.** No Slurm endpoint or credentials have been supplied (`RAVEL_SLURM_HOST`, `RAVEL_SLURM_USERNAME`, `RAVEL_SLURM_PASSWORD` are all unset), so no node of either case ran on a cluster; `test_p11_05_a_real_cluster_runs_the_workspace_preparation_built` skips for the same reason, and L-22 states what that leaves uncertified |
| **Live 真实实验台上的 experiment leg** | **`BLOCKED_EXTERNAL`: missing human input.** No person has been handed a package and run the experiment; in both cases the person is this process. L-29 states what a real bench would exercise that no test can |

The item is therefore **PARTIALLY_CERTIFIED** rather than done, and the two
blockers are the two above: software cannot supply a cluster or a person, and
neither run had both at once. L-32 states what that leaves uncertified, in the
terms of the join rather than of the legs — the parts are each certified, and
what has not happened is the pair of them inside one project. Supplying a Slurm
endpoint and standing somebody at a bench is what closes it; the software half
is asserted now, and the two legs that are not real are marked as not real
everywhere they appear in the record.
