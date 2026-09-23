# Known Limitations

What RAVEL V0 does not do, cannot do under its current design, or does only
because of a property of the machine it was built on. Written as the limits were
found rather than reconstructed at the end, so that an entry says what was
actually observed and not what the design intended.

Each entry says what is limited, why, and what a reader should not conclude from
it. Nothing here is a defect being hidden: a limitation that is written down is
one a reader can plan around, and one that is not is a surprise.

---

## L-01 — Crash recovery depends on `WorkBackend.submit` being idempotent

- **Since:** Phase 5
- **Where:** `src/ravel/execution/backends.py` (`WorkBackend`), `start_job` in
  `src/ravel/execution/temporal/activities.py`

A worker killed after a backend has accepted work but before the reference is
recorded leaves a row that is indistinguishable from one whose `submit` never
arrived. RAVEL recovers by calling `submit` again, which starts the work twice
unless the backend recognises the call as the same work.

The port therefore *requires* `submit` to be idempotent in
`(project_id, node_id, attempt)`, and carries all three fields for that reason.
This is a requirement RAVEL places on every implementor; it is not something
RAVEL can supply.

**Do not conclude** that a duplicated experiment is impossible. It is
impossible exactly to the extent that backends honour the port. `MockComputeBackend`
and `MockLabBackend` do. `SlurmComputeBackend` does, by three overlapping checks
— the run's own submission record, the scheduler's job-name lookup, and the
`sbatch` command itself (see L-22 for what has and has not been run against a
real cluster). A LIMS adapter must too.

## L-02 — Recovery after a worker dies takes up to `job_activity_timeout_seconds`

- **Since:** Phase 5
- **Where:** `job_activity_timeout_seconds` in `src/ravel/config.py`

Temporal cannot distinguish a worker that died from one that is merely slow, so
an activity belonging to a dead worker is not retried until its
`start_to_close_timeout` expires. That expiry *is* the recovery time: with the
deployment default of 120 s, a crashed worker's work resumes within roughly two
minutes, not immediately.

Shorter means faster recovery and less tolerance for a genuinely slow activity.
The tests run at 5 s.

**Do not conclude** that a restart is instantaneous. Nothing is lost — that is
what the restart tests assert — but "nothing is lost" and "nothing waits" are
different claims.

## L-03 — The test database is built by `create_all`, not by migrations

- **Since:** Phase 1
- **Where:** `tests/integration/conftest.py`

`create_all` never alters a table that already exists, so a test database
created before a constraint was added silently runs without that constraint.
This produced one genuinely mystifying failure before it was understood.

`_assert_schema_is_current` now compares every `CheckConstraint` the models
declare against `pg_constraint` and refuses to run when one is missing, naming
it. That catches missing constraints; it does not catch a changed column type or
a dropped index.

**Do not conclude** that a green integration suite proves the migrations are
correct. Migration correctness is a separate question, and the migration/model
parity test that would answer it is not yet written.

## L-04 — Migration upgrade/downgrade guards read the current code

- **Since:** Phase 1
- **Where:** `migrations/versions/`

A migration's `upgrade()` installs the guard functions as they are defined
*today*, not as they were when the revision was written. Replaying history from
an empty database installs the current guards at every step.

**Do not conclude** that running migrations to an old revision reproduces the
system as it was then. It reproduces the schema as it was then with today's
guard bodies — which is the right behaviour for a fresh deployment and the wrong
mental model for archaeology.

## L-05 — No search-provider credential is configured

- **Since:** Phase 4
- **Where:** `.env`, `docs/IMPLEMENTATION_DEVIATIONS.md` (D-003)

`.env` carries no search API key. Research reaches real sources by direct HTTP
against Crossref, arXiv, and publisher URLs whose robots rules permit it — no
mocking, no model priors as provenance. This is a constraint on *which* sources
are reachable, not on whether retrieved ones are real.

**Do not conclude** that Research is degraded or simulated. It is real; it is
narrower than a paid search API would make it.

## L-06 — Playwright's browser cannot run on this host

- **Since:** Phase 4
- **Where:** `src/ravel/research/`

Chromium is missing seven shared libraries and there is no passwordless sudo to
install them. Browser-based retrieval is implemented and its address checks run
before every navigation, but it cannot be exercised here.

**Do not conclude** that browser retrieval is untested because it is unused. It
is unused because it is untestable on this machine; the code path exists and is
unverified end to end.

## L-07 — The host intercepts DNS, so address-based URL policy is not authoritative

- **Since:** Phase 4
- **Where:** `src/ravel/research/addressing.py`; `SECURITY_NOTES.md` §1;
  `docs/IMPLEMENTATION_DEVIATIONS.md` (D-004)

Every hostname on this machine resolves into `198.18.56.0/15`, a benchmarking
range answered by a local interceptor. A connection to such an address is
answered by the interceptor, not by any internal service, so the address check
cannot by itself distinguish a journal from the instance-metadata service; a
name check covers that case instead.

**Do not conclude** that the guard is decorative. It is doing real work, and the
name check is what makes it sufficient here. On a host that resolves names
honestly, the address check becomes the stronger of the two.

## L-08 — Two research-gateway risks remain open

- **Since:** Phase 4
- **Where:** `SECURITY_NOTES.md` §1, §3

A DNS-rebinding TOCTOU window between the address check and the connection, and
a gap in the `RAVEL_POSTGRES_DSN` scrub for harness-launched processes.

**Do not conclude** that either is exploitable by a model acting alone. Both
require a hostile resolver or a hostile deployment environment; both are
recorded with the deployment assumption that makes them matter.

## L-09 — A renamed guard function leaves its predecessor in place

- **Since:** Phase 1
- **Where:** `_SUPERSEDED_FUNCTIONS` in `src/ravel/state/guards.py`

PostgreSQL cannot be told "and no other function starting with this prefix".
A guard renamed in a later migration is dropped by name through an explicit
list, so a name that is never added to that list survives.

**Do not conclude** that the database enforces exactly the guard set the code
declares. It enforces that set plus anything left over from an earlier revision
that nobody listed.

## L-10 — `users` is append-only, so a user cannot be deactivated

- **Since:** Phase 1
- **Where:** `src/ravel/state/tables.py`

`is_active` exists and defaults to true. No code path flips it, and the table's
immutability guards refuse updates.

**Do not conclude** that deactivation was forgotten, or that somebody who
should lose their access cannot. Two other things do that job: the authority
envelope, which is Master's and the User's lever, and — since Phase 11 —
withdrawing the membership, which takes away everything the account could do in
a project without touching the account. What remains absent is narrower than it
used to be: `is_active` is still a column nothing flips, so an account that
should be unable to log in at all has no path, and an account is never deleted
because a record of who did the work outlives their access. See L-17.

## L-11 — Dependencies are immutable once added

- **Since:** Phase 2
- **Where:** `src/ravel/state/repositories/dag.py`

`add_edge` was removed. A dependency, once recorded, cannot be edited or
deleted; changing the graph means opening new work.

**Do not conclude** that the DAG is rigid by accident. This is the "immutable
record > overwrite" principle applied to the graph's shape, and the cost is that
a mistaken edge must be superseded rather than corrected.

## L-12 — Test workflows accumulate in the Temporal namespace

- **Since:** Phase 5
- **Where:** `tests/integration/temporal/`

Each durable-execution test uses a private task queue so that a run abandoned by
a failing test is never picked up by the next test's worker. Abandoned runs are
not terminated, so a long-lived development namespace accumulates them.

**Do not conclude** that the Temporal namespace is a record of anything.
PostgreSQL is the record; the namespace is execution state, and the dev stack's
`scripts/dev_down.sh` discards it.

## L-13 — A mock backend forgets its jobs when its process restarts

- **Since:** Phase 6
- **Where:** `_MockBackend._jobs` in `src/ravel/backends/mocks.py`

Mock compute and lab jobs live in the backend's memory. A worker that restarts
while a mock is running loses the job, and `status` for a reference the mock no
longer holds raises rather than guessing — deliberately, because a mock that
answered for work it does not have would be inventing a result. The durable
layer's own crash recovery (L-02) therefore covers a restart of the *worker*,
but a run in flight across a restart of the *backend process* is not resumed by
these mocks.

What *is* preserved across a restart is the identity of the work: a reference is
derived from `(project, node, attempt)` by hashing, so a `submit` after a
restart returns the same reference the first process used, and the retried
activity resumes rather than starting a second experiment. The retry then calls
`status` on a job the new process never accepted, which raises.

