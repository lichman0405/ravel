# Test Report

What was actually run, what it reported, and what it does not cover. Every
number below comes from a run on this machine, and every command is one a reader
can repeat.

The distinction this file is written to preserve is between *an item that
passed* and *an item that did not run*. A suite that reports 43 passed and a
suite that reports 41 passed and 2 skipped are different claims about the
system, and only one of them is true here.

---

## 1. How to reproduce all of it

On Ubuntu 24.04 with the infrastructure up (`scripts/dev_up.sh`) and the
credentials in `.env` filled in:

```bash
make migrate       # tests/dsh reads the database `.env` names; every other suite
                   # builds its own schema on `ravel_test` with create_all
make lint          # ruff check src tests, then pyright src tests
make test-unit     # tests/unit, no services needed
pytest tests/integration   # all 586 cases. `make test-integration` runs the same
                           # path with `-m integration`, which deselects the 108
                           # tests in it that carry no marker — see §2 and L-28
make test-e2e
make test-live     # real-Internet research; needs RAVEL_RESEARCH_CONTACT_EMAIL
make test-dsh      # real model turns; needs DEEPSEEK_API_KEY
make acceptance    # A01-A20 plus the seven gates, printed as a matrix
make phase10-acceptance  # P10-01..P10-20 and the twenty worker items — 57 cases,
                         # two of them live runs of a whole project, ~26 minutes
make phase11-acceptance  # the Phase 11 items, printed as a matrix
```

Three warnings, each of which costs time when ignored:

- **Migrate first.** `tests/dsh` runs against the database `.env` names — the
  deployment's — and nothing in a test run migrates it, so it now refuses to
  start against one that is behind the models. Every other suite, the live cases
  in `tests/acceptance` and all of `tests/live_research` included, builds its
  schema against `ravel_test` with `create_all`, so a model that added a table is
  there without anybody running anything.
- **Never run two pytest processes at once on this machine.** They share the
  `ravel_test` database and both `TRUNCATE` on entry, so the second one's
  cleanup blocks on the first one's locks, and the failure surfaces as a
  timeout in a test that did nothing wrong.
- `make test` (`scripts/test_all.sh`) **fails without `DEEPSEEK_API_KEY`** by
  design: it sets `RAVEL_REQUIRE_DSH=1`, because a harness gate that can pass by
  not running is not a gate.

## 2. Results

Run on 2026-09-23 with `DEEPSEEK_API_KEY` and `RAVEL_RESEARCH_CONTACT_EMAIL`
set in `.env`, after `make migrate`. Every suite ran once, in sequence, except
the integration row — the first pass at it selected a subset of the directory
rather than all of it, and the paragraph under the table says which command
produced which number:

| Suite | Passed | Skipped | Failed | Exit |
|---|---:|---:|---:|---|
| `tests/unit` | 879 | 0 | 0 | 0 |
| `tests/integration` (all 580, see below) | 580 | 0 | 0 | 0 |
| `tests/dsh` | 11 | 0 | 0 | 0 |
| `tests/e2e` | 20 | 0 | 0 | 0 |
| `tests/live_research` | 15 | 0 | 1 | 1 |
| `tests/acceptance` (`make acceptance`) | 43 | 0 | 0 | 0 |
| `tests/acceptance -m phase10` (`make phase10-acceptance`) | 57 | 0 | 0 | 0 |
| `tests/acceptance -m phase11` (`make phase11-acceptance`) | 15 | 0 | 0 | 0 |

**The integration row is the count that runs the whole directory, and the first
attempt at it was not.** `scripts/test_all.sh` — what `make test` runs — invokes
`pytest tests/integration` with no marker; `make test-integration` invokes the
same path with `-m integration`, and 108 of the 580 cases collected there carry
no marker at all, so the flag deselects them: the whole of
`tests/integration/roles/` and `tests/integration/research/`, which is where a
role's permission surface, the worker and review tool rosters, and this item's
own seventeen deep-read integration cases live. This sweep's first pass used the
target's spelling and reported 472 — a number that looks like a result and is a
selection. The row above is the canonical run, re-measured: 580 passed, 0
deselected, 4:25. `KNOWN_LIMITATIONS.md` L-28 records the disagreement between
the two entry points and why neither was changed here.

**The one failure is OpenAlex refusing to answer, and it is left in the table
rather than re-run away.** `tests/live_research/test_live_sources.py::test_the_other_connectors_reach_their_real_services`
asks each of OpenAlex, arXiv and PubChem for a real record. OpenAlex answered
HTTP 429, RAVEL recorded it as this connector being *unavailable* rather than as
a search that found nothing — `unavailable=('openalex: ... answered HTTP 429',)`
— and the assertion, which is on leads, failed. The headers say what the 429 is:
`retry-after: 4588`, `x-ratelimit-onetime-remaining: 0`, `x-ratelimit-remaining: 8`
of `x-ratelimit-limit: 1000`. This host has spent OpenAlex's allowance for the
day: the same case was re-run minutes later and failed the same way in 0.72s, so
it is a quota and not a timeout. That case is one test rather than three — it
asks OpenAlex first and asserts on the three services' leads together — so arXiv
and PubChem were not exercised behind it; the other 15 live cases, which reach
Crossref, arXiv, PubChem and the real Internet, passed. The previous edition of
this table was measured on a day when the
allowance was not spent; nothing in RAVEL changed to make OpenAlex stop
answering, and no test was changed to accommodate it. A gate that passes by
tolerating a 429 is a gate that no longer checks whether the connector works.

The `tests/dsh` flake recorded in the previous edition of this table did not
recur: 11 passed, 0 failed, and the model answered in every case.

`make acceptance` reports by *item* rather than by test, which is a different
question from the one a pytest summary answers — see §3. The same is true of
`make phase10-acceptance`, whose 57 cases are the 37 behind the twenty `P10-`
items plus the twenty worker items — see §3.1 — and of `make phase11-acceptance`,
whose rows are the cases behind the `P11-` items — see §7.

**These rows were not all measured at the same moment, and the last two are the
oldest.** The integration row was re-measured after `L-27` landed — the paragraph
above says why — and the unit row carries the pass from before it. Measured
together on the tree that ends Phase 11's third item, `tests/unit` is 882 and
`tests/integration` is 586, the second including §7.4's six readback cases;
`tests/acceptance -m phase11` was 15 when this sweep ran and is 19 as of §7.4,
where every remeasured number is given with the command that produced it.

