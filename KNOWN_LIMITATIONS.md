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
and `MockLabBackend` do. A future Slurm or LIMS adapter must.

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

**Do not conclude** that deactivation was forgotten. Revocation in V0 is by
authority envelope, which is Master's and the User's lever; a user-level switch
is a later concern.

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

---

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

---

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