**Do not conclude** that RAVEL's crash recovery is untested, or that it depends
on a mock's memory. `tests/integration/temporal/test_node_run.py` kills a worker
mid-run and observes the run complete; the mocks survive that because the
killed process is the worker, not the backend. A real backend stores its own
jobs, which is the property `WorkBackend` is written against.

**And do not conclude** that a reference is human-readable. It is sixteen
hexadecimal characters of a SHA-256 of the work, because a reference is a
`REF` column — 64 characters — and spelling the work out through three
identifier paths is seventy-six. The work it stands for is a row RAVEL already
has, so nothing is lost; a person reading a bare reference cannot tell which
node it belongs to, and has to look it up.

## L-14 — The browser resolves names itself, so its connections cannot be pinned

- **Since:** Phase 8
- **Where:** `BrowserNavigator` and `_Session` in `src/ravel/research/browser.py`

RAVEL's fetcher connects to the address `addressing.address_for` validated
rather than to the name, which closes the DNS-rebinding race for everything it
reads. The browser cannot be given the same treatment: Playwright makes its own
connections inside a Chromium process this code does not own, and there is no
network backend to substitute. A page that navigates to, or whose subresources
point at, a host whose DNS answers publicly for the guard's lookup and
`169.254.169.254` for the browser's is therefore reached.

What is in place: the URL handed to `open`/`links` is guarded before a browser
starts, every request the page makes goes through a `context.route("**/*")`
handler that guards it, service workers are blocked so there is no path around
that handler, and an aborted navigation is re-raised as `UnsafeURL` rather than
recorded as a source that could not be read. The metadata-service *names* are
refused whatever they resolve to, which covers the highest-value target.

**Do not conclude** that the browser is unguarded, or that the fetch path shares
this gap — `Fetcher` is pinned and its behaviour is asserted in
`tests/unit/test_research_addressing.py`. **And do not conclude** that this is
unbounded: the attacker needs authoritative DNS for a host the page visits, and
needs the browser's lookup to land differently from the guard's, in a window
between the two. What RAVEL cannot do is rule it out.

## L-15 — The DSH gate needs a model credential the repository does not carry

- **Since:** Phase 0
- **Where:** `.env` (`DEEPSEEK_API_KEY`), `tests/dsh/conftest.py` (`model_credential`),
  `scripts/test_all.sh` (`RAVEL_REQUIRE_DSH=1`)

`tests/dsh` drives the pinned harness through a real model turn, which needs a
`DEEPSEEK_API_KEY`. `.env.example` ships that key empty, and the version of
`.env` in this working tree leaves it empty as well — the same size and the same
contents as the example, with only the local stack's credentials filled in
(PostgreSQL, MinIO, and the development JWT secret).

The suite behaves as designed under that condition: `pytest tests/dsh` reports
3 passed and 8 skipped, because the tests that need a turn skip rather than
substitute. But `scripts/test_all.sh` sets `RAVEL_REQUIRE_DSH=1` for exactly
this reason — *the harness gate is a gate: it must not be able to pass by not
running* — so the default `scripts/test_all.sh` run **fails** on a host with no
credential, at the `harness gate` stage, after lint, unit, and integration have
passed.

**Do not conclude** that the DSH integration is unverified. It was verified
against the pinned release and the record is in `vendor/DSH_PIN.json`: the
wheels install and import, the runtime self-reports `0.1.5-rc.1`, a real
end-to-end turn with a real model executed a tool and wrote a file that was
checked on disk, and the session log's durability was inspected. What that
verification cannot do is re-run itself on a host where nobody has supplied a
credential, and it is the re-run — not the first run — that a CI gate would be
making.

**Do not conclude** that the credential was lost by the implementation. It was
supplied to the Phase 0 spike from the operator's own shell environment and was
never written into `.env`; a key in a file inside the repository would be the
larger problem.

**Follow-up:** an operator running `scripts/test_all.sh` must export
`DEEPSEEK_API_KEY`, or accept that the run stops at the harness gate. Every
other suite in that script passes without it.

## L-16 — An upload is buffered whole in the Gateway's process

- **Since:** Phase 8
- **Where:** `src/ravel/gateway/routes/artifacts.py` (`MAX_UPLOAD_BYTES`)

`MAX_UPLOAD_BYTES` is 256 MiB, and the body is read into memory before it is
stored, because the object store hashes the whole object on the way in. Two
concurrent uploads at the cap are half a gigabyte of resident memory, and the
cap exists because of that rather than as a policy about size.

The check is done twice on purpose — declared `Content-Length` when it is sent,
and the counted length as well, because a chunked upload declares nothing.
Checking only the header would make the cap advisory.

**Do not conclude** that a large artifact is rejected by the design. 256 MiB is
well above anything V0 research produces; what V0 does not have is a streaming
upload path, and adding one means changing how the store hashes.

## L-17 — Accounts are created at a terminal, and authority could not be withdrawn

- **Since:** Phase 8
- **Resolved (the second half):** 2026-09-23, in Phase 11. A membership can be
  withdrawn, and the withdrawal is a fact rather than a deletion:
  `MembershipRepository.revoke` writes `revoked_at` and `revoked_by` onto the
  row and emits `MEMBER_REVOKED`, the uniqueness constraint that used to be
  `(project, user)` is a partial unique index over live rows so the same person
  can be granted again later, and every read that answers "what may this user do
  here" returns live rows only. The last owner cannot step down — refused in the
  repository, so the script, the Gateway's routes and any future screen obey the
  same rule — and a role change is a withdrawal followed by a grant rather than
  an edit, so what a row says is what was true while it was live.
  `scripts/create_account.py` remains the only thing that creates a person, and
  the Gateway now serves the rest: `POST /projects` opens one, `POST
  /projects/{id}/members` confers, `POST /projects/{id}/members/{user_id}/revoke`
  withdraws, and all three require the caller to be the project's owner. What is
  *still* absent is `L-10`: nothing deactivates an account, and nothing expires
  a membership on a clock.
- **Where:** `scripts/create_account.py`; `MembershipRepository` in
  `src/ravel/state/repositories/identity.py`; `src/ravel/gateway/routes/members.py`

The Gateway authenticates a person and cannot create one. Every `/auth` route
reads an account, and the only writes it performs are on login chains.
Membership is the one thing in RAVEL that manufactures authority, so it is
deliberately unreachable over HTTP by whoever happens to be logged in, and
provisioning is `scripts/create_account.py` on the host.

**`grant` used to be the only write on that table** — no revoke, no expiry, and
no path that lowered a role, so a user who should no longer direct a project
kept the authority until the project was over. That is the half the **Resolved**
line above describes, and the sentence is kept here in the past tense because it
is what the entry was about. Withdrawing is a command an operator now finds: on
the owner's screen, or at the terminal for a project nobody can log in to.

**Do not conclude** that the grant path is unaudited or unguarded. It is
checked twice — a granter must hold authority at least equal to what is being
granted, and only a project's *first* membership may be created without naming a
granter — and the append-only guards make every grant a record. The same holds
of the withdrawal: `revoke` refuses a caller who may not direct the project and
refuses the project's last owner, and both directions write an event naming
who did it.

## L-18 — The loop is a process, not a service

- **Since:** Phase 9
- **Updated in Phase 10:** a loop now has something that starts it again —
  `ProjectSupervisor` (`src/ravel/execution/supervisor.py`), started by
  `scripts/run_v0.sh` or `make supervisor`.
- **Updated in Phase 11 (P11-10):** the supervisor is installed as a service —
  `infra/systemd/ravel-supervisor.service`, `Restart=always` — so "until
  somebody does" is now systemd rather than a person. There is still no lease
  and no queue of projects; `L-31` says what that leaves.
- **Where:** `src/ravel/execution/loop.py`; `src/ravel/execution/supervisor.py`;
  `scripts/run_project.py`

`make project PROJECT=<id>` drives one project until it ends or stops moving,
and `ProjectSupervisor` does the same for every active project without being
told which. What has not changed is that both are *processes*: what starts one
again is a service manager rather than a scheduler inside RAVEL — there is no
lease of a project and no queue of them. A supervisor that exits — a crashed
host, an operator's `Ctrl-C` — leaves its projects exactly where PostgreSQL says
they are.

`ProjectLoop` is deterministic RAVEL software — it reads state, starts runs,
notices endings — and the seats that require judgement are filled by agents on
the pinned harness. That division is deliberate and is why there is no
agent-shaped scheduler: which projects run is a deployment's configuration, not
a model's decision, and the supervisor decides nothing about the science.

**Do not conclude** that a project is lost when a loop stops. Everything the
loop reads is in PostgreSQL and everything it started is in Temporal, so
starting it again continues the project rather than restarting it — and
`LoopHalted` is reported as "the project has not ended" rather than as a
failure. What a stopped loop needs is a process, and after Phase 10 that process
is the supervisor rather than a person.