**One suite reads the deployment database; every other one reads `ravel_test`.**
`tests/dsh` hands a tool server the settings `.env` names, so it needs that
database migrated and now refuses to start against one that is behind the
models. The live turns inside `tests/acceptance` do not: they are built on
`integration_settings`, and the environment the certification's seats were
launched with says so — `RAVEL_POSTGRES_DB=ravel_test`, quoted in
`KNOWN_LIMITATIONS.md` L-26, and the projects those runs left behind are rows in
that database, which is where §6's evidence was read from. Run `make migrate`
before the sweep in any case; §6 records what a stale deployment database looked
like before `tests/dsh` had a guard.

## 3. The acceptance matrix

`acceptance/V0_ACCEPTANCE.md` opens with "V0 is not complete until all 20 items
pass", and `make acceptance` is the command that answers that sentence rather
than a proxy for it. It runs the suite, then reads the JUnit XML and prints one
row per item, deriving each row from the *test names* — every case is called
`test_a14_...` or `test_gate_3_...`, so the mapping from item to test is the
name and there is no second table to keep in step.

An item with **no test at all is printed as `MISSING` and fails the run.** That
is the specific failure this script exists to make visible, because an
acceptance item nobody exercises looks exactly like a passing one from every
other angle.

```
── RAVEL V0 acceptance ──

  PASS    A01  Project creation              1 passed
  PASS    A02  Master start                  1 passed
  PASS    A03  Real research                 1 passed
  PASS    A04  Evidence provenance           1 passed
  PASS    A05  Initial Scientific DAG        1 passed
  PASS    A06  Compute success               1 passed
  PASS    A07  Compute failure               2 passed
  PASS    A08  Review outcomes               3 passed
  PASS    A09  Master replanning             1 passed
  PASS    A10  Long lab wait                 1 passed
  PASS    A11  Lab deviation                 1 passed
  PASS    A12  Contract response             2 passed
  PASS    A13  Incomplete delivery           1 passed
  PASS    A14  Acceptance freeze             2 passed
  PASS    A15  Master session kill/recovery  1 passed
  PASS    A16  Runtime/Temporal restart      1 passed
  PASS    A17  Parallel branch + join        2 passed
  PASS    A18  TUI interaction               3 passed
  PASS    A19  DAG authorization             6 passed
  PASS    A20  Project outcome               3 passed

── extra gates ──

  PASS    gate 1  live Research reaches real original sources  1 passed
  PASS    gate 2  fabricated DOI/source is refused  1 passed
  PASS    gate 3  mock compute/lab data is marked simulated  2 passed
  PASS    gate 4  DSH is pinned                 1 passed
  PASS    gate 5  all 5 roles use the intended preset and tool scope  1 passed
  PASS    gate 6  no direct public DSH endpoint  1 passed
  PASS    gate 7  Postgres remains authoritative after restarts  1 passed

27/27 demonstrated, 0 failed, 0 skipped, 0 missing (pytest exit 0)
```

### 3.1 The Phase 10 matrix

`make phase10-acceptance` runs the same script with `--phase phase10`, and prints
a second table beside the first: the twenty `P10-` items, then the twenty
`test_p10_wNN_...` worker items, each derived from the test name the same way.
The worker items are printed separately rather than folded into `P10-04` / `P10-05`
because the Compute and Experimental Worker items are architectural — *the seat
exists* — and the worker items are the twenty properties of that seat that
somebody would otherwise have to take on faith.

```
── RAVEL Phase 10 acceptance ──

  PASS    P10-01  latest DSH re-evaluated                     1 passed
  PASS    P10-02  single-host decision evidence-backed        1 passed
  PASS    P10-03  five real DSH agent roles exist             7 passed
  PASS    P10-04  Compute Worker live                         1 passed
  PASS    P10-05  Experimental Worker live                    1 passed
  PASS    P10-06  Compute Backend remains Mock                1 passed
  PASS    P10-07  Lab Backend remains Mock                    1 passed
  PASS    P10-08  no worker bypass                            1 passed
  PASS    P10-09  worker contract enforcement                 1 passed
  PASS    P10-10  worker death/recovery                       1 passed
  PASS    P10-11  Master death/recovery                       1 passed
  PASS    P10-12  Project Supervisor autonomous               6 passed
  PASS    P10-13  no manual run_project requirement           2 passed
  PASS    P10-14  long external wait/resume                   1 passed
  PASS    P10-15  TUI disconnect does not stop project        1 passed
  PASS    P10-16  multi-project isolation                     3 passed
  PASS    P10-17  live five-agent autonomous certification    2 passed
  PASS    P10-18  real Research provenance                    1 passed
  PASS    P10-19  original A01-A20 remain green               1 passed
  PASS    P10-20  one-command server startup                  3 passed

── worker-level items ──

  PASS    P10-W01  The supervisor's Worker seats are real DSH sessions, not scripts  1 passed
  PASS    P10-W02  The Experimental Worker seat is a real DSH session               1 passed
  PASS    P10-W03  Both Workers are task-scoped identities: the session is the task's 1 passed
  PASS    P10-W04  A Worker is told about one task, not about the plan              1 passed
  PASS    P10-W05  A Worker reaches only its own contract, and holds five tools between the two seats  1 passed
  PASS    P10-W06  A Worker cannot mutate the Scientific DAG                        1 passed
  PASS    P10-W07  A Worker cannot alter Acceptance Criteria                        1 passed
  PASS    P10-W08  The Compute Worker's turn reaches the MockComputeBackend execution path  1 passed
  PASS    P10-W09  The Experimental Worker's turn reaches the MockLabBackend execution path  1 passed
  PASS    P10-W10  Every artifact a mock produced is marked simulated               1 passed
  PASS    P10-W11  A simulated artifact cannot become Evidence                      1 passed
  PASS    P10-W12  A retry the contract permits is carried out                      1 passed
  PASS    P10-W13  An out-of-contract request is refused at the code layer           1 passed
  PASS    P10-W14  An unauthorized substitution is refused                          1 passed
  PASS    P10-W15  An experimental deviation escalates to Master rather than being answered by the Worker  1 passed
  PASS    P10-W16  A Worker waiting on something external needs no live turn and no process  1 passed
  PASS    P10-W17  A Worker's identity continues after a long wait ends              1 passed
  PASS    P10-W18  A dead Worker session is rebuilt from authoritative execution state 1 passed
  PASS    P10-W19  Two projects' Workers cannot reach each other's context, contract, or artifacts  1 passed
  PASS    P10-W20  No run bypasses the Worker that owns it                           1 passed

40/40 demonstrated, 0 failed, 0 skipped, 0 missing (pytest exit 0)
```

