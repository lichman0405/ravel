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

### 7.6 P11-05: the real Slurm backend

Computation can run on a real cluster, over SSH, through a real `sbatch`, and
both halves of the item are about what "real" costs: something outside RAVEL has
to be configured, and something outside RAVEL has to be true. Only the first
half is testable here, and the item says so in its own words rather than
counting the cluster it does not have.

The configuration half is what the two unskipped acceptance cases assert. A
worker told to use a cluster it was not given **refuses to start**, naming the
variable to set — `build_registry` under `--compute-backend slurm`, with neither
coordinate, with a host and no account, and with both. The alternative is the
failure the item was written against: a worker that comes up, takes work, and
stalls on the first node that reaches the queue, an hour later, with the reason
in a traceback rather than in front of the person who started it.

The credential is the one secret in RAVEL that is not RAVEL's own, and the
second case asserts by *value* that it reaches the worker and nothing else: not
the environment of the DSH runtime or any tool server RAVEL starts (it is in the
settings' launcher-only prefix set), not the start-up banner, and not what the
redactor leaves in a `detail`, a progress fact, an exception message or
`completion_metadata`. The integration suite adds the case that makes the
redaction worth asserting — a cluster that echoes the password on every command
— because a redactor tested only against strings that never contained the
credential is a redactor nobody has run.

| | |
|---|---|
| Unit | `tests/unit/backends/slurm/` — **98 passed** in 0.10s |
| Integration | `tests/integration/backends/test_slurm_collection.py` — **9 passed** in 1.53s |
| Acceptance | `tests/acceptance/test_phase11_slurm.py` — **2 passed, 1 skipped** in 0.74s |
| Gate row | `slurm-integration` — **115 passed, 1 skipped** in 7.19s |
| Matrix | `make phase11-acceptance` — `PASS P11-05 the real Slurm backend 2 passed, 1 skipped` |

**The item is PARTIAL, and the missing part is not a test that was not written.**
`test_p11_05_a_real_cluster_runs_the_workspace_preparation_built` skips, naming
four things at once: `RAVEL_SLURM_HOST`, `RAVEL_SLURM_USERNAME`, a credential
(`RAVEL_SLURM_PASSWORD` or `RAVEL_SLURM_KEY_FILENAME`) and `RAVEL_RASPA_DATA_DIR`.
Whether a given cluster accepts RAVEL's job script is a fact about that cluster —
whether the account may submit, whether the filesystem RAVEL writes into is
mounted, whether the software the contract names is installed — and no test in
this repository can establish it. The release gate reports the row as skipped
rather than certified, which is the reason its two verdicts are different words.
Supplying an endpoint and running `RAVEL_REQUIRE_SLURM=1 make phase11-acceptance`
is what closes it, and until somebody does, the honest statement is that the
adapter is exercised against a scripted cluster and has never been run against a
real one.

### 7.7 P11-06: the human laboratory channel

The directive names the chain in full — `Master → Experiment Contract →
LabPreparation → Experimental Worker → HumanLabBackend → LAB_USER → result /
deviation → Review → Master` — and the backend, the handover record and the
person's upload door were already committed. What was missing is the thing only
a *run* can show: a real durable wait, a real signal, and a seat that reads the
ending afterwards. A backend can hold every one of its promises and a workflow
can still lose the answer, because between them sit Temporal's durability and
PostgreSQL's authority, and nothing but a run exercises both.

Writing the acceptance module is what found the two defects, which is the reason
the item is reported here rather than in the commit that built the channel.

**A bench's wait was mirrored as a run.** `HumanLabBackend.submit` answers
`WAITING_EXTERNAL` — a person at a bench has the work the moment it is handed
over and RAVEL can see nothing about them afterwards — and `start_job` recorded
the job without moving the node, so a contract handed to a bench read `RUNNING`
for as long as the bench took. It was not self-correcting either: `_watch`
enters the durable wait precisely *because* the state is `WAITING_EXTERNAL`, so
`check_job` is not called again until the answer arrives, and the disguise would
have held for a week. Both halves of `start_job` now mirror the node — the
fresh submission and the recovered handover, the second because that is the call
that would otherwise leave a recovered wait reading as a run.

**A deviation could not end the run it stopped.** `_stop_for_deviation` stopped
the job without moving the node, on the rule that `finish_node_run` decides
where a node ends up. That rule is right and the omission was still wrong, and
the state machine said so in as many words: `finish_node_run` refuses a node
that is still waiting (`node EXP-C8C1695F is WAITING_EXTERNAL but the run that
started it has only just ended; something else has moved it`). A wait ends by
*resuming*; the stop leaves the job `CANCELLED`, so resuming is what
`_follow_node_status` computes, and it is now called in the same transaction as
the stop. A deviation is the one report that ends a run without the job ever
resuming, and `abandon_job` is the other path with that shape.

| | |
|---|---|
| Integration | `tests/integration/backends/test_lab_backend.py` — **13 passed** in 2.41s; `tests/integration/gateway/test_lab_handover.py` — **17 passed** in 4.33s |
| Acceptance | `tests/acceptance/test_phase11_humanlab.py` — **3 passed** in 1.66s |
| Gate row | `humanlab-integration` — **33 passed** in 7.94s |
| Matrix | `make phase11-acceptance` — `PASS P11-06 the human laboratory channel 3 passed` |

The three cases are the three claims the item makes. `test_p11_06_masters_
contract_reaches_a_bench_and_comes_back_as_a_record` walks the whole chain: the
node is prepared by the real materializer through the real activities, the
package the person is handed is the one RAVEL wrote, the upload goes through the
same function the Gateway's route calls, and the delivery is a real signal to a
real workflow. `test_p11_06_a_delivery_that_does_not_cover_what_is_owed_does_not
_finish_it` is the directive's own sentence — 不能上传一个任意文件就自动完成，
必须根据 `required_outputs` 检查 — asserted where it belongs, because "the run is
still waiting" is a fact about a workflow rather than about a backend. And
`test_p11_06_a_bench_that_reports_a_deviation_stops_the_run_for_master` is the
second defect above, as a case.

One assertion in the first case is worth recording because it is the shape the
projection has rather than the shape a reader expects. The case asserts
`at_a_glance["REVIEWING"] == 1` and then
`at_a_glance.get("WAITING_EXTERNAL", 0) == 0` — `.get`, because a status nobody
is in is left out of the tally rather than reported as zero. The second form is
the stronger statement of the two: the bench's wait is over, and the projection
has stopped counting it at all.

What is scripted is the *seats*. No model is asked anything here, because what
the item is about is the channel rather than what a Master would decide with it;
the live five-agent run is where a real Master is on the other end.

### 7.8 P11-07: the release gate

The matrix above answers "does every item have a case, and did the cases pass".
A release asks a different question — is the *system* certifiable — and the
difference is where this item's risk lives, because a matrix can be all green
while lint is red, while a suite nobody runs is broken, or while a live
certification was never attempted. `scripts/release_gate.py` runs seventeen rows
and prints one of three words. `CERTIFIED` is every row ran and passed;
`PARTIALLY_CERTIFIED` is every row that could run passed and at least one could
not, for a reason outside this repository, printed with the dependency it named;
`NOT_CERTIFIED` is a row that ran and failed. The exit code is the verdict, and
a skip that matches no dependency this repository knows is reported as
**unattributed** rather than filed under "external" — an unexamined skip is
exactly what a certification must not absorb.

**The first full run returned `NOT_CERTIFIED`, and that is the item working.**
Twelve rows passed, including all four that need something RAVEL does not own —
the live research row, the live five-agent end-to-end run (10m35s), and the two
live acceptance rows — and two failed:

- `e2e` — `cannot address '/projects/{project_id}/lab/tasks/{task_id}/handover/
  documents/{document}'; teach this probe its placeholders`. The route added
  with the laboratory package door was taught to the integration probe in
  `tests/integration/gateway/test_projects.py` and not to the e2e twin in
  `tests/e2e/test_tui.py`; both enumerate the same application, and the second
  one crashed on a placeholder it had never seen. The fix is the class rather
  than the instance: `tests/support/routes.py` now holds one
  `ABSENT_PLACEHOLDERS` table and one `probe_path()` that raises on a leftover
  `{name}` instead of sending a literal one, and both probes call it — which is
  also what stops the next route from being taught in one place and not the
  other.
- `dsh` — `the database this suite is pointed at is older than the models, so
  these tables are missing: execution_preparations, lab_handovers`. The
  deployment database was three revisions behind the models; `make migrate`
  applied `d4e7a1b90c26 -> c8f2a5d10e47 -> e1b7c3f4a920 -> a7c3e5b1f284` and
  the row went green on re-run.

| | |
|---|---|
| Gate, first full run | **NOT_CERTIFIED** — 12 rows PASS, 2 FAIL (`e2e`, `dsh`), 3 SKIP (`v0-acceptance`, `phase11-acceptance`, `slurm-integration`, all attributed to `RAVEL_SLURM_HOST`) |
| Re-run after the fix | `tests/e2e` — **20 passed** in 25.57s; the `dsh` row (`RAVEL_REQUIRE_DSH=1 … tests/dsh -m dsh`) — **11 passed** in 40.48s |
| Acceptance | `tests/acceptance/test_phase11_release_gate.py` — **5 passed** in 0.70s |
| Matrix | `make phase11-acceptance` — `PASS P11-07 the release gate 5 passed` |

The one wording defect that run exposed is fixed too: rows that *ran* with a
skip inside them were printed under "Rows that did not run", which is false
about `phase11-acceptance` — a row that ran all seven of its items and skipped
one case. The heading now says what those rows are. It is a small thing to
report in a test report and it is the same defect the whole item is about: a
summary sentence standing in for a check, in the direction of stating less than
happened.

**The second half of the item is not the script.** Every integration suite ran
on the deployment's Temporal task queue until now, so a test could put work in
front of a deployment's Execution Worker and have it answered against a database
the test does not own. Both halves are refused rather than trusted:
`integration_settings` builds the queue as `ravel-v0-test-<uuid4>` per process —
so two suites never take each other's workflows either — and it *checks* the
override against what `Settings()` reads from `.env`, raising if they match, so
deleting the line fails rather than passing quietly. The `_test` suffix rule on
the database sits one layer below it, and L-26 — the entry that recorded a role
session on the deployment's queue — now carries its resolution.

`make release-gate` is the runner and `make release-gate-rows` prints the rows
without running them. The full run takes about fifty minutes, most of it the
live rows; the certification run for the phase is recorded in §2.

### 7.9 P11-08: users, projects and membership

The three Phase 11 items before this one gave RAVEL a real cluster and a real
bench. This one decides who may point them at something, and every claim in it
resolves to the same place: **authority is a row in `project_memberships`,
re-read on the request that wants it.** Nothing a caller presents carries it,
and the only thing that creates a person is an operator at a terminal.

**The operator's own script is what makes an account.** `tests/acceptance/
test_phase11_membership.py` runs `scripts/create_account.py`'s `main` — its
flags, its one transaction, its refusals — against the database the Gateway is
serving, and then logs in as the account it made, over a real socket, against a
real uvicorn. The one substitution is where the script reads its configuration,
because production's script is the only thing running and `Settings()` in a test
is the deployment's; everything else, including the Argon2id hash the account is
stored with, is the operator's path. The other half is asserted against the
running Gateway: every `POST` route it declares is probed with an owner's token
and with nobody's, and the accounts are counted afterwards. There is no open
registration, and the probe is what says so rather than a reading of the routes.

**A withdrawal is history, not an edit.** `revoke` writes `revoked_at` and
`revoked_by` onto the row and emits `MEMBER_REVOKED`; the uniqueness constraint
that used to be `(project, user)` became a partial unique index over live rows,
which is what lets the same person be granted later without the two grants being
confused for one; and the last owner cannot step down, refused in the repository
so the script and the routes obey the same rule. A membership that is withdrawn
is a project's history and no longer a project the person may open, and the
acceptance case holds a token issued *before* the withdrawal to assert that:
`/auth/me` still answers and names the same person, `/projects` is empty, and
every route into the project answers 404 on that same credential.

**One real defect, found by the acceptance case and fixed in the query.** The
route that reads one project consults the membership on every request, so a
withdrawal took effect there immediately. The route that lists the projects a
caller may open did not: `memberships_of` — also what `/auth/me` builds its
membership list from — filtered on the user and not on the revocation. That was
correct for exactly as long as no membership could be withdrawn, which is why
it survived every suite until this item gave it a withdrawal to be wrong about.
The symptom is not a stale label: the client is offered a project that every
other route refuses it, so the TUI draws a project it cannot open. The fix is
one `revoked_at.is_(None)` and a docstring that says why; the case that pins it
is `tests/integration/state/test_identity.py::test_a_withdrawn_membership_is_
not_a_project_the_user_belongs_to`, which asserts the owner's own list too,
because the cheap way to make the first half pass is to filter everything away.

| | |
|---|---|
| Acceptance | `tests/acceptance/test_phase11_membership.py` — **8 passed** in 4.11s (real uvicorn, real socket, real PostgreSQL, the operator's own script) |
| Integration, gateway | `make test-integration` — the 142 cases of `tests/integration/gateway/` pass, including the fourteen in `test_members.py` |
| Integration, identity | `tests/integration/state/test_identity.py` — **27 passed**, one more than before this item, and the one that is new is the defect above |
| Static analysis | `ruff check src tests` and `pyright src tests` — clean, 0 errors, 0 warnings |
| Matrix | `make phase11-acceptance` — `PASS P11-08 users, projects and membership 8 passed` |

**What the design deliberately does not do.** `ADMIN` outranks `PROJECT_OWNER`
in the domain's ordering, and the item is explicit that this must not become a
platform superuser: the ranking exists so that authority cannot be *escalated*,
and `may_direct_project` is false for `ADMIN`, so an administrator can answer an
approval and cannot decide the research route. The acceptance case asserts the
half a route can get wrong — a project an administrator holds no membership in
is answered 404, in the same words as a project that never existed — and the
row's own `may_direct_project` is asserted alongside it. And `POST /projects` is
open to any authenticated caller, which is not an escalation either: a person
who opens a project owns a project that did not exist a moment ago, and what
putting somebody into *somebody else's* project requires is an owner of that
project.

`KNOWN_LIMITATIONS.md` moves with it. L-17 loses its second half — "nothing
revokes a membership" — and keeps its first, with what is *still* absent named
rather than implied: an account cannot be deactivated, and no membership expires
on a clock. `README.md`'s security list said the same thing and now says this
instead.

### 7.10 P11-09: role-specific surfaces

`docs/08` §5 gave three people three views and the console had one screen with a
role check in front of it. This item is the claim that the surfaces are
*genuinely different* — three screens whose actions differ because the authority
behind them differs — and the differences that matter are absences: the
administrator's console has no approval control because an administrator has no
approval authority, and an absent method is the honest rendering of absent
authority rather than a disabled button.

**Two of the three screens already existed and grew; the operator's is new.**
The reason it is new is that everything an operator needs — is the supervisor
alive, is Temporal reachable, what has the work actually been handed to, which
jobs are stuck, what had to be recovered — is a fact about the runtime rather
than about a project, and there was nowhere to read it. It is answered
*project-scoped*: the panel reports how many projects a service holds and not
which, because a membership in this project is what admits the caller and other
people's research is not the price of an uptime check.

**The one panel that reads a process's account of itself says so.** Whether a
supervisor is running is not derivable from any row — a project it stopped
driving and a project with nothing left to do are the same state — so the
alternative to trusting the report is not verifying it, it is having no answer.
What is not taken on trust is the service's word for its own health: the age is
computed by the Gateway from a clock it owns, against a cadence the service
promised in its own report, and `runtime_services` is guarded so that a
service's identity columns cannot be rewritten. A service that has never
reported is drawn as one that has never reported, which is a different sentence
from one that has gone quiet, because the two send an operator to different
places. `KNOWN_LIMITATIONS.md` L-30 records what remains unverified about it.

**One real defect, found by the acceptance case and fixed in the screen.** The
bench's Send-a-result control posted the file to the generic artifact route and
never said which required output it answered. That route files bytes as an
artifact and nothing more, so an upload from the console could not become part
of a delivery: the prepared panel's split of sent-and-owed could never move, and
the one control the item names beside *required outputs* was disconnected from
them. The fix gives the panel a selector built from the contract's own names —
so a file is attached to an output that was actually owed, and a filename cannot
be mistaken for one — and routes the upload through the handover door, which
checks the name against what was handed over, records the version against the
handover, and tells the waiting run. The A/B is recorded: with the old screen the
new acceptance case fails, and the failure is the panel never leaving
`still owed`. A18's lab case moved with it, because a world nothing has been
handed to has no output to answer; it now runs on a project a bench has really
been given, and the e2e case that used the old door asserts the refusal instead.

| | |
|---|---|
| Acceptance | `tests/acceptance/test_phase11_roles.py` — **5 passed** in 6.97s (the three screens, the operator's prohibitions, and the distinctness of the mapping) |
| Acceptance, A18 | `tests/acceptance/test_surfaces.py` — **4 passed**, including the lab case rebuilt on a real handover |
| Acceptance, P11-06 | `tests/acceptance/test_phase11_humanlab.py` — **3 passed**, unchanged, after its two preparation helpers moved into `tests/support/lab.py` for the second caller |
| End to end | `make test-e2e` — **27 passed**; `tests/e2e/test_tui.py` alone is **19**, six of them added here |
| Integration, gateway | `tests/integration/gateway/test_admin.py` and `tests/integration/state/test_services.py` — **38 passed** in 5.79s |
| Integration, all | `make test-integration` — **579 passed, 126 deselected** |
| Unit | `make test-unit` — **1088 passed** |
| Static analysis | `ruff check src tests scripts` and `pyright src tests` — clean, 0 errors, 0 warnings |
| Matrix | `make phase11-acceptance` — `PASS P11-09 role-specific surfaces 5 passed`, **9/9 demonstrated** |

**What the acceptance case does that a screen test cannot.** The bench's case
walks the whole channel through the console: the node is prepared by the real
materializer, the run is a real workflow, and the two files are typed into the
path field and attached to outputs chosen from the contract's list. After the
first the panel splits and the node is *still waiting* — a file that answers
half of what was owed finishes nothing; after the second the run reads what was
recorded, closes the handover and moves the node to the seat that judges it.
Nothing is asserted from the screen alone: the artifacts are read back out of
MinIO through the repository, attributed to the account that sent them.

### 7.11 P11-10: managed services

The three long-running processes existed since Phase 9 and nothing started them
again. This item is the difference between a process that can be run and a
process a machine keeps running, and most of it is not Python: it is three unit
files, an installer that writes them, and one column that says a stop was
deliberate.

**The restart policy is a file, and it is read as one.** `Restart=always` rather
than `on-failure`, because this system's failures are not all crashes: a
supervisor whose loop raised and exited cleanly is a project that has stopped
moving, and `on-failure` would leave it there. `KillSignal=SIGTERM` is written
out though it is systemd's default, because it is load bearing — the
application's lifespan and the supervisor's shutdown path run on that signal.
`tests/unit/test_service_units.py` reads the three files rather than restating
them, and `tests/unit/test_service_logs.py` holds the other half: one JSON
object per line, `extra` fields flat beside the reserved five, and an unknown
format refused at start-up rather than read as text.

**A stopped service and a killed one are told apart by a column.** Both endings
are silence; what separates them is that one of them said so on the way out. The
row is never deleted — `runtime_services` is on the append-only guard, so a
`DELETE` is refused by a statement-level trigger, which matters because a
process able to erase its own row could erase one belonging to a service that
never said anything — and a beating process clears `stopped_at`, because a
process that came back is running.

**The acceptance chain is proved in two halves, because it is two claims.** The
restart is systemd's, and it is demonstrated with a real transient user unit
running the real `scripts/run_supervisor.py`: the policy is read out of the
checked-in unit rather than restated in the test, the process is `SIGKILL`ed,
and the row's own `instance` is compared against `systemctl show -p MainPID`. The
recovery is RAVEL's, and it is demonstrated in process, where a scripted Master
is affordable. Neither stands in for the other.

**What "no duplicate submission" is asserted on.** Not on artifacts — an
artifact produced once and a computation submitted twice look identical
afterwards, and the second is what a lab invoices for. The second supervisor's
case wraps the real mock backend in a ledger and asserts that every
`(project, node, attempt)` was answered with **one** `backend_job_ref` — which is
the contract `WorkBackend.submit` is written to, so a repeated *call* answered
with the same reference is the idempotency working rather than a defect.

**The first defect this case found was in the case.** Its first version used the
suite's own four-second run deadline, so the run it needed still in flight was
over before the first supervisor had stopped, and the node reached Master as
"nothing passed". The deadline is a deployment's number rather than a policy
under test; the case lengthens it for its duration and waits for the job to
reach `RUNNING` — not `SUBMITTED` — before stopping the supervisor, because
stopping on the first sight of a submission sometimes stops it mid-`start_execution`,
which is a stranded run with a different answer and a coin toss rather than a
test.

**The second, and the one that cost the afternoon, was also in the case.** The
wait compared the port's answer to a state — `counting.status(ref) is
JobState.RUNNING` — but `WorkBackend.status` returns a `JobStatus`, the record
*about* a job, and a record is never the state it reports: the comparison is
false for every state a job can be in, so the case failed on a job that had run
to completion, and it failed the same way in each of five runs. What made it
look like a product fault is that the database was right — the run reached
`RUNNING` at +10s and the project `COMPLETED` at +21s — because the *workflow*
reads `.state` and the test did not. It was isolated by watching the same
expression from a second coroutine in the same event loop: the observer, which
printed `.state`, saw `SUBMITTED → RUNNING → COMPLETED` while the wait beside it
saw nothing for its full two minutes. A wait that cannot be satisfied by any
observation is not a slow system, and the first thing to suspect is the
predicate rather than the stack under it.

| | |
|---|---|
| Acceptance | `tests/acceptance/test_phase11_services.py` — **3 passed** (the systemd restart, the recovery, the four-state endpoint) |
| Unit | `tests/unit/test_service_units.py`, `tests/unit/test_service_logs.py` — **18 passed** |
| Integration | `tests/integration/state/test_services.py` — **14 passed** |
| Static analysis | `ruff check src tests scripts` and `pyright src tests` — clean |
| Matrix | `make phase11-acceptance` — `PASS P11-10 managed services`, **10/10 demonstrated** |

### 7.12 P11-11: the whole chain, certified end to end

Ten items certified ten parts, and none of them said the parts join. This one is
the join: one project, one run, every leg planned from what the leg before it
produced, and an ending that rests on the row the first leg wrote.

**The join is stated four ways, and all four are recorded.** The order is the
first — the measuring stage cannot exist before the reading has ended and left a
claim, because its criteria are written from that claim — and it is the weakest
of the four, because a script could take that order and write nothing down. The
other three survive the session: `dependencies` on both measuring nodes naming
the reading node with `JoinPolicy.ALL`; `LITERATURE_DERIVED` on every frozen
criterion with the evidence row's identifier in `provenance_ref`, a pairing the
contract's own validator requires, so it cannot be omitted; and the claim in
`evidence_refs` on the `DecisionDraft` that creates the measuring stage and on
the one that ends the project. **A dependency is the DAG's record of where work
came from, and the case does not claim more than that for it**: V0 records
dependencies and does not gate readiness on them, and only
`replan_after_failure` reads them.

**The claim crosses through Master's own readback, not around it.** The scripted
Master reads the reading leg with `read_research_result` — the tool the seat
calls over MCP — and the case asserts that what it was handed is what the ledger
holds: the same `evidence_id`, a Research Record present, and a PASSED node. A
case that queried the ledger directly would have certified a path nothing in
production takes, and the difference is exactly what P11-03 exists for.

**The derived join is the scripted case's claim, and deliberately not the live
one's.** The live case runs `LIVE_OBJECTIVE`, the Phase 10 work order, and that
objective says in as many words that none of the three pieces depends on another
and none may be gated on what another turns up. A live plan with a derived second
stage in it would be a Master ignoring its work order, so the live case asserts
the run's shape and leaves the derived planning to the case that can fix the
plan. Two cases, two claims, and neither one covers for the other.

**What the case does with the two legs software cannot supply.** No Slurm host is
configured, so the computation runs `MockComputeBackend`, and the *scripted* case
asserts on a joined run the property the marking exists for: every artifact the
mock produced is `simulated` with its scenario in `provenance`, and the set of
simulated artifact identifiers is disjoint from every claim's `source_refs`. The
live case asserts the second half of that pair unconditionally and the first half
over the computations it actually ran, because whether a live run *contains* a
computation is the deployment's answer rather than the model's: this host
registers RASPA's materializer and the bench's, and RASPA's refuses to choose a
simulation's parameters rather than inventing them. The item's first live run
tried `software/python`, was refused for having no materializer here, tried
`software/raspa`, was refused for naming no temperature or pressure or cycles,
and re-committed the deliverable as a research node — three correct moves, each
with its reason on the record. A case that demanded a marked mock artifact from
that run would have been demanding that the model plan what the machine cannot
prepare. A person at a bench cannot be scheduled either, so the person is the
test: it files one stub against each name the handover requires and delivers
through the real `deliver_external_result` signal. Both legs are marked as not
real everywhere they appear, which is what makes the certification honest rather
than complete.

**What it found was in the harness.** `ScriptedReview` resolved a node's
definition of done through the acceptance-contract table and asserted criteria
existed — but `FROZEN_CRITERIA_NODE_TYPES` is `{COMPUTATION, EXPERIMENT}`, and a
RESEARCH node has none to freeze: it is judged against its Execution Contract, on
the Research Record it submitted. The script could therefore not judge a node
type the product judges, and the failure it produced —
`AssertionError: RES-AC298FE5 has no frozen criteria` — was the fixture
answering for RAVEL instead of testing it. It now resolves the contract through
`ReviewService.definition_of_done`, the call `submit_review` makes. The second
finding was in the case: the master recorded one node identifier per stage where
a stage held two, so the measuring stage looked like a single node — the
assertion that reads *what Master planned* is what caught it, which is the
argument for asserting on the plan rather than on the run's artifacts.

**The other two findings came from the live half, and both were the case's as
well.** The person filed a fixed pair of output names — the ones the *scripted*
contract owed — at whatever package the live Master had written, and the first
live run's Master had written `undoped_tio2_conductivity_300K.csv` and
`undoped_tio2_measurement_report.md`. Nothing answered the handover, so the run
waited out its full external deadline and recorded the execution `TIMED_OUT`
with `INCOMPLETE_DELIVERY`, Review failed all six criteria on absence, and Master
rejected the result: every step after the upload was the product being right, and
the upload was the case being wrong. The helper's own docstring had said all
along that it files what the contract required; the code did not, and it now
reads the names off the handover.

**And the fourth is that same shape one level up.** The live case required a
simulated artifact to exist, which requires a computation to have run, which
requires the deployment to have been able to prepare one — and on the item's
first live run none could be, for reasons the Master itself recorded in its own
decisions. The
assertion is now the pair described above: the marking where a computation ran,
the disjointness always. Neither change touches a claim about the product; what
changed is that the case no longer supplies the outcome it then asserts on.

**On the run that certified it, the mock did the work and Review threw it out.**
The first live run never got a computation as far as execution. The run that
passed did, and what it left is stronger evidence than the assertion alone:
computations reached `MockComputeBackend`, each produced its pair of artifacts
marked `simulated` with its scenario in `provenance`, and every one of them was
reviewed at FINAL and failed *on the marking*. One review's own words under a
criterion the Master itself had committed — "the delivered files contain no
laboratory data, nothing is formatted or labelled as simulated/mock" — are
`Mock/simulated/inadmissible flag present on both artifacts — explicit failure
condition met` and `Mock CSV, 239 B, hash identical to prior failures;
inadmissible`. The bench leg came back the same way: the person delivered both
names the handover owed, the execution was recorded `COMPLETED` with
`CompletenessVerdict.COMPLETE`, and Review failed it on the bytes it was handed
— `Execution note: 'certification harness stub — not a measurement'; CSV only 84
B`. So the sharpest thing this item certifies is a pair of refusals: nothing
simulated and nothing stubbed was accepted as evidence, by a Review seat that
had not been told the difference. The reading leg, by contrast, passed on ten
sources with retrievable DOIs, fourteen attributed claims and one conflict left
unresolved — which is what makes the refusals mean something, because the same
seat accepted a result on the same run.

**The Master ended it honestly, and its ending report is prose rather than the
record.** The ending is `CONCLUDE_INCONCLUSIVE`, decided against the frozen
success contract: "ONE IS DELIVERED AND ACCEPTED and TWO PRODUCED NOTHING — not
because the questions were answered the other way, but because this deployment
could not execute the work". It names each stop with the record that explains it
— two runs lost to `INFRASTRUCTURE`, refusals of `UNSUPPORTED_ENVIRONMENT` and
`MISSING_SCIENTIFIC_PARAMETER`, a Review that withheld clearance at PRE_RUN —
and it refuses the shortcut that would have produced numbers: supplying RASPA's
adsorption parameters "would have run a gas-uptake simulation and named it
conductivity". Where it is imprecise is that it also says the computation line
never produced output, and the ledger holds the executions and the rejected
artifacts described above. Both readings survive at the level of admissibility —
nothing admissible was produced — but a reader of the ending report alone would
not learn that a mock ran. The database is the authoritative record and the case
asserts on it; `KNOWN_LIMITATIONS.md` L-33 records what the prose costs a
reader.

**The fifth finding was the case's as well, and the matrix is what found it: the
scripted case locked the database behind itself.** It read the project's
research records, executions and criteria inside one `read_only()` block, and
then asked two of those repositories a question *after* the block had closed. A
repository used past its session begins a second transaction on a connection
that was already returned, and nothing ever returns that one: it sits `idle in
transaction` holding a read lock on the table it read. The next test's
`TRUNCATE` waits out its ten-second `lock_timeout` and fails — and the failure
lands on the *next* test rather than on the one that caused it, which is what
the fixture's message says in as many words. Here it landed on the live case,
whose setup errored, and then on the sixteen tests behind it. It is a defect the
case could not see about itself: nothing follows it in a `-k one_project` run,
and `tests/e2e` is a different process, so both of the runs that had exercised
it had ended before the next one began. The matrix runs the module next to the
others, in one process, which is the run that surfaced it — and
`tests/acceptance/test_review.py` documents the hazard in the same words, a
precedent this case had not followed. Nothing guards the class mechanically —
what the harness has is detection *after* the fact: a ten-second `lock_timeout`
that turns the leak into a failure on the next test. Both leaks were closed (the
second was the same mistake, with the executions), and the case now leaves no
connection checked out at all; that was measured with a probe over the engine's
pool that prints the stack of every connection checked out and never returned,
which went from one to zero. So the five failing rows and seventeen failed set-ups of the
matrix run *before* that fix were the leak and not a product regression: they
are the tests downstream of a locked `TRUNCATE`, and each one passes in a
process that starts after it.

| | |
|---|---|
| Acceptance, scripted case | `test_phase11_certification.py -k one_project` — **1 passed** in 2.72s |
| Acceptance, live case | `test_phase11_certification.py -k live` — **1 passed** in 1461.78s (0:24:21), five real agents, real Internet, one ending |
| Acceptance, whole matrix | `make phase11-acceptance` — **11/11 demonstrated, 0 failed, 0 skipped** (49 passed, 1 skipped) in 21m36s, with both certification cases among them: `PASS P11-11 the whole chain, certified end to end`, 2 passed |
| E2E | `tests/e2e` — **27 passed** |
| Unit | **1106 passed** |
| Static analysis | `ruff check src tests scripts` and `pyright src tests` — clean |

Both certification cases ran on the fixed file, and the live one's evidence above
comes from the run before the leak fix — the fix is entirely inside the scripted
case (two reads moved inside their block), so the live case's own code is
byte-for-byte what produced that record.

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