**Do not conclude** either that the supervisor makes a project move that does
not want to. It re-drives a halted loop on its next tick, which is a retry, not
progress: a project whose Master or Review has stopped answering is asked the
same question again, costs what L-23 says it costs, and halts again.


## L-19 — Live agents now drive a whole project, rather than only the DSH spike

- **Since:** Phase 9
- **Resolved:** 2026-09-21. `tests/acceptance/test_phase10_live.py::test_p10_17`
  drives a project from discovery to one of A20's four endings with all five
  seats as live DSH sessions, unattended, in about twenty minutes.
- **Where:** `tests/acceptance/test_phase10_live.py`; `tests/dsh/test_spike.py`

The DSH spike tests had already shown that a real model could start a runtime,
call RAVEL's tools, be refused a tool it does not hold, and recover Master state
from PostgreSQL rather than from harness session context. What was unmeasured was
the *composition*: many consecutive model turns, every replanning decision and
every verdict live, until a project ended — and that is the thing a spike cannot
show, because the failure modes that matter only appear once a plan is long
enough to go wrong.

It has now been measured, twice, and the second time it passed. Both runs are
recorded in working notes this repository does not publish, so what they showed
is stated here rather than cited; between them the live Master cancelled a node
its own failure had orphaned while quoting that node's required outputs by name,
committed four replacement threads with parameters pre-registered in their own
criteria, adapted a research task to a read ceiling nobody had told it about, and
then chose between INCONCLUSIVE and FAILED by quoting the unresolved-uncertainty
policy it had frozen before any work ran. That last paragraph is the one worth
reading: *"every failure criterion in the contract is a claim about evidence of
absence ... nothing supports the negative claim any more than it supports the
positive one."*

What remains scripted is deliberate and is not this limitation. A05, A09, A12,
A17 and A20 still use `ScriptedMaster` and `ScriptedReview`, because those items
are about the loop's *correctness* — does a blocked node propagate, is an
out-of-scope tool refused, does a join wait for every branch — and a live model
is the wrong instrument for a question whose answer must not vary with the
model's mood. The split is: scripted seats test the machinery, and `test_p10_17`
tests whether the machinery is worth anything when the seats are real.

**Do not conclude** that this makes a live run reproducible. It does not, and
P10-17 is written knowing that: it asserts the *shape* of a run — a terminal
ending, every seat reached, a plan, a verdict, an Execution Record — and not
which ending or which plan, because those belong to the science. A passing live
run is evidence that RAVEL can do this, not a guarantee about what it will do
next time.

**Do not conclude** either that a passing run means the science succeeded. Under
V0's mock backends a COMPUTATION or EXPERIMENT node cannot pass a live Review —
the artifacts are marked `simulated` and the judge declines to let them satisfy a
criterion written for a real measurement — so every live run so far has ended
FAILED or INCONCLUSIVE. That is the simulated marking doing its job, and the
live Review's own words are where it is visible: the backend is `mock-compute`,
every artifact carries the note *"produced by a mock backend; not a measurement
and not admissible as evidence"*, and the judge declined to let the system's own
disclaimer satisfy criteria written against real outputs.

**Do not conclude** that "every seat reached" follows from the architecture
alone. It follows from the architecture *and* from an objective that asks for
work all three work seats can be given. A live run on 2026-09-23 is what showed
the difference. Under the question-shaped objective the item then carried —
"establish whether niobium doping raises the conductivity of titanium dioxide by
at least 15% over the undoped baseline ... I will put the two together myself" —
a live Master planned the literature first, two RESEARCH nodes were created, the
two research records reported that the protocols could not be fixed from the
sources that were retrievable, and Master
concluded INCONCLUSIVE in a written Decision naming the missing stream and
refusing to read the outcome as a negative result about niobium. Every turn in
that run was correct, and it reached three of the five seats — because for a
question the answer *is* the deliverable, and there is nothing left to compute
or measure once Research reports that the literature does not settle it. The
item asserted five seats regardless, and so failed a run whose plan was right.

P10-17 is now two cases, and the split is the finding rather than a workaround.
The certification case asks for three pieces of work, one per work seat, with
each bench's inputs — the composition grid, the reference value, the specimen
and conditions — written into the objective, so that no bench waits on what the
literature did or did not yield, and asserts that all five seats took a live
turn. The second case asks a question, asserts that the ending is the Decision
Record naming it and that every node carried out was carried out by the seat
that owns its type, and lets the ending be whichever of A20's four the evidence
supports. Neither case mocks Research, loosens a Review, hides an evidence gap
from Master, or asks any seat to behave unscientifically to make a row pass.

## L-20 — The live-research suite now runs with a contact address

- **Since:** Phase 9
- **Resolved:** 2026-09-19, when `RAVEL_RESEARCH_CONTACT_EMAIL` was set in
  `.env` and `pytest tests/live_research` reported **13 passed, 0 skipped**.
- **Where:** `tests/live_research/`; `.env` (`RAVEL_RESEARCH_CONTACT_EMAIL`)

The suite now reaches the real Internet on every run that has a contact address:
Crossref, OpenAlex, arXiv, PubChem, and direct URL retrieval are all exercised,
and every registered Evidence row carries a real retrieval timestamp, a
retrievable URL, and a content hash matching the stored bytes.

Without a contact address the suite still skips, deliberately: RAVEL will not
fetch anonymously, and a placeholder would defeat the point of asking. The
refusal remains a stop rather than a warning.

**Do not conclude** that the structural half is no longer valuable. Gate 1 still
asserts every connector points at a real service and that research cannot be
answered offline; gate 2 still asserts a source nobody read cannot enter the
ledger. Those checks run without credentials and remain the first line of
defense against accidental mock evidence.

**Do not conclude** either that the suite will pass on every network. The
external services RAVEL calls can change, rate-limit, or become unreachable; a
failure there is a network condition, not a RAVEL defect, and the suite treats
it as such.

## L-21 — A mock backend plays one scenario for every node it serves

- **Since:** Phase 6
- **Where:** `MockComputeBackend` / `MockLabBackend` in
  `src/ravel/backends/mocks.py`; `--compute-scenario` in
  `scripts/run_temporal_worker.py` (named `run_worker.py` before Phase 10)

The scenario is fixed when the backend is constructed, and one backend instance
serves every COMPUTATION node in the deployment. So a worker started with
`--compute-scenario COMPUTE_SCIENTIFIC_FAILURE` does not make *one* node fail —
it makes every compute node fail, for as long as that worker runs.

Selecting a scenario per node would mean reading it from the node's contract,
which is the wrong place for it: a real backend decides how the work goes, and a
mock that let the graph dictate its own failure would be testing the graph
against itself.

The consequence is operational. The acceptance suite is unaffected, because each
case builds its own backend for the one scenario it is about. A *deployment*
watches failure handling by restarting the worker pointed at a failure scenario,
and every node of that type then behaves that way. In V0 this is the intended
way to see the loop replan — not a defect, but not obvious from the flag's name
either.

**Do not conclude** that the mock is per-node-configurable and merely defaults
to success. It is not configurable per node at all, and `--compute-scenario` is
a property of the process.

## L-22 — The compute side has a real backend that has never met a real cluster

- **Since:** Phase 4, restated in Phase 10, narrowed in Phase 11 (P11-05)
- **Where:** `src/ravel/backends/mocks.py` (`MockComputeBackend`,
  `MockLabBackend`); `src/ravel/backends/slurm/` (`SlurmComputeBackend`);
  `scripts/run_temporal_worker.py` (`build_registry`, `--compute-backend`);
  `tests/acceptance/test_phase10_backends.py`;
  `tests/acceptance/test_phase11_slurm.py` (the live case that is skipped, and
  the two claims about the deployment that are not)

Until Phase 11 the answer here was "every backend is a mock". P11-05 added
`SlurmComputeBackend`: a real adapter that submits a prepared workspace to a
Slurm cluster over SSH, polls the scheduler, and files the outputs a run's
manifest required as artifacts under the project the run's own submission record
names. It is written and unit-tested against a scripted cluster that parses the
commands it sends, and exercised against a real PostgreSQL and a real object
store in `tests/integration/backends/test_slurm_collection.py`.

**It has never been run against a real cluster.** No Slurm endpoint and no
credentials were supplied to this build, so every claim about it is a claim
about the code rather than about a queue. That is the limitation, stated first
because it is the one that matters: the parts a real cluster would exercise —
host key verification against a production scheduler, `sbatch` under a site's
partition policy, a queue that answers slowly, a filesystem that returns a file
no shell would — have not been exercised at all. It is recorded as
`BLOCKED_EXTERNAL` on the Phase 11 item rather than as a completed
certification.