One row is expensive. `P10-17` is two live, unattended runs of a whole project
against real model turns — one driven by a work order, one by an open question —
and the pair took 22:25 in this matrix and a real bill. They are the only items
here that cannot be answered by reading code, and the only ones whose *coverage*
is a live model's judgement rather than a property of the suite. The two cases
were also run separately the same day, at 10:45 and 16:32, and both passed; a
re-run earlier that day failed in 13:52 with the experimental seat never
reached, for a reason the Decision Record states in Master's own words — the
objective had left the bench's readiness to be discovered, and Master checked it
instead of assuming it. §6 has the whole of that. The endings the run reached
across those runs are `INCONCLUSIVE` and `FAILED`, with `executed_by` naming
`compute-worker` and `experimental-worker` and the backends `mock-compute` and
`mock-lab` behind them. What a live run *cannot* end as, and why, is stated in
`acceptance/PHASE10_ACCEPTANCE.md` under P10-17 and in `KNOWN_LIMITATIONS.md`
L-19 — a COMPLETED ending would require a live Review to accept a `simulated`
artifact as a real measurement, and it does not.

## 4. What used to be skipped, and now runs

With both credentials present, the two previously skipped acceptance items and
their underlying suites now execute against real external services:

- **A03 / A04 and `tests/live_research`** now reach the real Internet. The 16
  cases exercise Crossref, OpenAlex, arXiv, PubChem, and direct URL retrieval —
  including three that read a real paper's body text out of the stored snapshot
  (§7.2). Fifteen passed on 2026-09-23 and the sixteenth is the OpenAlex quota in
  §2. The connectivity probe at the start of `scripts/test_live_research.sh`
  reports Crossref and OpenAlex as reachable; arXiv answers the probe with HTTP
  400 but the actual `arxiv.org/abs/1606.00335` fetch in the tests returns 200 —
  the probe URL (`export.arxiv.org/api/query?max_results=1`) appears to be
  stricter than the path the product uses.
- **`tests/dsh`** now makes real DeepSeek model turns. The 11 cases verify that
  the pinned runtime starts, runs RAVEL tools, refuses cross-role tool access,
  and recovers Master state from PostgreSQL rather than from harness session
  context.

## 5. The seven claims the development contract forbids declaring without evidence

`docs/15_AUTONOMOUS_DEVELOPMENT_CONTRACT.md` lists what a "no false completion"
claim must not rest on. Each is answered by a test rather than by an assurance:

| Claim | What demonstrates it |
|---|---|
| Web research is not mocked | gate 1 asserts every connector is pointed at a real service and that research cannot be answered offline; gate 2 asserts a source nobody read cannot enter the ledger, with no network call involved. **A03, A04 and 15 of the 16 cases of `tests/live_research` passed against real services on 2026-09-23; the sixteenth is OpenAlex answering HTTP 429 to a spent daily quota (§2), which the gateway reports as an unavailable connector rather than as a search that found nothing.** |
| DSH is pinned and tested | gate 4 — the installed distributions match the pin, the bundled runtime self-reports `0.1.5-rc.1`, and the vendored checkout's `HEAD` is the pinned commit |
| DSH role presets and tool scope are verified | gate 5 — every role boots with its own contract and no other role's roster; `tests/integration/roles` holds the permission matrix for all five |
| Master recovery is tested | A15 — the project survives the Master session and is recovered from it |
| Temporal restart and wait recovery are tested | A16 — a waiting task survives the worker being killed; A10 — a waiting lab task is woken by its signal |
| Acceptance freeze is tested | A14, both cases: a frozen criterion cannot be edited in place, and new criteria are a new version naming the decision that authorized them. A08 records a verdict against the criteria as frozen |
| Unauthorized DAG mutation is tested | A19, all three cases: no non-Master role's server registers a DAG-mutating tool, the mutation service refuses a non-Master actor, and no HTTP route lets a member change the DAG |
| The TUI is not static | `tests/e2e/test_tui.py` drives the real console headlessly against a real uvicorn over real HTTP and a real WebSocket; A18 covers the three member-facing surfaces |

## 6. What the tests found

The suite is load-bearing, and the record of what it caught is the evidence for
that. Every finding below is kept, including the ones that were found only once
a live gate could run and the one that was found by a live gate failing.

**A hang that named the wrong test.** The first full acceptance run failed at
A15 with `Timeout (>600.0s) from pytest-timeout`. A15 was not the problem.
`pg_stat_activity` showed a session `idle in transaction`, holding a read lock on
`acceptance_contracts`, with the next test's `TRUNCATE` blocked behind it: an
earlier test had used a repository *after* its `with ... read_only()` block had
closed, which checks out a connection nothing ever returns. The lock is held
until the process ends, so the failure lands on whichever test runs next — which
is why every isolated run of A15 passed.

Fixed in two layers. The specific leak now reads inside its block. And the
`clean` fixture sets `SET LOCAL lock_timeout = '10s'` and raises a sentence
naming the usual cause, so any future instance is a ten-second, self-explaining
failure instead of a ten-minute mystery attributed to an innocent test.

The guard was verified by holding a lock deliberately and observing the refusal:
`refused after 10.0s: canceling statement due to lock timeout`.

**A retried start crashed instead of being a no-op.** Running every suite in one
sweep turned up a failure in
`tests/e2e/test_headless_loop.py::test_a_project_reaches_a_terminal_outcome_with_no_human_input`:
`WorkflowAlreadyStartedError`, on a node whose run was already under way.

The cause was a guard written against the wrong exception type.
`NodeRunClient.start_node_run` documents that a repeat start raises
`RunAlreadyStarted`, and it caught `RPCError` with status `ALREADY_EXISTS` to
produce it. But the SDK does not raise `RPCError` for that case: `temporalio`
converts it into `WorkflowAlreadyStartedError`, which derives from
`TemporalError` and **not** from `RPCError`. The `except` clause therefore never
fired for the one condition it existed for, and the exception escaped to the
caller. `TemporalNodeRuns.start` catches `RunAlreadyStarted` precisely so that a
second start of a running node is logged and ignored — the loop's restart
tolerance was resting on a branch that could not be reached.

Fixed by catching `TemporalError` and recognising both shapes, and pinned by
`tests/integration/temporal/test_node_run.py::test_starting_a_node_that_already_has_a_run_is_a_fact_and_not_a_crash`.
That test was checked against the old code before being kept: with `except
RPCError` restored, it fails with the same `WorkflowAlreadyStartedError` seen in
the sweep — so it tests the fix rather than merely coexisting with it.

