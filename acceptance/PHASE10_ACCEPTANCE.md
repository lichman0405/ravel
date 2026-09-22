# RAVEL Phase 10 Acceptance

Phase 10 is Architecture Convergence & True Autonomy. It is not complete while
any item below is `SKIP`, `MISSING`, or `FAIL`.

Run it with:

```bash
make phase10-acceptance
```

That command runs `tests/acceptance -m phase10` and prints a pass/fail row for
every item in this file, then the same for the twenty worker-level items in the
table at the end. An item nobody wrote a test for is printed as `MISSING`
rather than left blank: the whole point of the matrix is that an item nobody
checks looks exactly like an item that passes.

Phase 10 does not relax Phase 9's acceptance. A01–A20 and the seven extra gates
in `V0_ACCEPTANCE.md` still stand, and P10-19 is the item that says so.

## P10-01 latest DSH re-evaluated

The pinned DeepSeek Harness was re-examined against the release that is current
now, rather than assumed to be current because it was current in Phase 0. What
was compared, what was found, and why the pin holds are recorded in
`PHASE10_DSH_SPIKE_REPORT.md`.

## P10-02 single-host decision evidence-backed

The decision to keep one runtime per `(project_id, role)` — rather than run one
DSH Host per CVM carrying many projects as sessions — is supported by evidence
about what the pinned and newer releases actually do: per-session Agent Presets
are unreachable through the Python SDK, and an MCP tool call carries no session
identity, so a shared server would have to trust a model-supplied scope. The
decision, what would reopen it, and the deviation it keeps are recorded in
`SINGLE_DSH_BLOCKER_REPORT.md`.

## P10-03 five real DSH agent roles exist

Master, Research, Review, Compute Worker, and Experimental Worker are five
distinct DSH sessions with five distinct tool scopes. A role's authority is a
property of the process it was launched with, and no role holds another's
tools.

The five are wired in as well as named. The Supervisor builds one seat per role
and the loop hands each node to the seat `NODE_EXECUTOR` names for its type —
including a RESEARCH node, which reaches the Research Agent and lives a whole
life there: the seat begins it through `begin_research`, its own authorized way
in, and hands the result over with `submit_research_record`, which writes the
record and moves the node to the FINAL checkpoint in one transaction. Research
decides nothing and runs nothing: no backend is called, no Execution Record is
written, and the verdict is still Review's.

That verdict has to be about something. A research node owes no Execution
Record and produces no artifacts, so the package Review judges it from carries
the record instead — the claims it was assembled from, the sources those rest
on, and the conflicts between them, read through the same function the Research
seat reads its own ledger through. A judge given only the keys an execution
node fills would read a finished task as a non-delivery, and one did, in those
words, on the first live run.

A new project is missing three things, and Master writes all three. First
`commit_research_contract`: what the user asked for, in the words they asked it
in, and the scientific problem that goal was turned into. That row is what a
Research seat reads as the terms its task is answered under — without it the
seat's first read is refused, which is what a live run found when the only
writers of that row in the whole tree were two test fixtures. It is Master's
alone: a seat that could state its own question would be stating the terms it is
judged against. It changes no node, so it is deliberately *not* a DAG mutation,
and it is written once — a change to what the user wants is a new project.

Then `commit_success_contract`: what would count as answering the question, what
would count as answering it the other way, when to stop early, and what to do if
the evidence runs out. This one is not paperwork. Every ending A20 names is a
claim about results, and a claim is measured against a frozen definition —
*inconclusive* included, since a project whose policy is "keep going" has
nothing to be inconclusive against. Until the first version is written the
project cannot be concluded at all, and writing it is what takes the project out
of `CREATED`: `SuccessContractRepository` performs the move in the same
transaction as the write, so a project cannot hold a definition of success while
its own status says it has none. A later version is a different act — the
project was aiming somewhere and now aims somewhere else — so it carries a
Decision Record with the reasoning that made Master change it. Like the research
contract, it changes no node and is deliberately *not* a DAG mutation.