Written out, because this is the kind of sentence that gets softer every time it
is paraphrased:

```text
MockComputeBackend = YES  (the default; what an untouched deployment runs)
MockLabBackend     = YES  (the default)

SlurmComputeBackend = WRITTEN, opt-in, never run against a real cluster
HumanLabBackend     = WRITTEN, opt-in, never run against a real bench (L-29)
Real VASP          = NO
Real LAMMPS        = NO
Real GROMACS       = NO
Real RASPA         = NO  (preparation generates its inputs; nothing runs them)
Real LIMS          = NO
Robot lab          = NO
```

`HumanLabBackend` is P11-06's and is real where it counts and not where it would
be a lie: what it hands over is a prepared package, and what completes a run is a
file a person uploaded, recorded with a hash and an author and checked against
the outputs the contract required. What it is *not* is a measurement RAVEL
performed — nothing in this repository can make a bench produce a number — so
`Real laboratory` is no longer `NO` in the sense of "there is no channel", and is
still `NO` in the sense that matters to a reader of results: RAVEL has never
received one from a bench. See L-29 for the laboratory's own limitation.

The three properties that made the old statement worth asserting are still
asserted, each one now exact rather than inclusive:

- **What a deployment runs.** `build_registry` with a worker's default arguments
  builds the two mocks. The real backend is reached only by `--compute-backend
  slurm`, and asking for it without a host or a username is refused at start-up
  rather than at the first node that reaches the queue.
- **What could be handed work.** The whole `ravel` package is walked for
  anything with a backend's shape — a name and the five calls the durable layer
  makes — and the exact set is the two mocks, `SlurmComputeBackend` and
  `HumanLabBackend`. Nothing else: no LIMS, no robot, no wrapper around a
  simulation package.
- **What a mock's output is.** Every artifact a mock produces is marked
  `simulated` in a column, and the Evidence Ledger refuses a simulated artifact
  as a source — so mock output can drive the loop without ever becoming a finding
  RAVEL cites. What a cluster produces is *not* marked simulated, and is not
  marked as evidence either: whether it satisfies a node's criteria is Review's
  judgement against frozen criteria, not a backend's claim about itself.

**Do not conclude** that a real backend is a configuration change away. It is a
`WorkBackend` implementation *plus* the property that makes it safe to retry:
`submit` must be idempotent in `(project_id, node_id, attempt)` (see L-01), and
a real instrument is exactly where that is hard. `SlurmComputeBackend` carries
three overlapping idempotency checks for that reason — the run's own submission
record, the scheduler's job-name lookup, and the `sbatch` command itself.

**Do not conclude** either that writing one is the same as certifying it. The
gap between the two is the paragraph above about a real cluster, and the honest
way to close it is to run a job on one rather than to read the tests.

## L-23 — A question nobody answers stops the project, and the retry repeats

- **Since:** Phase 10
- **Where:** `src/ravel/execution/loop.py`
  (`ProjectLoop.max_turns_per_question`, `_ask`); `src/ravel/execution/supervisor.py`

Four of the loop's dispatches are questions rather than turns on a node's
episode: whether the project can be ended, whether a result passes, whether a
node may run, and what Master decides. Each is asked again while its condition
holds, and asked at most `max_turns_per_question` times (three by default).
After that the loop stops asking, counts its rounds as unmoved, and its stall
detector ends the run as halted.

That is the whole of the escalation. There is no backoff, no notification, and
no fifth agent to notice: the supervisor's next tick re-drives the halted loop,
which asks the same question three more times and halts again. A project in that
state does not progress and does not end; it costs three model turns per
attempt, and each attempt is `max_stalled_rounds` rounds of polling (sixty at
the loop's default, so about thirty seconds at the deployment's half-second
poll).

**Do not conclude** that this bounds an unattended deployment's cost to a
constant. It bounds it *per question per attempt* — the defect it replaced was
one model call per round forever, measured at 4,756 turns in a minute — but a
project whose seat has stopped answering is retried indefinitely, because
stopping the retry would need a policy about how long a research project may
wait, and V0 does not have one. What an operator has is the log line the loop
writes once per exhausted question, and the project's status, which stays
non-terminal.

**Do not conclude** either that RAVEL cannot tell "stuck" from "working". It can,
and reports the difference as a halt with the question named. What it does not
do is *write* that into the project: a "stuck" status would put a runtime's
problem into the scientific record, and whether a project that has stopped
moving should be replanned, narrowed, or abandoned is a decision for a person —
or for Master, once somebody tells it what happened.

## L-24 — A run whose workflow died left the node RUNNING, and nothing ended it

- **Since:** Phase 10, found by a live run of `test_p10_17`
- **Resolved:** 2026-09-22, in Phase 11. `ExecutionReconciler` sweeps the live
  nodes of every project on each supervisor tick, asks Temporal about the one
  run each is supposed to have, and for a run that is gone it ends the job,
  moves the node to `WAITING_DECISION` where Master is asked, and writes an
  immutable `RunReconciliation` saying what it saw — in one transaction. The
  three things the paragraph below says recovery needed are the three it now
  has: a read of Temporal that the control plane makes with a client it only ever
  reads with, a classification that puts the loss on RAVEL's machinery rather
  than on the science, and a record for it. What is deliberately still absent
  is the Execution Record: a run that never reported did nothing RAVEL can
  attest to, so the loss is a question for Master rather than a result. See
  `ravel.domain.reconciliation`, `tests/integration/reconcile/`, and
  `tests/acceptance/test_phase11_reconcile.py`.
- **Where:** `src/ravel/execution/temporal/workflows.py`
  (`NodeRunWorkflow.run`, `_ACTIVITY_RETRY`); `src/ravel/execution/loop.py`
  (`Situation.in_flight`, the stall counter in `ProjectLoop.run`)

Every step of a run after planning is an activity — `start_job`, `check_job`,
`finish_node_run` — and each is retried five times
(`RetryPolicy(maximum_attempts=5)`) before the workflow fails. The last of them
is the one that writes: `finish_node_run` records the Execution Record, the
artifacts and the node's move to REVIEWING. When it exhausted its retries the
workflow ended as FAILED, the activity error was logged by the Temporal worker,
and **PostgreSQL was left holding a node in RUNNING for a run that no longer
existed.**

Nothing recovered from that state, and the reason was the loop's own deliberate
rule: a run in flight is not a stall, so rounds spent on it do not count toward
`max_stalled_rounds` (`loop.py:482`). The supervisor re-drove a project that was
not terminal, the loop read the same RUNNING node, took no turn that could move
it, and waited again. A node in this state was not slow — it was unreachable, and
the project could not reach an ending while it held one.

Seen once, in a live run: a plan whose required output was a sentence rather
than a file name made `finish_node_run` raise
`ValueError` on all five attempts, and the item then spent forty-five minutes
waiting for a workflow that had already died. The plan-side cause is fixed at the
planning gate. The hole itself took a design rather than a patch, and that design
is what the **Resolved** line above describes.

A second live run found something in the recovery itself, and it is worth
recording because the mistake was in the reasoning rather than in the code. The
first version asked Temporal about every node in a live status. A RESEARCH node
is in a live status for as long as its agent is searching, and no workflow was
ever started for it — the Research Agent does that work inside its own turn — so
Temporal answered `NOT_FOUND`, which was true and meant nothing. Three research
nodes of one project were moved to `WAITING_DECISION` mid-search, each with a
reconciliation saying its run had been lost. The sweep now considers only
`WORKER_RUN_NODE_TYPES`, the two node types whose execution *is* a durable run,
because "is this node live" and "does this node have a run" are different
questions and `NodeStatus` answers only the first.

**Do not conclude** that the fix is "catch the error in the activity". Catching
it would hide the fault and still leave the node RUNNING. What recovery needed
was three things the loop did not have, and each is a decision rather than a
patch. First, a liveness fact PostgreSQL can read: a stranded node has a status
and nothing else — no Execution Record, since `finish_node_run` is what writes
one and it never succeeded — and while the workflow id is derivable from the node
and its contract version, "is this run still alive" can only be answered by
asking Temporal, which the loop did not do and held no client for. That is
`TemporalWorkflowProbe`, and it was built to read and to do nothing else. Second,
a rule about who may write an Execution Record for a run that never reported:
that write is a Worker's act in every other path, and `run once` for a node. The
answer was to write none — `RunReconciliation` records that RAVEL looked and what
it saw, and leaves the question of what the loss means to Master. Third, a
termination vocabulary for it — `TerminationStatus` has no value for "RAVEL lost
this run", and `FAILED` would attribute to the backend a failure that was
RAVEL's own. So `RunFailureClass` is a vocabulary of its own, with a fifth member
that is never assigned precisely to mark where the science would have been
judged. Re-running is not the answer either: Temporal's workflow id carries
`REJECT_DUPLICATE`, so a dead run cannot simply be started again, and running the
work again is a decision that opens a new node or a new contract version — which
is Master's to make, and is why recovery ends by asking.