**The DSH gate could not run until the project existed.** When the harness gate
finally ran with a real API key, two live DSH tests failed because
`read_project_state` reported `no project '<id>'`. The `project_id` fixture had
always generated a deterministic slug, but it never created the matching
PostgreSQL row. The model did exactly the right thing: it read the tool result,
refused to guess, and reported that the project was missing. The test expected
the model to echo a title that had never been written.

Fixed by making the fixture create the project row before starting the harness
session, and by letting `ProjectRegistry.create` accept an optional caller-supplied
`project_id` for deterministic test fixtures.

**A timestamp serialized two ways.** Once live research ran, one test failed on
`source["retrieved_at"] == opened["retrieved_at"]`. The `open_source` tool
serialized the moment with `datetime.isoformat()` (`...+00:00`), while the
registered `EvidenceSource` row was serialized through Pydantic's JSON mode
(`...Z`). Both strings name the same UTC instant, but they are not equal.

Fixed by adding `json_iso()` to `ravel.domain.clock` and using it wherever a
timestamp is hand-serialized to match Pydantic's `model_dump(mode="json")`
output.

**A node that stopped reported the wrong reason.** P11-01's acceptance case
`test_p11_01_a_lost_run_leaves_a_question_and_no_scientific_verdict` failed on
its first run, and the fault was in the product rather than in the test.
`read_project_state` reports why each stopped node is stopped, under `stopped`,
and the reason it reported was the node's latest Review. For a node a Worker's
contract refused, or one a Review would not clear, that is correct — the verdict
*is* what stopped it. But a node that ran and was then stranded by something else
still has its pre-flight clearance as its latest review, and that clearance is a
PASS: a document saying the node was allowed to go, handed to Master as the
answer to why it did not.

The fix is one condition — a `PASS` is not a reason anything stopped, so it is
reported as no verdict rather than as one. It would have been easy to close this
by weakening the test, which is why the finding is recorded here: the assertion
was right and the read was wrong, and the reason it was reachable at all is that
until Phase 11 no node could reach `WAITING_DECISION` without a verdict that put
it there.

**The reconciler asked about runs that had never existed.** The live five-agent
certification — `test_p10_17`, the one item no amount of reading can answer —
failed on the first full run with Phase 11 in it. The project reached
`CANCELLED` with three of the five seats never having taken a turn, and the log
said why:

```
reconciled a lost run: node=RES-287B4188 version=1
  workflow=node-run:9ea8e5eef6d0428799669fadad3183e6:v1
  observed=NOT_FOUND class=WORKFLOW_LOST node now=WAITING_DECISION
```

Three **research** nodes, parked in the middle of their searches, each with a
reconciliation saying its run had been lost. No run existed to lose. A Research
Agent does that work inside its own turn: it never calls `start_execution`, no
workflow is started for it, and no activity of a run ever moves the node. The
sweep asked Temporal about a workflow id nothing had ever started, and Temporal
correctly answered that it was not there. The Master then saw three of its
evidence-gathering tasks waiting on it, could not get the evidence, and the
project ended without a Review or a Worker ever running.

The defect was in the reasoning, and the test that should have caught it had the
belief written into it. `LIVE_NODE_STATUSES` was the whole filter: any node in
`RUNNING` or `WAITING_EXTERNAL` was asked about, and
`test_recovery_does_not_depend_on_which_node_type_it_is` asserted that a RESEARCH
node *was* recovered, on the argument that the reconciler reads the durable layer
and not what kind of work the node asked for. The argument is sound and the
conclusion is wrong — which nodes have a durable run is precisely what the
durable layer knows, and a node type performed by an agent has none.

The filter is now `WORKER_RUN_NODE_TYPES`: COMPUTATION and EXPERIMENT, the two
node types whose execution *is* a run, stated once in `ravel.domain.state_machines`
and pinned by a unit test to the seats `NODE_EXECUTOR` assigns them, so a node
type added later cannot quietly join one set and not the other. The integration
case that had it backwards now asserts the opposite, and asserts it twice —
nothing was written, and the probe was never asked. The second assertion is the
one that would have caught this before the live run: the reconciler had no
business forming the question.

**A table without the migration that installs it.** Phase 11 adds
`run_reconciliations`, and `tests/dsh` reads the *deployment* database rather
than `ravel_test`: it hands a tool server the settings `.env` names, and nothing
in a test run migrates that database. Every other suite builds its schema with
`create_all` and could not have caught it. The symptom was as misleading as it gets — `read_project_state`
raised inside the tool server, the agent reported `Error executing tool
read_project_state` and then answered, correctly and at length, that it could not
read the authoritative state and would not guess. The case failed on a sentence
about the model, and the model had done nothing wrong.

`make migrate` was the fix, and `tests/dsh/conftest.py` now refuses to start
against a database older than the models, naming the missing tables and the
command. Verified by pointing it at a scratch database pinned to the previous
revision, where it reports exactly `run_reconciliations` — and at the migrated
one, where it is silent.

**A search that could not find a phrase the reader had just read.** The live
deep-read case read page one of a real paper, took forty characters off its
longest printed line, and searched the paper for them — and the search came
back empty on the first run. The phrase had been assembled by joining the words
of a *different* line, so the string it searched for contained a space where the
document has a newline. A literal search is right, and it is what makes the
offset a search reports an offset a read can use: a search that folded
whitespace would have to report positions in a text the reader never sees. What
the run found was that the boundary was undocumented, so a model would meet it
the way the test did. `search_source` now says to search a few words rather than
a sentence, and `tests/unit/test_deepread.py` pins the behaviour with a phrase
that spans a line break and the shorter one that finds the same passage.

**Five seats was a property of the plan, not of the code.** P10-17's
certification asserts that all five seats take a live turn, and it failed a run:
three seats, `INCONCLUSIVE`, after a live Master planned the literature first,
read two research records reporting that the protocols could not be fixed from
what was retrievable, and concluded in writing that the evidence did not settle
the question. Every turn in that run was defensible and the ending was correct
science; the assertion was about coverage, and no coverage had been promised.
Phase 11's deep read was still the obvious suspect, because the seat that stopped
was the one whose reading tools it had just changed. Testing that meant two runs
of one test with a single difference between them. Both arms drove the same
objective out of the same case: the two trees collected the same 56 Phase 10
acceptance tests at the moment each run started, against the same mock backends
and the same `ravel_test` — one after the other, the first against `5f01ff9`'s
source and the second against `HEAD`'s:

| | `5f01ff9` (no deep read) | `HEAD` (deep read) |
|---|---|---|
| Ending | `INCONCLUSIVE` | `CANCELLED` (`TERMINATE_PROJECT`) |
| Nodes in the final plan | 4 | 38 |
| Node decisions | 5 (4 create, 1 conclude) | 59 (28 create, 30 cancel, 1 terminate) |
| Execution Records | 2 | 3 |
| Seats that took a turn | 5 (master, research, review, compute, experimental) | 5 (master 206 turns, research 70, review 26, compute 6, experimental 3) |
| Result | **passed**, 15:53 | **passed**, 42:58 |

The earlier failure did not reproduce — and the two arms, whose source trees
differ by three files that no seat but Research can even see, disagree about
every row of that table. That is the finding: what varies between runs of this
test is the plan a live Master builds. The code cannot be the explanation, and
that was checked rather than assumed — P11-02's whole footprint under `src/` is three
files and one new module (`mcp/registry.py`, `mcp/tools/research.py`,
`research/fetching.py`, `research/deepread.py`), the registry change adds three
tools and alters no existing entry, all three are `frozenset({RESEARCH})` and
therefore invisible to Master's tool list, and no file under `domain/`, `state/`,
`execution/` or `master/` is touched at all. Role permissions, DAG semantics, and
execution/reconciliation behaviour are unchanged by construction. The one path
P11-02 shares with older code was moved rather than changed: `excerpt_of`, which
`search_web` and `open_source` have always rendered their excerpts with, now
calls the same tag-stripping `html_text` performs instead of carrying its own copy
of it, and neither search results nor excerpts gained a byte. What P11-02
does change is what the Research seat can *read*, and that is visible in the two
ledgers: the `5f01ff9` arm's own record says of the one PDF it retrieved that it
"is freely retrievable (HTTP 200, PDF, ~8.39 MB, sha256 78be2ec0…), but RAVEL's
gateway returned no extractable text from it", while the `HEAD` arm's ledger
holds four PDFs at `access_status = OK`, and neither ledger shows a seat holding
a tool it did not hold before.

So the item was asserting something no run owes. Coverage is a property of the
plan, and the plan belongs to a live model — which its own reasoning already
said: "a plan that answers half the objective is a worse plan, not a failed
test". The objective both arms ran has since been rewritten for the same reason,
and what it shows is worth reading twice: under a question — "establish whether
niobium doping raises the conductivity of TiO2 by at least 15% ... I will put the
two together myself" — `5f01ff9` answered it the expensive way, by actually
building both benches and letting the mock artifacts fail Review, while `HEAD`
spent its whole run on review gates that could never be dispatched and
terminated. Both are defensible plans, and neither is a property of the source
tree. The two are now separate cases. The certification asks for three pieces of
work, one per work seat, with each bench's inputs written into the objective
(the composition grid, the reference value, the specimen and conditions) so that
no bench waits on what the literature did or did not yield, and asserts all five
seats. The second case asks the screening *question*, asserts that the ending is
the Decision Record naming it and that each node carried out was carried out by
the seat that owns its type, and lets the ending be whichever of A20's four the
evidence supports. Neither mocks Research, loosens a Review, hides an evidence
gap from Master, or asks any seat to behave unscientifically for a row.

**A node type that is planned, promoted, and never handed to anybody.** The
`HEAD` arm above also showed what a live Master does when it is handed one. It
built nine REVIEW-typed nodes; two reached READY and neither was ever dispatched.
All nine ended `CANCELLED`, and none of the run's fifteen review records names a
REVIEW-typed node as the node being judged — the seat was working the whole
time, writing those fifteen verdicts on ten of other seats' nodes, and was never
given work of its own. `NODE_EXECUTOR` assigns REVIEW to the Review seat and the
comment on `WORKER_RUN_NODE_TYPES` counts it among the types "performed by the
agent of their seat, inside its own turn", but `ProjectLoop._executor_for`
answers with a seat for exactly three types — COMPUTATION, EXPERIMENT, RESEARCH —
so a REVIEW node lands in `Situation.unexecutable`, the bucket the loop
documents as "not work and nobody will ever be given them". Master read the wait
correctly and wrote it down — "the review seat has not executed in this
environment: two review-typed nodes, one with an edge and one without, sat READY
and undispatched, while the research seat ran" — then re-cast the checking work
onto RESEARCH nodes and rewired around the rest. The run made 28 create and 30
cancel decisions, took 43 minutes, and ended `TERMINATE_PROJECT`. What is wrong
is not the bucket — a verdict is Review's turn, and a REVIEW node may be a way of
asking for one the wiring does not implement — but that nothing says so: not the
domain table, not the tool that lets Master create the node, not the situation
Master is shown, and not a test, since the four cases covering that bucket all
use DECISION, the one type whose presence there is documented. `L-27` records it,
including the third run that saw it — the research-driven case that day, where
another Master cancelled two REVIEW gates and wrote down an inference nobody
could have corrected from what it was shown: that the block "is not specific to
review executors", because a RESEARCH node was sitting READY too.

**The same gap one type over, and what it cost when it was legible.** A later
run of the certification case — the redesigned one, below — showed the same class
of node again, this time as a DECISION node, and this time resolved. A live
Master committed one for a deliverable and withdrew it on the very next turn,
writing why: the framework had assigned the node `executor_role` `master`, and
the Master has no mechanism to produce the file artifacts the node owed, so it
could never satisfy its own required outputs. One turn, against the forty-three
minutes the REVIEW case cost. The difference is not the plan but the
discoverability: DECISION is the one type whose place in the unexecutable bucket
is documented, so its role was legible the moment the node existed, and Master
could see it had asked itself for a file it cannot write. `L-27` now records both.

**Five seats is still a property of the plan, and a work order leaves the plan
open.** The redesigned certification — three independent deliverables, one per
work seat, each with its own inputs in the objective — passed in the full sweep at
06:35 and failed its own re-run at 06:43, in 13:52. The project reached
`INCONCLUSIVE` on four nodes with the experimental seat never reached, and the
Decision Record says why in Master's own words: "Deliverable (3) needs a physical
specimen and an instrument, and the project has no evidence yet that either
exists. Committing a measurement node blind would either sit unexecutable in the
plan or invite a value to be filled in from literature." The objective had
described the specimen as "characterised" without saying it already was one, and
Master — correctly — read an unestablished precondition rather than a premise.
The run then spent a research task establishing availability, and the same Master
committed and withdrew a DECISION node for the measurement's prerequisites before
it did. Nothing here is a defect in a seat: the fix went into the objective, which
now states the bench as the requester's own given — the specimen is in hand and
characterised, the instrument is available — and says that no deliverable is
gated on another's findings. Master is told nothing about which nodes to create.