And Master has to be able to read what it is deciding about. A node that stops
waits at `WAITING_DECISION`, which nothing but Master can move — and a Worker's
contract refusing something and a Review withholding the pre-flight clearance
both park a node there, for opposite reasons and with opposite answers. So the
reason travels with the state: `read_project_state` reports every stopped node
with the verdict that stopped it, diagnosis and recommendations included. It is
a reading and stays one — the package a verdict is written from is Review's
window onto one node, and Master holds no part of it. A seat that is asked to
change the plan and cannot see why the plan stopped is a seat being asked to
guess, which is how a live Master came to commit the same refused node twice.

Then the roadmap, which is Master's to write. A project is created with no
stages, and every DAG tool takes a stage — a node is filed under one and
`expand_dag_phase` expands one that already exists — so `commit_roadmap_phase`
is the first *planning* act a live Master performs on a new project. It is a DAG
mutation like the rest, and Master is still the only role that holds one.
Committing a stage writes no Decision Record by design: a decision records what
changed among the nodes, and a stage on its own commits none — the decision
arrives with the expansion that gives it work.

And a plan is refused where it is written, not where it fails. A node whose
terms cannot be carried out is not a mistake to be discovered by the machinery:
a COMPUTATION or EXPERIMENT node committed with no acceptance criteria can never
start, and a required output that is not a file name can never be delivered —
the backend names the artifact after it, the completeness check compares the two
by equality, and the storage key is built from it. Both are refused at the tool
that wrote them, with the node and the offending value quoted, so the session
that can fix the plan is the one that reads the refusal. The second one earned
its place the hard way: a live Master wrote a sentence where a file name goes,
and the project spent forty-five minutes waiting on a run whose workflow had
already died five retries deep in an activity no seat could see.

## P10-04 Compute Worker live

The Compute Worker seat takes real turns on computation nodes that are running,
through the pinned harness. It reads its contract and its task's state; it does
not execute the computation, which stays Temporal's and the backend's.

## P10-05 Experimental Worker live

The Experimental Worker seat takes real turns on experiment nodes, including
nodes waiting on a lab. It is the only role that may say one of the four
permitted things to a bench.

## P10-06 Compute Backend remains Mock

The backends a deployment registers are `MockComputeBackend` and
`MockLabBackend`, and nothing else. Phase 10 does not implement real HPC or
Slurm.

## P10-07 Lab Backend remains Mock

No real LIMS or instrument adapter exists anywhere in the tree — not
registered, and not present as a class the runtime could be pointed at.

## P10-08 no worker bypass

No normal path carries a node's work to Review without the Worker that owns
that node having taken the turn that began it.

## P10-09 worker contract enforcement

Every request a Worker can make — retry, parameter change, substitution, action
— is decided by the Execution Contract through a code-layer lookup. An
out-of-envelope request is refused, recorded as a deviation for Master, and the
task stops; the Worker's own words are recorded and are not an input to the
verdict.

## P10-10 worker death/recovery

A Worker session that dies does not lose the run. The project continues, and
the next turn is served from authoritative execution state rather than from the
memory of the process that died.

## P10-11 Master death/recovery

A Master session that dies does not lose the project. The project survives, and
Master is rebuilt from Project State and continues correctly.

## P10-12 Project Supervisor autonomous

The unattended Project Supervisor discovers active projects from PostgreSQL,
drives each one, survives the failure of one project's loop without stopping
the others, leaves paused and ended projects alone, and resumes from
PostgreSQL after a restart. No human names a project for it.

## P10-13 no manual run_project requirement

Nothing in the deployment's normal operation requires
`scripts/run_project.py`. It remains as a debugging, recovery, and
administration tool, and the server starts, discovers, and drives projects
without it.

## P10-14 long external wait/resume

A node that waits on something outside RAVEL holds no live LLM turn and no
resident process while it waits, and the wait ends by an event: the run
resumes, is judged, and records what it received.

## P10-15 TUI disconnect does not stop project

Closing the console does not stop RAVEL. The console is a client; the
supervisor keeps discovering and driving active projects after it exits, and
`Ctrl-C` on the deployment is what stops them.

## P10-16 multi-project isolation

Two projects run at once without reaching each other. Each project's sessions
have their own scope, their own working directory, their own contracts,
artifacts, and evidence — and a session that names another project's node
fails at the tool authorization layer, not in a prompt.

## P10-17 live five-agent autonomous certification

The item has two cases, because the question "does the whole thing work when
every seat is a live agent" has two halves that a live model can answer
differently.