**Do not conclude** either that this is the same as L-23. L-23 is a question a
seat does not answer, and the loop *does* notice it — it halts and says so. This
was a run nothing could notice: the loop's answer to "what is this node waiting
for" was `in_flight`, which is true and useless. Phase 11 opened with it, which
is why the reconciler is a deterministic layer rather than something a seat does:
the fact it needs is a fact, not a judgement, and a seat asked to notice it would
be a seat given the power to end runs.

**Do not conclude** that a recovered node resumes by itself. Recovery ends where
a decision begins: the node sits at `WAITING_DECISION`, the project stays
unfinished, and it stays that way until Master moves it — so a project whose
Master seat is not running waits exactly as L-23 describes, only now with a
record saying why. What is closed is the permanent, silent strand; what is not
is any promise that the work comes back without someone deciding it should.

## L-25 — RAVEL keeps what it reads and gives no seat a way to read it back

- **Since:** Phase 9, when the source gateway was built; found by a live run of
  `test_p10_17` in Phase 10
- **Resolved:** 2026-09-22, in Phase 11. The Research seat holds three reading
  tools — `source_metadata`, `read_source`, `search_source` — and they read the
  bytes RAVEL kept, out of the object store, by the `source_id` a row in the
  Evidence Ledger already carries. `read_source` returns a bounded region of a
  document's text and says where the next region starts; a PDF is addressed by
  page instead of by character, and a scanned or encrypted one is reported as
  having no text rather than guessed at. Every read re-hashes the stored bytes
  against the `content_hash` the row records and refuses on a mismatch, so the
  text and the provenance cannot come apart. No new provenance system exists:
  the tools write nothing, add no row, and return the ledger's own fields as
  `source`. See `ravel.research.deepread`, `src/ravel/mcp/tools/research.py`,
  `tests/integration/research/test_deep_read.py`, and
  `tests/acceptance/test_phase11_deepread.py`.
- **Still open:** the `required_outputs` mismatch in the paragraph below — a
  RESEARCH node's contract still names files, and the seat still has no tool
  that writes one. And a deployment with no object store can read the ledger's
  metadata and not its bytes: `source_metadata` says so rather than failing, and
  the ten in-memory retrievals a session opened itself are readable either way.
- **Where:** `src/ravel/research/fetching.py` (`EXCERPT_CHARS`, `excerpt_of`);
  `src/ravel/mcp/tools/research.py` (`open_source`, `register_source`); the
  absence of any tool that reads an artifact

A source RAVEL opens is fetched for real, capped at `MAX_INLINE_BYTES = 8 MiB`,
hashed, tiered, and written to the object store as a snapshot. What any session
ever sees of it is `excerpt_of`'s output: at most `EXCERPT_CHARS = 600`
characters, and only when the media type is HTML or XHTML — for a PDF, JSON,
XML, or plain text it is the empty string. Of the thirty-one registered tools,
not one returned stored bytes; `ArtifactStore` was reached only by
`register_source`, to write. (The paragraph is written as the state of things
before the resolution above; the excerpt is still the only thing an *opening*
returns, and deliberately so.)

That is not an oversight in the excerpt. Its comment states the intent — *"enough
to see that the page is about what the lead claimed; short enough that the
excerpt is never mistaken for the source"* — and for its purpose it is right. The
limitation is that it is the only reading surface there is. RAVEL can prove it
read a page and cannot read the page.

What it costs was measured in the live run this phase ended on: a RESEARCH node
whose six frozen acceptance criteria asked for a solubility bound and a measured
undoped baseline — two numbers that live in papers — found ten real sources,
opened the ones that were open, and could produce neither, because the one source
carrying the answer was behind a paywall and the rest were longer than six
hundred characters of visible text. The verdict failed the node and said, in
writing, that the access layer explained the failure without excusing it, which
is the correct call. The science did not happen.

One related mismatch lives beside it: `required_outputs` on a RESEARCH node names
files, and the Research seat holds no tool that writes one, so those entries can
never be artifacts. The live run's Review seat judged the delivered dossier on its
content instead and said so — so the system absorbs it — but a contract field that
nothing can satisfy is worse than an absent one.

**Do not conclude** that the fix is to raise `EXCERPT_CHARS`. Six hundred
characters is a considered number for a provenance excerpt, and what is missing is
a *different* surface — a tool through which a seat can read a region of a
snapshot it has already registered, bounded so that a session's context stays
bounded. PDF text extraction is a separate decision with a dependency behind it.
Nor is it obvious that a Research seat should be able to write files: giving it
that tool and removing `required_outputs` from research contracts are two answers
to one question about what that seat is for, and the live Review seat named both.
Phase 10 wired the seat in and proved it takes real turns against the real
Internet; what it may do with what it finds is Phase 11's.

## L-26 — A role session can be configured with the deployment's task queue

- **Since:** Phase 10, when role tool servers began carrying the supervisor's
  coordinates
- **Resolved:** 2026-09-23, in Phase 11, by making the test queue something
  `integration_settings` chooses rather than something it inherits.
  `integration_settings` now builds the queue as `ravel-v0-test-<uuid4>`, beside
  the `_test` database interlock it already applied, so the coordinates a role
  environment is handed are a test's own and there is no configuration left in
  the tree that puts a session on a queue a deployment polls. The interlock is
  written as a pair: the database check refuses a name that is not a test
  database, and the queue check refuses a name that *is* the deployment's, which
  `Settings()` — the same source the deployment reads — is asked for rather than
  compared against a literal. Both are belt-and-braces over a line that already
  makes them unreachable, and the entry says so, because an interlock nobody can
  trip is one that has stopped being a test.
- **Where:** `tests/integration/conftest.py` (`integration_settings`);
  `tests/integration/temporal/conftest.py` (`execution_settings`, which layers a
  second per-test queue on top for the suites that start runs);
  `tests/integration/roles/conftest.py`; `tests/acceptance/test_project.py`;
  `src/ravel/config.py` (`tool_server_env`)