The reworded objective then produced exactly what the case is for, in 10:45: a
Master that committed "three dependency-free root nodes in a single decision — a
RESEARCH node for the literature route, a COMPUTATION node for the anchored
series values, and an EXPERIMENT node for the bench measurement", and a run in
which all three were carried out — one Research Record, and Execution Records
whose `executed_by` names `compute-worker` and `experimental-worker` — ending
INCONCLUSIVE on the merits, with the two mock results failed by a Review that
would not accept `simulated` artifacts and the literature delivery PARTIAL. One
run is one run, and the honest count for this item is now two passes and one
failure; what changed between them is a sentence about a bench, not the plan a
Master builds.

## 7. Phase 11

Phase 11 is a sequence of tasks, and the first thing it did was not one of them:
`L-27`, a node type that could be planned and could not be run, resolved before
the items that follow because every one of them would otherwise be built on a
plan that can hold work nobody is ever given. Three items have landed after it —
the reconciliation that keeps a dead run from stranding its node, the deep read
that lets a Research seat read back what it stored, and the readback that carries
a research result the last arrow of the chain, back to Master.

### 7.1 P11-01: execution reconciliation

Phase 11 opens with the hole Phase 10's own documentation named and left open.
`KNOWN_LIMITATIONS.md` L-24: every step of a run after planning is an activity
retried five times, and the last of them is the one that writes. When it exhausts
its retries the workflow ends FAILED, PostgreSQL keeps a node in `RUNNING` for a
run that no longer exists, and nothing in RAVEL ever asks again — the loop reads
`in_flight`, which is true and useless, and the project cannot reach an ending
while it holds one. It was found by a live run, and it is the limitation that
makes every other kind of autonomy unsafe, because a real Slurm cluster and a
real laboratory make a lost run more likely rather than less.

`ExecutionReconciler` closes it. On each supervisor tick it sweeps the live nodes
of every active project, asks Temporal about the one run each node is supposed to
have, and for a run that is gone it ends the job, moves the node to
`WAITING_DECISION` — the state that means Master is asked — and writes an
immutable `RunReconciliation` recording what it observed, in one transaction,
behind a re-read of the node so a decision that landed mid-sweep wins.

| | |
|---|---|
| Unit | `tests/unit/domain/test_reconciliation.py` — **16 passed**; `test_state_machines.py::test_a_durable_run_is_what_a_worker_seat_does` for the node-type set |
| Integration | `tests/integration/reconcile/test_execution_reconcile.py` — **22 passed** in 3.46s |
| Acceptance | `tests/acceptance/test_phase11_reconcile.py` — **9 passed** in 20.71s |
| Matrix | `make phase11-acceptance` — `PASS P11-01 execution reconciliation 9 passed` |

Run with `make phase11-acceptance` (the matrix above) or
`make phase11-acceptance-raw` for the suite alone. The acceptance cases run
against the real Temporal cluster; the integration cases drive the same code
against PostgreSQL with a scripted probe, which is what makes the races
reachable — a competing write that lands during the probe is arranged by the
probe itself, because that is where it happens in a deployment.

The expensive case is the one that reproduces L-24 the way L-24 happened.
`test_p11_01_a_run_that_died_writing_its_result_does_not_strand_its_node` starts
a real run against a backend whose `collect` raises — `finish_node_run` calls
`collect` outside its write transaction, so the activity fails while the job has
already reported COMPLETED — and waits out five real retries with the real
`1, 2, 4, 8` second backoff, about eighteen seconds. What it then asserts is the
whole point of the item: the workflow is observed FAILED, the job is left exactly
as the backend reported it, the node moves to `WAITING_DECISION`, and the
classification recorded is `INFRASTRUCTURE`, not `SCIENTIFIC`.

**What the reconciler refuses to do is most of the design.** It writes no
Execution Record, because a run that never reported did nothing RAVEL can attest
to and that record is a Worker's act in every other path. It does not send the
node to `REVIEWING`, because Review measures a delivered result against criteria
frozen before the run and an empty result is not evidence. It does not fail the
node, because `FAILED` is RAVEL saying the work failed and work that never
happened did not. It does not start another run: the workflow id carries
`REJECT_DUPLICATE`, and running work a second time opens a new node or a new
contract version, which is Master's decision. And it does not act on a probe that
established nothing — an unreachable frontend is `UNKNOWN`, and `UNKNOWN` is not
evidence that a run is dead. Each omission has a case in
`tests/integration/reconcile/`, and the cumulative claim is that a lost run
pollutes no scientific conclusion:
`test_p11_01_a_lost_run_leaves_a_question_and_no_scientific_verdict` asserts no
Execution Record, no PASS, and no ending.

The supervisor wiring is a read-only one. `ProjectSupervisor` holds a Temporal
client for the first time, and the sweep is the only thing it does with it:
`describe_workflow`, never `start_workflow`, never a signal, never a terminate.
A tick that fails to reconcile is logged and the supervisor carries on driving
the projects that are not affected, because a supervisor that died over one
unreachable node would take every other project down with it.

### 7.2 P11-02: research deep read

The second item is the other half of the first one's problem. A run that is lost
is one RAVEL cannot see; a source that cannot be read back is one it can see and
cannot use. `KNOWN_LIMITATIONS.md` L-25: every registration writes the bytes to
the object store and hashes them into the ledger, and no tool returned them, so
the most a Research session ever saw of a paper it had obtained was a
six-hundred-character excerpt — and for a PDF, nothing at all. The live run that
found it had identified the single source carrying the answer to its question
and could not read a word of it.

Three tools close it, all read-only and all Research's: `source_metadata`,
`read_source` and `search_source`. They read the snapshot rather than
re-fetching, they hash the bytes against the row's `content_hash` before
returning anything, and the provenance they report is the ledger row itself.
PDFs are addressed by page rather than by character, a scanned one is reported
as having no text layer, and nothing in the path can produce text the source did
not contain.

| | |
|---|---|
| Unit | `tests/unit/test_deepread.py` — **56 passed** in 0.31s |
| Integration | `tests/integration/research/test_deep_read.py` — **17 passed** in 33.5s |
| Live | `tests/live_research/test_live_deep_read.py` — **3 passed** in 13.2s, against a real arXiv PDF |
| Acceptance | `tests/acceptance/test_phase11_deepread.py` — **6 passed** in 19.1s, one of them live |
| Matrix | `make phase11-acceptance` — `PASS P11-02 research deep read 6 passed` |