**The certification case.** A project is driven from discovery to one of A20's
four endings by five live agents, unattended, with every seat a real DSH session
and every turn a real model call against a real tool server. The run leaves
behind a plan, a decision, a verdict, and an Execution Record, and every one of
the five seats took at least one live turn. All five seats are dispatched, which
is what makes that reachable: the Research seat is handed a RESEARCH node
exactly as a Worker seat is handed a computation or an experiment, and the stage
the specification's unattended path names — `User Goal → Master → RESEARCH →
Research Agent → real evidence → Master` — has a seat at every step of it.

**The research-driven case.** The same supervisor, the same five live seats, the
same mock backends, under an objective that asks a *question* rather than
ordering work. It asserts what the architecture owns and not what the science
returns: the project reached one of the four endings, the ending is the Decision
Record that names it, and every node that was carried out was carried out by the
seat that owns its type — a RESEARCH node leaves a Research Record and never an
Execution Record, a COMPUTATION or EXPERIMENT node the other way round. Any of
the four endings passes.

Four things about this item are worth knowing before reading its rows, because
all four were learned from runs that failed.

The first is what a live run *cannot* end as. V0's backends are mocks, a mock's
artifacts are marked `simulated`, and a live Review reads that mark and refuses to
let such a result satisfy criteria written against real work — correctly, and
`P10-W10`/`P10-W11` are what say so. So a project whose criteria describe a real
computation can end FAILED, INCONCLUSIVE or CANCELLED, and not COMPLETED. The
certification is written for that: a run that ended FAILED with a verdict behind
it shows as much about the architecture as one that ended COMPLETED, and what it
is *not* allowed to do is fail because a seat was never reached.

The second is why the certification's objective is a work order. It was a
question twice, and each wording failed the item for its own reason — which is
what the two-case split settles. The first made the laboratory half wait on the
computational half — compute the series, then confirm the leading candidate —
and that put the fifth seat out of reach by construction: no computation can pass
under mock backends, so no candidate is ever nominated, so an experiment to
confirm one would measure nothing, so a live Master declines to commit one and
says why in a Decision Record. The item then failed on a plan that was *right*.
The second stated the two benches as independent deliverables and kept the
question — "establish whether niobium doping raises the conductivity of TiO2 by
at least 15%" — and a live Master planned the literature first, read research
records reporting that the protocols could not be fixed from what was
retrievable, and concluded INCONCLUSIVE in writing. That ending was correct:
the evidence did not settle the question, so there was nothing left to compute or
measure, and the run had reached three seats.

The difference between those two failures is the difference between a question
and a work order, and it is not about how a plan is staged. A question has one
deliverable — the answer — so a run that cannot produce it is *finished*, and
ending there is right. A work order has three, so stopping after the first is an
incomplete delivery rather than a result. The certification's requester therefore
asks for three pieces of work, one per work seat, each with the input that bench
needs written into the objective: the composition grid and reference value for
the computation, the specimen and conditions for the measurement. No bench's
inputs depend on what the literature did or did not yield, so the run's coverage
of the five seats does not depend on that either. Master still decides the plan —
which seat does what, in what order — and nothing in the objective or the test
says otherwise.

The third wording failure came later, in a re-run of the case that had just
passed, and one level down. The objective described the specimen as
"characterised" without saying that it already was, and a live Master read the
adjective as a precondition nobody had established: "Deliverable (3) needs a
physical specimen and an instrument, and the project has no evidence yet that
either exists. Committing a measurement node blind would either sit unexecutable
in the plan or invite a value to be filled in from literature." It spent a
research task establishing availability instead, and ended INCONCLUSIVE with four
nodes and no experimental seat. Nothing about that reading is wrong — a work
order that leaves a bench's readiness to be discovered invites exactly that check
— and that is why the fix belongs in the objective rather than in Master: the
requester has a bench, so the requester says so, and says too that no deliverable
is gated on another's findings. What the run does with the premise is still
Master's to decide, and the next run shows it deciding: the reworded objective
produced a Master that committed "three dependency-free root nodes in a single
decision — a RESEARCH node for the literature route, a COMPUTATION node for the
anchored series values, and an EXPERIMENT node for the bench measurement", all
three of which were carried out by their seats, in a run that ended
INCONCLUSIVE in 10:45. The item has now passed twice and failed once.

The fourth is what the second case is for. Reaching all five seats is a claim
about the plan, and a plan's shape is a live model's judgement; asserting it
under a question-shaped objective would be asserting that a live model must find
an answer, which is not a property of the architecture, and the ending such a
test demanded would be the one it got whether or not the evidence supported it.
So the two halves are separated: coverage is asserted where the objective asks
for coverage, and the ending is left to the science where the objective asks a
question. Neither case mocks Research, relaxes Review, hides an evidence gap
from Master, or asks any seat to behave unscientifically to make a row pass.

## P10-18 real Research provenance

Research reads the real Internet, and the Evidence Ledger row for a source is
the retrieval: the URL the bytes came from, the time they were read, the hash of
the bytes that were kept, and the tier with the rule that assigned it. A page
RAVEL read is distinguishable, in a column, from a result RAVEL produced.

## P10-19 original A01-A20 remain green

`acceptance/V0_ACCEPTANCE.md` is unchanged, and all twenty items plus the seven
extra gates still pass. Phase 10 adds; it does not relax.

## P10-20 one-command server startup

`scripts/run_v0.sh` — `make up` — starts infrastructure, migrations, the
Gateway, the Temporal Execution Worker, and the Project Supervisor, and opens
the console. The console is not the server: the process named for hosting
activities is documented and commented as infrastructure rather than as one of
the five agents, so that "worker" does not mean three things at once.

---

# Behaviour this phase adds that is not an item

Written down because it changes what an unattended deployment costs and how it
ends, and because it is behaviour rather than an acceptance criterion — there is
no new item for it, and `KNOWN_LIMITATIONS.md` L-23 is where its limits are.

The execution seats get one turn per `(node, status)` episode. The four
dispatches that are not execution seats — the decision, the pre-flight
clearance, the final verdict, the ending — are questions rather than episodes:
their condition holds until somebody answers, so they are asked again while it
holds, at most `max_turns_per_question` times (three by default). Past that the
loop stops asking, records the round as unmoved even though something may be in
flight, and its stall detector ends the run as halted — the same ending a
stopped project already got. The Supervisor's re-drive is therefore a bounded
retry rather than a spin: an unanswered question costs three turns per attempt
instead of one per round forever.

No new vocabulary was invented for this. A "stuck" project status would put a
runtime's problem into the scientific record, which is not where it belongs.

# Worker-level items

Phase 10M's twenty worker items are the detail behind P10-03 to P10-16: what a
Worker is, what it may reach, what happens when it asks for something it may
not have, and what survives when a process dies. They are demonstrated by cases
named `test_p10_wNN_...` in the same suite and are printed as their own rows.

| Item | What it demonstrates |
|---|---|
| P10-W01 | The supervisor's Worker seats are real DSH sessions, not scripts |
| P10-W02 | The Experimental Worker seat is a real DSH session |
| P10-W03 | Both Workers are task-scoped identities: the session is the task's |
| P10-W04 | A Worker is told about one task, not about the plan |
| P10-W05 | A Worker reaches only its own contract, and holds five tools between the two seats |
| P10-W06 | A Worker cannot mutate the Scientific DAG |
| P10-W07 | A Worker cannot alter Acceptance Criteria |
| P10-W08 | The Compute Worker's turn reaches the MockComputeBackend execution path |
| P10-W09 | The Experimental Worker's turn reaches the MockLabBackend execution path |
| P10-W10 | Every artifact a mock produced is marked simulated |
| P10-W11 | A simulated artifact cannot become Evidence |
| P10-W12 | A retry the contract permits is carried out |
| P10-W13 | An out-of-contract request is refused at the code layer |
| P10-W14 | An unauthorized substitution is refused |
| P10-W15 | An experimental deviation escalates to Master rather than being answered by the Worker |
| P10-W16 | A Worker waiting on something external needs no live turn and no process |
| P10-W17 | A Worker's identity continues after a long wait ends |
| P10-W18 | A dead Worker session is rebuilt from authoritative execution state |
| P10-W19 | Two projects' Workers cannot reach each other's context, contract, or artifacts |
| P10-W20 | No run bypasses the Worker that owns it |

The row for a `P10-WNN` item is decided exactly the way a `P10-NN` row is:
cases named for it, `FAIL` if any failed, `SKIP` if all skipped, and `MISSING`
if there are none.