This entry exists because a report said the opposite, and the check that refuted
it is worth more than the claim was. The claim was that tests and the deployment
share the Temporal task queue `ravel-v0`, which would let a workflow started by a
test be delivered to the deployment's Execution Worker. **It never held for any
suite that starts a run.** `execution_settings` — which `tests/e2e`'s `headless`
is built on, and through it the Phase 10 and Phase 11 acceptance runs — overrode
the queue with a private per-test name, and the run's own tool-server environment
said so: the live five-agent certification launched its seats with
`RAVEL_TEMPORAL_TASK_QUEUE=ravel-v0-test-731180fc92f04be59e5846c9895b2bab` and
`RAVEL_POSTGRES_DB=ravel_test` (`dsh_cwd/<project>/*/ravel-role.cordis.patch.yml`
under the run's `runtime_dir`).

What was true was narrower, and it is what the fix closes: a role environment
built straight from `integration_settings` — `tests/integration/roles/conftest.py`,
and the one case in `tests/acceptance/test_project.py` that uses it — kept the
default `ravel-v0`, because `tool_server_env()` carries every non-launcher
coordinate to the session that acts on it. Those sessions read and wrote
`ravel_test` while being configured with a queue the deployment is listening on.

**It was inert, and that was never the reason to leave it.** Nothing in
`ravel.mcp` starts a durable run — `start_execution` is the supervisor's loop
reaching the DAG, and no MCP tool so much as imports `NodeRunClient` — so a role
session had no way to put work on that queue. What would have happened if one
did is the whole argument: the run would be answered by a worker whose
activities open the deployment's database, where the test's project does not
exist, and the failure would arrive as a run that never reports rather than as a
queue that was wrong. That is a two-hour diagnosis to reach a one-line cause, and
the next tool that starts work from a session is not the place to discover it.

## L-27 — A REVIEW-typed node is planned, promoted, and never handed to anybody

- **Since:** Phase 10, since the supervisor's loop has held seats at all
- **Resolved:** 2026-09-23, in Phase 11, by abolishing the type rather than by
  teaching the loop to dispatch it. A review is not a kind of work the plan
  contains; it is a checkpoint *on* a node that runs, and RAVEL already asks for
  it through that node's own status — a COMPUTATION or EXPERIMENT node is
  cleared to run by a PRE_RUN verdict, and every node that ran holds at
  REVIEWING until a FINAL verdict moves it. A REVIEW node asked for a second
  copy of that question, on a node that did not exist, and no seat was ever
  going to be given one: `NODE_EXECUTOR` assigned it to the Review Agent, whose
  whole surface is verdicts about *other* nodes, and the loop has no execution
  path into one. So the domain table and the runtime now say the same thing, and
  they say it about a type that is no longer plannable.
  `ravel.domain.state_machines.PLANNABLE_NODE_TYPES` is the set a node may be
  created as; `DagNode.create` refuses anything outside it, so every path into
  the DAG meets the same refusal, and `add_dag_node`/`expand_dag_phase` report
  it to the model with the sentence that says where the checking goes instead.
  `NodeType.REVIEW` stays a member of the enum and stays in the database's check
  constraint, because a node written while the type existed is a record of what
  a project did and has to remain readable — `executor_for` answers `None` for
  it rather than raising, and the model validator tolerates a stored executor
  that no longer has a table entry. No migration was written and none is needed:
  cancelling a node is a DAG mutation that requires Master and a Decision
  Record (`DagRepository.transition_node` refuses the target outright, and says
  so), so a migration that cancelled every surviving REVIEW node would be RAVEL
  taking an act that is Master's — and with the second half of the fix it no
  longer has to.
- **Where:** `src/ravel/domain/state_machines.py` (`PLANNABLE_NODE_TYPES`,
  `SEATED_NODE_TYPES`, `NODE_EXECUTOR`, `unexecutable_reason`);
  `src/ravel/domain/dag.py` (`DagNode.create`);
  `src/ravel/execution/loop.py` (`Situation.unexecutable`)
- **Seen:** 2026-09-23, live, in two Phase 10 acceptance runs — the certification
  and the research-driven case

### The half that was about the plan, and the half that was about the telling

Abolishing the type closes one door. It does not close the class: `HYPOTHESIS`
and `DECISION` are still plannable, still Master's, and still never handed to an
execution seat — that is by design, because Master's part in the loop is to be
*asked* — so a plan can still hold a node nothing will ever run, and the loop
still puts it to Master. What was wrong was that **nothing told Master why**. A
round whose only question was such a node produced a prompt with "Nothing in the
DAG is waiting on a decision." under "What is waiting on you", and
`read_project_state`'s `stopped` list — the read a session is pointed at when a
node stops — filtered on `WAITING_DECISION` and `BLOCKED` only, so a READY node
nobody could run appeared in neither window. Master was asked a question and
shown nothing; twice, live, it concluded that the prompt was mistaken rather
than the plan. `unexecutable_reason` is now the one sentence both windows
carry — "why no execution seat is ever handed a node of this type", stated for
the type and derived from `NODE_EXECUTOR`, so a type added later is explained
the day it exists — and `tests/acceptance/test_phase10_agents.py::test_l27_a_node_nobody_can_run_is_named_to_master`
asserts that the prompt and the read say the same thing, because a read that
exists and a prompt that paraphrases it are two statements that can disagree.

The evidence that follows is what was true before the change, kept because the
cost of the gap is the reason the fix is shaped the way it is.

`NODE_EXECUTOR` assigns REVIEW nodes to the Review seat, and the comment on
`WORKER_RUN_NODE_TYPES` counts REVIEW among the types "performed by the agent of
their seat, inside its own turn". The loop does not agree. `_executor_for`
answers with a seat for exactly three types — COMPUTATION, EXPERIMENT, RESEARCH —
and `None` for everything else, so a READY REVIEW node lands in
`Situation.unexecutable`: the bucket whose own docstring says these are "not work
and nobody will ever be given them".

For DECISION that is right, and the reason is written down: Master's part in the
loop is to be *asked*, not handed a task. Review was in the same position for
the same reason — its part in the loop is to return PRE_RUN and FINAL verdicts
on other seats' nodes — but nothing said so, and the domain table and the
comment above it said the opposite. The loop does put such a node to Master
(`needs_decision` includes `unexecutable`), so a run does not hang on it; it
costs a Master turn to find out, and a Master that does not work it out rewires
around it.

The reading that settled it was the second one, and it is the one the codebase
had already written down elsewhere: `WORKER_RUN_NODE_TYPES` counts a Research
Agent's work as work a seat does *inside its own turn*, and `PRE_RUN`/`FINAL`
are the Review Agent's turns for exactly the same reason. Review's turn is a
verdict about a node somebody else performed. A REVIEW node would have needed a
verdict about a verdict, which is why making the loop dispatch one was never the
fix: there was nothing for the seat to do when it got there.

What that cost, measured. On 2026-09-23 a live Master built nine REVIEW-typed
nodes across one certification run. Two reached READY and neither was ever
handed to anybody — `REV-B76C0440` became READY at 21:34:56 and was cancelled at
21:37:35, `REV-FE89EA1D` became READY at 21:39:20 and was cancelled at 21:41:02.
All nine ended CANCELLED, and none of the run's fifteen review records names a
REVIEW-typed node as the node being judged — the Review seat was working the
whole time, writing fifteen verdicts on other seats' nodes across ten nodes, and
was never once given work of its own. Master read the wait correctly and wrote it down — "the review
seat has not executed in this environment: two review-typed nodes, one with an
edge and one without, sat READY and undispatched, while the research seat ran" —
and then re-cast that checking work onto RESEARCH nodes. The run made 28
`CREATE_NODE` and 30 `CANCEL_NODE` decisions, took 43 minutes, and ended
TERMINATE_PROJECT.

**A third run shows what the gap costs when Master tries to reason about it.** In
the research-driven case, also on 2026-09-23, another live Master planned two
REVIEW gates, watched them sit, and cancelled both — with reasons that show it
measuring the right thing and concluding the wrong thing from it. Of the first:
"it is cancelled only because its dependency, REV-128E30E1, has now sat READY
through a full cycle without dispatching, so pairing is held hostage to a node
whose dispatchability I cannot establish." Of the second, one cycle later: "That
a RESEARCH node (RES-01C6E229) also sits READY undispatched shows the block is
not specific to review executors, so re-placing this gate elsewhere in the chain
would not make it run." The observation is exact and the inference is not. A
RESEARCH node waiting its turn is work in a loop that dispatches one thing at a
time; a REVIEW node at READY was work that would never be dispatched, however
many cycles pass. Nothing a Master could read distinguished the two, so it
generalised from the case in front of it to the dispatch path as a whole,
cancelled both gates, and re-based their checking onto a node that would run. Fourteen sources
were read, nine of them papers, and the run ended INCONCLUSIVE on research nodes
that had come back PARTIAL — a defensible ending reached by a route that a
one-line note in the situation would have shortened.

It was not a dispatch defect, and it was not only a missing sentence. Both were
true at once: a verdict is Review's turn and a REVIEW node asked for one the
shipped wiring does not implement, *and* nothing said so — not the domain table,
not the tool that let Master create such a node, not the situation Master was
shown. Fixing either alone would have left the other standing, which is why the
resolution did both: the type is gone from the plan, and the node types that
remain unexecutable are now explained where Master reads them.

**The same class, one step over, with a cheaper outcome.** On 2026-09-23 a
different live Master — a later run of the same case — committed a `DECISION`
node for one of its three deliverables, and withdrew it on the very next turn,
writing why: "the framework assigned the node executor_role 'master', and the
Master has no mechanism to produce the file artifacts the node owes
(lab_capability_findings.md, undoped_tio2_measurement_protocol.md), so it could
never satisfy its own required outputs". One turn, not forty-three minutes. The
difference is not that this Master is better at planning; DECISION is the type
whose place in the unexecutable bucket is documented, so the role it was assigned
was legible as soon as the node existed, and Master could see that it had asked
itself for a file it has no way to write. The REVIEW case above stayed invisible
for the whole run because nothing anywhere says a REVIEW node is not dispatched.
The cost of the gap is set by how discoverable the gap is, and only one of the
two has been made discoverable.

**Do not conclude** either that this is why the item failed. It did not fail: the
same run passed. The finding is about what a live Master does when it is handed a
node type that will never move, and the answer is that it works around it at
length rather than stopping.

## L-28 — `make test-integration` runs 583 of the 709 tests in `tests/integration`

- **Since:** 2026-09-19, when `tests/integration/roles/` and
  `tests/integration/research/` were added without the marker
- **Where:** `Makefile` (`test-integration`); `scripts/test_all.sh`; every module
  under `tests/integration/roles/` and `tests/integration/research/`, plus
  `tests/integration/backends/test_slurm_collection.py` and
  `tests/integration/master/test_research_readback.py`

The directory and the target disagree about what an integration test is.
`make test-integration` runs `pytest tests/integration -m integration`, and 126
of the 709 tests collected under that path carry no marker at all — the whole of
`tests/integration/roles/` (83) and `tests/integration/research/` (28) chief
among them, with `test_slurm_collection.py` (9) and `test_research_readback.py`
(6) behind — so the flag deselects them. `scripts/test_all.sh`, which is what
`make test` runs, invokes the same path with no marker and collects all 709, the
e2e-marked cases inside the directory included. Both spellings are written down,
and a report can be filled from either.

What that costs is a number that means two things. Measured through the target
the suite is 583; measured the way the canonical runner measures it, 709, and
neither figure says which command produced it. The 126 are not incidental: they
are where a role's permission surface, the worker and review tool rosters, and
every read-back case of Phase 11's deep read live, and each of them stands up a
real database and a real tool server. Two of the four files are run by name in
release-gate rows (`slurm-integration`, `research-readback`), which is coverage
without a fix: the target still under-collects, and only the gate knows which
files it is reaching.

**Do not conclude** that the tests are at fault. Every case runs and passes in
the canonical runner. What is wrong is that two entry points in one repository
answer "run the integration tests" differently and neither says so — the kind of
gap a report launders into a number without noticing. The Phase 11 sweep hit it
exactly there: the marked count was the one taken, and the item's own new
integration cases were not in the run that was reported as covering them. The
row was re-measured with the canonical command instead, and `TEST_REPORT.md` §1
now names the command its numbers came from. The target itself was left alone:
changing what CI runs is a decision for whoever owns the inventory of tests, not
a side effect of an item about reading a source.

The counts above are as of the Phase 11 sweep (2026-09-23) and both keep moving:
the directory grew by 129 cases across Phase 11 and the deselected set grew with
it, which is the reason the two figures are re-measured at the end of each item
rather than carried forward.

## L-29 — The bench is a person, and RAVEL can neither see nor stop one

- **Since:** Phase 11 (P11-06)
- **Where:** `src/ravel/backends/lab/` (`HumanLabBackend`); `src/ravel/domain/lab.py`;
  `src/ravel/state/repositories/lab.py`; `src/ravel/gateway/routes/lab.py`;
  `docs/06_EXECUTION_AND_REVIEW.md` §5; `tests/acceptance/test_phase11_humanlab.py`

`HumanLabBackend` is a real channel in the sense that a person at a bench is on
the other end of it, and the three things that follow from that are limitations
rather than design choices.

**No bench has ever been on the other end of it.** Every claim about this backend
is a claim about the code: the package is really built, the handover is really a
row, the upload really is an artifact version with a hash and an author, and the
run really does complete on the recorded outputs. What has never happened is a
human being handed one of these packages and doing the experiment. The parts a
real bench would exercise — whether the protocol reads as instructions to a
chemist, whether the reagents named are the ones on the shelf, whether the
measurement plan is physically possible, whether a person uploads the right file
under the right name at the right moment — are exactly the parts no test can
reach, and they are recorded as `BLOCKED_EXTERNAL` on the Phase 11 item rather
than as a certification.

**RAVEL cannot see the bench.** A handover is never `RUNNING`, because a state
meaning "somebody is probably working on it" would be an observation RAVEL never
made. The consequence is that a lab user who does nothing produces no signal at
all: the run sits in `WAITING_EXTERNAL` until its wait expires and ends
`TIMED_OUT`, and nothing distinguishes "working slowly" from "walked away". The
wait has its own clock for that reason, and `--lab-backend human-lab` with nobody
at a bench is a deployment whose experiments wait rather than a deployment that
fails.

**RAVEL cannot stop a person.** Withdrawing a handover (`cancel`) ends the record
so a later delivery cannot complete a run that was given up on, and the record
says out loud that whether the bench stopped is not something RAVEL can see. The
person may work on, and RAVEL will neither know nor claim otherwise. An upload
arriving after a withdrawal is refused because the handover has ended, not
because anybody stopped anything.

**There is no LIMS, no robot, and no instrument integration.** The channel is a
package and a person. It does not schedule equipment, does not reserve reagents,
does not read an instrument, and does not validate that a number a human typed
came from a real measurement. What a lab user uploads is recorded as what they
uploaded and attributed to them; whether it is true is not a question the record
answers, and Review's acceptance is against criteria frozen before the run rather
than against anything the backend could check.

**Do not conclude** that a real laboratory is a configuration change away. It is
a `WorkBackend` implementation *plus* the two things this one deliberately does
not have: a way to observe the work, and a way to make it stop.
## L-30 — The operator's health panel believes a row, and probes nothing

- **Since:** Phase 11 (P11-09)
- **Where:** `src/ravel/gateway/routes/admin.py`; `src/ravel/state/repositories/`
  `services.py`; `src/ravel/state/migrations/versions/20260923_1700_c5a9e2f01b73_`
  `a_process_can_say_it_is_alive.py`; `src/ravel/tui/screens/admin.py`

The administrator's screen answers two questions that look like health checks and
are not.

**`runtime_services` is a process's account of itself.** Whether a long-running
service is running is not derivable from any record — a project a supervisor
stopped driving and a project with nothing left to do are the same rows — so the
panel trusts a report, and the alternative to trusting it is not verifying it but
having no answer at all. What the Gateway owns is the *arithmetic*: the age is
computed here, from a clock this process holds, against the cadence the service
promised in its own report, and the identity columns are guarded so a service
cannot be renamed into another's place. What it does not own is the report's
truth. A process that can write to the database can claim to be the supervisor,
and nothing on this screen would notice; a process that is alive and wedged
between beats reads as dead at the budget. There is no heartbeat the Gateway
sends and no socket it opens to check.

**Reachability is not probed, and the panel says so in as many words.** The
Slurm half of the backend panel reads configuration: which user, which host,
which port, which authentication method, and the *name* of the setting that
holds the secret. It does not connect. A host that is down and a host that was
never configured are both drawn from the same settings, and the only thing that
separates them on this screen is the sentence the route prints underneath. That
is deliberate — a health check that opens an SSH connection from the Gateway
would put the credential in this process and give an operator a way to make the
Gateway a client of the cluster — but a reader who skims the panel will take
`slurm: ravel@cluster:22` for a working cluster.

**Do not conclude** that either panel is useless. The absences are the useful
part: a service that has never reported is drawn as exactly that, which is a
different sentence from having gone quiet, and the two send an operator to
different places. What they cannot do is tell an operator that the machine is
well.


## L-31 — One host, one supervisor, and nothing enforces the second

- **Since:** Phase 11 (P11-10)
- **Where:** `src/ravel/execution/supervisor.py`; `infra/systemd/`
  `ravel-supervisor.service`; `scripts/install_services.sh`

The supervisor is now a service: `Restart=always`, a `SIGTERM` it acts on, and a
row that says whether it is alive. What that buys is a process that comes back
by itself. What it does not buy is *exactly one* of them.

**There is no lease.** A supervisor's guarantee that two loops are not driving
one project is `self._tasks`, a dictionary in its own memory. Two supervisors
against one database — a second unit on another host, a `systemctl start` beside
a `run_v0.sh` already running, an operator who started one by hand — each
discover the same active projects and each give them a loop. Nothing in
PostgreSQL refuses the second. What holds the damage down is the layer below:
`submit` is idempotent in `(project, node, attempt)`, so a duplicated drive
resolves to the same backend job rather than to a second experiment, and the DAG
services are guarded. Two control planes therefore cost wasted model turns and a
contended row, not two of somebody's experiment — and "not two experiments" is
the property P11-10's acceptance case pins at the port. It is still a
duplication of authority that no single row arbitrates, and a deployment that
wants a real one needs a lease or a leader election that V0 does not have.

**systemd will stop restarting a supervisor that cannot start.** The units set
no `StartLimitIntervalSec` or `StartLimitBurst`, so systemd's defaults apply:
five restarts inside ten seconds and the unit goes to `failed` and stays there.
That is the right default — a process that dies before its first tick will not
be fixed by being started a sixth time, and `failed` is a state an operator can
see — but it means the guarantee is "restarts unless it is hopeless", and a
deployment watching only for `active` should also watch for `failed`.

**A restart is not a resume of everything.** What a replacement recovers is what
was written down: the DAG, the contracts, the handovers, the node statuses, the
Temporal histories. A role *turn* in flight is not written down until it
finishes, so a supervisor killed mid-turn loses that turn and the replacement
asks the question again — the same cost L-23 describes for a retry. Nothing
scientific is lost (a turn that had not returned had decided nothing), but the
tokens are spent twice, and a Master several minutes into a long reasoning turn
is the expensive case.

**And the units are per host.** `runtime_services.instance` is `host:pid`, which
is one machine's vocabulary. Nothing in this item addresses a deployment spread
over several, and the container runtime the units want (`After=docker.service`)
is the one §1 of `DEPLOYMENT.md` describes rather than a cluster scheduler.

## L-32 — The whole chain has been run twice, and neither run had both external halves real

- **Since:** Phase 11 (P11-11)
- **Where:** `tests/acceptance/test_phase11_certification.py`; `acceptance/PHASE11_ACCEPTANCE.md` §P11-11

P11-11 is the item that certifies the join rather than the parts: one project, one
run, every leg planned from what the leg before it produced, and an ending that
rests on the row the first leg wrote. Two runs demonstrate it, and they are
strong in opposite directions.

**The scripted run is complete and not real.** Every leg of it is the deployment's
— real PostgreSQL, a real Temporal worker, the real materializers, the real
`HumanLabBackend`, the real tool handlers reached through a seat's own context —
and the two legs software cannot supply are supplied by the case: the
computation runs `MockComputeBackend`, and the person at the bench is the test
process. What it proves is the join. What it cannot prove is anything about
hardware, because nothing in it touched any.

**The live run is real and not complete.** Five real agents, real model turns,
the real Internet, and a person played by the test. What it asserts is the shape
of the run — a terminal ending with Master's Decision Record behind it, every
seat non-silent, all three kinds of work in the plan, a ledger whose sources were
hashed and snapshotted, nothing simulated admitted as evidence — and it does not
assert an ending, because which of A20's four a live Master reaches is a
scientific result. And its computation leg, where a plan has one that runs, is
the mock, because no Slurm host is configured.

**The person is a stub, and the files say so.** In both cases the bench leg is
answered by this process, which now files one body per name the handover's own
`required_outputs` holds — read off the handover, because a live Master writes
its own names and a person filing a fixed pair of them would answer nothing —
and every one of those bodies carries a line saying it is a certification
harness stub and not a measurement. So a reader who opens what the bench leg
produced cannot mistake it for science, and an ending that rests on it rests on
what the record says it rests on.

**And a live plan need not contain a computation at all.** This host registers
two materializers, RASPA's and the bench's, and RASPA's refuses a contract that
names no temperature, pressure, cycles or framework rather than choosing them
itself. A live Master asked for a calculation it cannot express as a RASPA run
therefore re-commits the work in another form — the item's first live run did
exactly that, after refusals it recorded the reasons for — which leaves the live
run with nothing for the mock to have marked. The marking is therefore certified
on the scripted run, where the mock really did the work, and the live run
certifies the other half of the same guarantee: that nothing simulated is ever
cited.

**The run that passed then certified the marking too, from the other side.** Its
plan did contain computations, they reached `MockComputeBackend`, and every one
of them was reviewed at FINAL and failed *on the marking* — "Mock/simulated/
inadmissible flag present on both artifacts — explicit failure condition met"
— under a criterion the Master itself had written into the contract. The bench
leg was refused in the same terms, on the stub note the person's files carry.
So on that run both halves of the guarantee were exercised by seats that had not
been told about it: the marking where the mock ran, and no simulated row in any
claim's `source_refs`. What the marking does *not* have is a run where a real
cluster and a real bench were both present (above), and no simulated artifact
has ever been offered to a reviewer that accepted it.

**So neither run has a real cluster and a real bench at the same time.** Each of
the two is certified on its own — P11-05's backend against a scripted cluster and
its live case awaiting a host, P11-06's channel against a test playing the lab
user and its live case awaiting a bench — and what is *not* certified is the pair
inside one project: that a criterion derived from a real paper's claim is the one
a real cluster computes and a real person measures against. The specific external
requirements are named on the Phase 11 acceptance rows: `RAVEL_SLURM_HOST`,
`RAVEL_SLURM_USERNAME` and a credential for the first, and a human being for the
second.

**Live runs vary, and the case is written for that.** A live Master builds a
different plan each time it is asked, so a case that fixed the ending would be
grading the model's science rather than RAVEL's chain, and one that fixed the
plan would fail on a correct decision. What the case fixes is what the
architecture promises: the shape, the record, and the refusals.

**Do not conclude** that the item is done, and do not read its live half as a
scientific result. It is `PARTIALLY_CERTIFIED`: both blockers are stated, the
software chain is covered, the two legs that are not real are marked as not real
everywhere they appear in the record, and the live run's bench answer is a file
whose first line says it is a harness stub. What the live half certifies is the
shape — that five real seats plan, read, hand over, are delivered to, review and
end — and not that anything measured was measured.

## L-33 — Master's ending report is prose, and the ledger is the record

- **Since:** Phase 11 (P11-11)
- **Where:** the ending `DecisionRecord` of a live run; `acceptance/PHASE11_ACCEPTANCE.md` §P11-11

A live project ends with a long report from the Master: what each line
delivered, what stopped the ones that did not, what it considered and refused,
and what would finish the job. It is the document a person reads, and on the
run P11-11 certified it was careful in the ways this file cares about — it
quoted the record for every stop, distinguished a blocked line from a failed
one, and refused the shortcut that would have produced numbers ("supplying
RASPA's adsorption parameters would have run a gas-uptake simulation and named
it conductivity").

It is also generated text about a run, and it can disagree with the run. That
same report says the computation line "never produced output", and the database
holds computations that executed on `MockComputeBackend` to completion, with
artifacts written and Review verdicts on them. Read at the level of
admissibility the Master's sentence is true — nothing admissible was produced —
and it is the reading a scientist would want; but a reader who had only the
report would not know a mock ran at all, and could not tell a line that was
never attempted from one whose every attempt was thrown out.

- **What is authoritative:** PostgreSQL. The DAG, the Execution Records, the
  Review verdicts and the claims are rows, and both cases of P11-11 assert on
  those rather than on the prose.
- **What this costs:** the ending report is the one artifact a person is likely
  to read first, and its precision about the run's own history is the model's,
  not RAVEL's. Nothing in V0 renders the ledger's account of a run into that
  report, and nothing checks the report against the ledger.
- **Where it does not bite:** every material decision the report rests on is
  also a row — the ending decision's type, the refusals, the evidence — so a
  reader who checks any of its claims against the database can settle them. The
  failure is one of accounting, not of record.

## L-34 — An item's own suites cannot see a case in a phase behind it

- **Since:** Phase 11, found when the first full release gate ran after P11-10
  (`443fb37`) and recorded when P11-11's sweep ran it again
- **Where:** `scripts/run_v0.sh`, `tests/acceptance/test_phase10_ops.py`,
  `scripts/release_gate.py`

P11-10 moved the Gateway's start out of `run_v0.sh` and into
`scripts/run_gateway.py`, the entry point the systemd unit starts, so that a
launcher and a unit cannot disagree about how the Gateway comes up. P10-20's
acceptance case asserts that the one command starts the Gateway, and it asserted
it as the string `ravel.gateway.app:create_app` *in the shell script*. The case
is marked `phase10`: it is in neither `make phase11-acceptance` nor any suite
P11-10 ran, so every number that item reported was green while a Phase 10
acceptance case had been failing since the commit.

**What that costs is a phase that looks finished while the evidence behind it is
red.** An item's verification is its own suites — that is what "one item at a
time" means — and an item that changes something every phase shares is exactly
where the boundary bites. Nothing in the repository runs the older phases' cases
when a shared script changes.

- **What found it:** the release gate's row per phase. `make release-gate` runs
  `tests/acceptance -m phase10` (and `-m acceptance`, and `-m phase11`) and is
  the only thing in this repository that reads across phases; it is what turned
  the item green into a phase red, one item later. The case was fixed to assert
  the claim rather than the spelling — the script starts the entry point, the
  entry point's own `uvicorn.run(...)` serves `ravel.gateway.app:create_app` as a
  factory, and the unit starts the same entry point — and the finding is recorded
  in `TEST_REPORT.md` §7.11 and `acceptance/PHASE11_ACCEPTANCE.md` §P11-10.
- **Where it does not bite:** the phases' own runners are unchanged. A01–A20 are
  still run by `make acceptance` and the P10 items by `make phase10-acceptance`;
  what is limited is *when* they run, not whether they exist.
- **Do not conclude** that the fix is that every item should run the gate. The
  gate is every phase's suites in one command, it takes about two hours, and its
  live rows spend real model turns; running it per item is running it constantly
  and reading it never. The honest statement is that an item's green means its
  own suites, and that a change to a shared script, a shared model, or a shared
  route is the case where somebody should run the gate before calling the item
  done — a judgement, not a mechanism.