The PDF cases are tested on real PDF files, not on strings that look like them.
`tests/documents.py` writes them byte by byte — header, catalog, per-page
content streams, and a cross-reference table with real offsets — because the
three cases that matter most cannot be produced any other way: a document with
no text layer (pages that are drawings), one that needs a password, and one
whose bytes begin `%PDF-` and are not a PDF. `pypdf` reads these the way it
reads a publisher's, which is why the same fixtures run through the real tool
server and the real object store in the integration suite.

What the acceptance cases assert is mostly about the *edges*, because the happy
path is the easy half. `reading_a_source_changes_nothing_about_the_project`
counts sources, claims and artifacts before four reads and after them. The
integration suite does the same and adds the refusals: a source registered
without a snapshot, a paywalled row, a reference this project never issued, a
character region asked of a PDF, and — the one that makes the rest worth
anything — a snapshot whose stored bytes no longer hash to what the row records,
where the read stops and names both hashes rather than returning text under a
provenance that does not describe it. The live case reads page one of a real
paper, checks the arXiv identifier printed on it, extracts the same page
independently with `pypdf` from the bytes in MinIO, and searches the paper for a
phrase taken off that page.

One thing the phase did *not* add is a second provenance system. The reading
tools write nothing: no row, no event, no artifact, no counter. What they return
as `source` is the ledger's own fields, and the only verification in the path is
the one the ledger already made possible.

That the item changed nothing outside a Research seat's reading was measured
rather than asserted, because P10-17 was failing at the time and this item was
the obvious suspect: `5f01ff9` and `HEAD` were run against one another on the
Phase 10 certification and are written up in §6. P11-02's whole footprint under
`src/` is three files and one new module, its three new tools are
`frozenset({RESEARCH})` like the rest of the reading surface, and no file under
`domain/`, `state/`, `execution/` or `master/` is touched at all. The one
difference the two runs' ledgers show is the difference the item is for: a PDF
the `5f01ff9` arm recorded as retrievable but with no extractable text, against
four PDFs read at `access_status = OK` in the `HEAD` arm.

### 7.3 L-27: the node type that could be planned and never run

This one is not an item. It is a defect a live certification run found, and it
was fixed before the next item was started because the items that follow assume a
plan is work. `KNOWN_LIMITATIONS.md` L-27: `REVIEW` was a plannable node type,
`NODE_EXECUTOR` assigned it to the Review Agent, and the loop has no execution
path into a REVIEW node at all — that seat's whole surface is verdicts about
*other* nodes. A Master that planned one had asked for work no seat could be
given, and nothing it could read said so. Nine of them were planned in a single
certification run, all nine cancelled, thirty `CANCEL_NODE` decisions and
forty-three minutes spent on nodes that were never going to move.

The resolution has two halves, because either alone leaves the other standing.
The type is gone from the plan: `PLANNABLE_NODE_TYPES` is what a node may be
created as and `DagNode.create` refuses anything outside it, so every path into
the DAG meets the same refusal rather than each tool remembering to check. And
what is *still* unexecutable — `HYPOTHESIS` and `DECISION` are plannable and
belong to Master, who is not an execution seat — is now explained where it is
read: `unexecutable_reason` is one sentence, carried by `Situation.unexecutable`,
by Master's turn prompt, and by `read_project_state` under `stopped`.

`NodeType.REVIEW` stays a member of the enum and of the database's check
constraint, because a node written while the type existed is a record of what a
project did and has to stay readable. `executor_for` answers `None` for it rather
than raising, and the model validator tolerates a stored executor that is no
longer in the table. **No migration was written and none is needed.** Ending a
node that is already in the DAG is a DAG mutation: it requires Master and a
Decision Record saying why, and a migration that cancelled every surviving REVIEW
node would be RAVEL taking an act that is Master's.

| | |
|---|---|
| Unit | `tests/unit/domain/test_state_machines.py` — **255 passed** with the rest of `tests/unit/domain` |
| Integration | `tests/integration` — **580 passed** (exit 0) |
| Acceptance | `make acceptance-raw` — **43 passed**; `make phase10-acceptance-raw` — **58 passed** in 1741.52s (29:01) |
| Lint | `ruff check src tests` and `pyright src tests` — clean |

The regression tests are the fix's own shape. `test_a_node_may_not_be_created_as_the_retired_type`
holds the creation path; `test_no_role_executes_a_review_node_and_no_plan_may_hold_one`
holds the table and the set together; `test_a_legacy_review_node_can_still_be_read`
reads a stored one, which is the half that a fix by deletion would have broken;
`test_each_plannable_node_type_has_exactly_one_executor` is the general property
the four specific ones are instances of. Over the real transport,
`tests/integration/roles/test_permissions.py::test_a_review_node_is_refused_at_planning_and_the_plan_keeps_nothing`
asserts the refusal *and* that the plan holds nothing afterwards — a tool that
refused after writing would pass a test that only asked whether it raised — and
`tests/acceptance/test_phase10_agents.py::test_l27_a_node_nobody_can_run_is_named_to_master`
drives a Master through a real project and requires the prompt it is given to
name the node and say why nothing will run it.

One correction to the record: `30cbf6d`'s message reports `tests/integration` as
583 passed. That run was taken with this section's own test file — P11-03's, then
untracked and already on disk — inside the tree, and three of its cases were
passing at that moment. Measured on the commit itself, the suite is **580**.

### 7.4 P11-03: research → Master result readback

The chain the architecture claims is `Master → Research Agent → Evidence /
ResearchRecord → Master → Decision`, and the last arrow had no tool behind it.
Master could read the whole project state and could not read one research task's
result, so what a task found reached the next decision only if the session that
received it was also the session making it. Across a crash, a rebuild or a
supervisor restart — the situations a long-lived project is made of — it is not,
and the record that would have answered the question was in PostgreSQL the whole
time with nothing pointing at it.

Four tools close it, all reads, all Master's, none of them writing anything:
`list_research_results` names each research task and what it delivered — record,
completion status, sufficiency, claim/source/conflict counts, verdict — and
`read_research_result` returns one task's record, the claims it was assembled
from, their sources, the conflicts between them, a sufficiency assessment
measured live off the ledger, and the verdicts given about the node.
`read_evidence` and `read_source_metadata` are the narrower reads a decision
turning on one claim needs, and the second returns no text at all: what a
document says is read by the seat whose task it answers.

`read_project_state` gained `completed_research`, the research tasks that have
handed a result over, one flat summary each. It is a pointer, not the evidence —
a task still in flight is deliberately absent, and no claim text reaches the
state read, which is the read every turn begins with. A Master with no memory of
the turn a record arrived in learns from the state it reads first that there is
something to read, and the turn prompt says the same thing in the same words for
the same reason.

