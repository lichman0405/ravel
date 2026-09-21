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
make lint          # ruff check src tests, then pyright src tests
make test-unit     # tests/unit, no services needed
make test-integration
make test-e2e
make test-live     # real-Internet research; needs RAVEL_RESEARCH_CONTACT_EMAIL
make test-dsh      # real model turns; needs DEEPSEEK_API_KEY
make acceptance    # A01-A20 plus the seven gates, printed as a matrix
```

Two warnings, both of which cost time when ignored:

- **Never run two pytest processes at once on this machine.** They share the
  `ravel_test` database and both `TRUNCATE` on entry, so the second one's
  cleanup blocks on the first one's locks, and the failure surfaces as a
  timeout in a test that did nothing wrong.
- `make test` (`scripts/test_all.sh`) **fails without `DEEPSEEK_API_KEY`** by
  design: it sets `RAVEL_REQUIRE_DSH=1`, because a harness gate that can pass by
  not running is not a gate.

## 2. Results

Run on 2026-09-21 with `DEEPSEEK_API_KEY` and `RAVEL_RESEARCH_CONTACT_EMAIL`
set in `.env`:

| Suite | Passed | Skipped | Failed | Exit |
|---|---:|---:|---:|---|
| `tests/unit` | 803 | 0 | 0 | 0 |
| `tests/integration` | 538 | 0 | 0 | 0 |
| `tests/dsh` | 11 | 0 | 0 | 0 |
| `tests/e2e` | 20 | 0 | 0 | 0 |
| `tests/live_research` | 13 | 0 | 0 | 0 |
| `tests/acceptance` (`make acceptance`) | 43 | 0 | 0 | 0 |
| `tests/acceptance -m phase10` (`make phase10-acceptance`) | 56 | 0 | 0 | 0 |

`make acceptance` reports by *item* rather than by test, which is a different
question from the one a pytest summary answers — see §3. The same is true of
`make phase10-acceptance`, whose 56 cases are the 36 behind the twenty `P10-`
items plus the twenty worker items — see §3.1.

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
  PASS    P10-17  live five-agent autonomous certification    1 passed
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

One row is expensive. `P10-17` is a live, unattended run of a whole project
against real model turns, and on this machine it takes about twenty minutes and
a real bill; it is the only item here that cannot be answered by reading code.
Its most recent run ended `FAILED` in 20:12, with `executed_by` naming both
`compute-worker` and `experimental-worker` and the two backends `mock-compute`
and `mock-lab`. What a live run *cannot* end as, and why, is stated in
`acceptance/PHASE10_ACCEPTANCE.md` under P10-17 and in `KNOWN_LIMITATIONS.md`
L-19 — a COMPLETED ending would require a live Review to accept a `simulated`
artifact as a real measurement, and it does not.

## 4. What used to be skipped, and now runs

With both credentials present, the two previously skipped acceptance items and
their underlying suites now execute against real external services:

- **A03 / A04 and `tests/live_research`** now reach the real Internet. The 13
  cases exercise Crossref, OpenAlex, arXiv, PubChem, and direct URL retrieval.
  The connectivity probe at the start of `scripts/test_live_research.sh` reports
  Crossref and OpenAlex as reachable; arXiv answers the probe with HTTP 400 but
  the actual `arxiv.org/abs/1606.00335` fetch in the tests returns 200 — the
  probe URL (`export.arxiv.org/api/query?max_results=1`) appears to be stricter
  than the path the product uses.
- **`tests/dsh`** now makes real DeepSeek model turns. The 11 cases verify that
  the pinned runtime starts, runs RAVEL tools, refuses cross-role tool access,
  and recovers Master state from PostgreSQL rather than from harness session
  context.

## 5. The seven claims the development contract forbids declaring without evidence

`docs/15_AUTONOMOUS_DEVELOPMENT_CONTRACT.md` lists what a "no false completion"
claim must not rest on. Each is answered by a test rather than by an assurance:

| Claim | What demonstrates it |
|---|---|
| Web research is not mocked | gate 1 asserts every connector is pointed at a real service and that research cannot be answered offline; gate 2 asserts a source nobody read cannot enter the ledger, with no network call involved. **A03, A04 and all 13 cases of `tests/live_research` now pass against real services.** |
| DSH is pinned and tested | gate 4 — the installed distributions match the pin, the bundled runtime self-reports `0.1.5-rc.1`, and the vendored checkout's `HEAD` is the pinned commit |
| DSH role presets and tool scope are verified | gate 5 — every role boots with its own contract and no other role's roster; `tests/integration/roles` holds the permission matrix for all five |
| Master recovery is tested | A15 — the project survives the Master session and is recovered from it |
| Temporal restart and wait recovery are tested | A16 — a waiting task survives the worker being killed; A10 — a waiting lab task is woken by its signal |
| Acceptance freeze is tested | A14, both cases: a frozen criterion cannot be edited in place, and new criteria are a new version naming the decision that authorized them. A08 records a verdict against the criteria as frozen |
| Unauthorized DAG mutation is tested | A19, all three cases: no non-Master role's server registers a DAG-mutating tool, the mutation service refuses a non-Master actor, and no HTTP route lets a member change the DAG |
| The TUI is not static | `tests/e2e/test_tui.py` drives the real console headlessly against a real uvicorn over real HTTP and a real WebSocket; A18 covers the three member-facing surfaces |

## 6. What the tests found

The suite is load-bearing, and the record of what it caught is the evidence for
that. The two earlier findings are kept below; a third was found only when the
model-turn gate could finally run.

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

## 7. What these numbers do not say

- A green suite is not a proof of correctness. It is a record of what was
  exercised, and `KNOWN_LIMITATIONS.md` is the record of what was not.
- Every number above was measured once, on one Ubuntu 24.04 host, with one set
  of credentials. Re-running on a different network, a different DeepSeek
  account, or a different DSH release may surface different behavior.
- The project loop has been observed with a real model in the DSH spike tests,
  and as of Phase 10 it has also been observed as `P10-17`: one unattended run
  of a whole project, twenty minutes, five live seats, ending `FAILED`. That is
  one run, not stress-testing. Repetition, runs of hours rather than minutes,
  and the endings that need a non-mock backend to reach are all outside what has
  been measured. `KNOWN_LIMITATIONS.md` L-19 says so in the same words.
- `40/40 demonstrated` is a claim about the tests that ran, not about the
  science. Every live run of `P10-17` ends `FAILED`, `INCONCLUSIVE` or
  `CANCELLED`, because V0's backends are mocks and a live Review will not accept
  a `simulated` artifact as a measurement; a run reaching an ending says the
  architecture carried the project there, not that the hypothesis was answered.