| | |
|---|---|
| Unit | `tests/unit/test_tool_roster.py` — **33 passed** |
| Integration | `tests/integration/master/test_research_readback.py` — **6 passed**; `tests/integration` — **586 passed** in 270.74s (4:30) |
| Acceptance | `tests/acceptance/test_phase11_readback.py` — **4 passed**; `make phase11-acceptance-raw` — **19 passed** in 55.82s |
| Matrix | `make phase11-acceptance` — `PASS P11-03 research → Master result readback 4 passed` |
| Lint | `ruff check src tests` and `pyright src tests` — clean |

What makes the item a claim rather than a tool listing is that the two directions
are asserted together. The record is produced through the *Research seat's* own
server, in another process, over the same stdio transport a deployment uses, and
every field Master reads is compared against the rows in PostgreSQL — so a
readback that returned what a fixture had put in a convenient place would fail.
The other direction is the roster: `test_the_readback_tools_are_masters_and_write_nothing`
holds that no non-Master role holds any of the four and that none of them is in
`WRITE_TOOLS`, the acceptance case asks every role over the real transport, and
the ledger's own writing tools are still refused to Master. A Master that could
file its own evidence would be the judge of a record it wrote.

The filter is a claim too, and it is the one this report has to correct. The
first version of the case asserted that a research task nothing had been recorded
on was absent from the whole state read — which is false, and the test said so:
the state read names the DAG, and a READY research node is in `ready_to_run`
because that is what it is. What the item means is narrower — that such a node is
not reported as a *result* — and the case now asserts both halves, that the node
is named as a node and not as a result, so that it cannot pass against a task the
read never mentioned at all. `HANDED_OVER_NODE_STATUSES` is what the filter turns
on: `REVIEWING` is where a seat leaves finished work, and `PASSED`, `FAILED` and
`PARTIAL` are where a FINAL verdict moves it.

### 7.5 P11-04: execution preparation

An Execution Contract may name the environment its work needs — `{"software":
"raspa"}`, `{"lab": "bench-chemistry"}` — and the sentence the layer exists to
make true is that *a run happens in a workspace built from the contract, or it
does not happen at all*. The materializers, the refusals and the record were
already in place; what this item found is that a refusal could be written,
correct, and invisible to the only seat that may answer it.

**The finding is a live one, and its evidence is a Master's own words.** In a
five-agent run a node refused preparation, the workflow parked it at
`WAITING_DECISION`, and Master read the project state, found nothing that named
a reason, and cancelled the node. Its cancel rationale is quoted in the
acceptance module verbatim: "the state records no verdict from a seat, no run
reconciliation from RAVEL, and no unexecutability reason". Every clause was
true and the refusal had been in `execution_preparations` the whole time.
`read_project_state`'s `stopped` block gained `preparation_refusal` as a third
field beside `verdict` and `run_reconciliation`, `PreparationRepository.
stopping_refusal` reads across contract versions and reports the refusal only
when it is the *newest* thing RAVEL did for the node, and the Master turn names
all three fields — because a reason in a record no prompt mentions is a reason a
session will not go and read.

Two smaller corrections came with it. A refusal now records `required_outputs`
from the contract, so a refusal describes what the node owed and not only what
went wrong; and the object store is required by a *resolved* input rather than a
named one, so a contract whose inputs are all names the project holds nothing
under — the research contract, a file the run produces itself — no longer
refuses on a host with no store configured. That second one was a refusal about
the host for a fact about the project.

| | |
|---|---|
| Unit | `tests/unit/preparation/` — **82 passed** in 0.13s |
| Integration | `tests/integration/temporal/test_preparation.py` — **9 passed** in 12.50s; `tests/integration/state/test_preparations.py` — **7 passed** in 1.15s |
| Acceptance | `tests/acceptance/test_phase11_materialization.py` — **2 passed** in 4.12s |
| Gate row | `compute-preparation` — **43 passed** in 16.47s |
| Matrix | `make phase11-acceptance` — `PASS P11-04 execution preparation 2 passed` |

The two acceptance cases are the two directions of one claim. `test_p11_04_a_
contract_this_deployment_cannot_build_reaches_master` refuses, then reads the
refusal back through Master's own tool field by field against the row, and
asserts the prompt that hands Master the node names the field it is in.
`test_p11_04_the_same_contract_prepares_where_the_environment_exists` runs the
identical contract against a deployment that can build it, which is what keeps
the item from being satisfiable by a layer that refuses everything — the
distinction it asserts is between a limit of this machine and a fault in the
work, and a suite with only the first case could not tell them apart.

**A live Master writes contracts this deployment has not registered.** The same
run produced `{"software": "python"}` and `{"lab": "four-point-probe"}` and both
were refused as `UNSUPPORTED_ENVIRONMENT`. That is the layer working: a contract
naming an environment RAVEL cannot build gets a refusal that says so rather than
a workspace that pretends, and the refusal is attributed to RAVEL rather than to
the host, which is a different sentence for Master to act on.

## 8. What these numbers do not say

- A green suite is not a proof of correctness. It is a record of what was
  exercised, and `KNOWN_LIMITATIONS.md` is the record of what was not.
- Every number above was measured once, on one Ubuntu 24.04 host, with one set
  of credentials. Re-running on a different network, a different DeepSeek
  account, or a different DSH release may surface different behavior.
- The project loop has been observed with a real model in the DSH spike tests,
  and as of Phase 10 it has also been observed as `P10-17`: unattended runs of a
  whole project, five live seats, between 16 and 43 minutes, ending `FAILED`,
  `INCONCLUSIVE` and `CANCELLED` across them — and no two of them built anything
  like the same plan, including the pair that differed only in the source tree.
  That is a handful of runs, not stress-testing, and it is the reason the item
  asserts what the architecture owns rather than what a plan happened to contain.
  Repetition, runs of hours rather than minutes, and the endings that need a
  non-mock backend to reach are all outside what has been measured.
  `KNOWN_LIMITATIONS.md` L-19 says so in the same words.
- `40/40 demonstrated` is a claim about the tests that ran, not about the
  science. Every live run of `P10-17` ends `FAILED`, `INCONCLUSIVE` or
  `CANCELLED`, because V0's backends are mocks and a live Review will not accept
  a `simulated` artifact as a measurement — though those are not the only roads
  to a non-`COMPLETED` ending, and the `CANCELLED` one took another: a live
  Master that could not get its review gates dispatched terminated its own
  project rather than wait. A run reaching an ending says the architecture
  carried the project there, not that the hypothesis was answered.
