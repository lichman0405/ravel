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

On Ubuntu 24.04 with the infrastructure up (`scripts/dev_up.sh`):

```bash
make lint          # ruff check src tests, then pyright src tests
make test-unit     # tests/unit, no services needed
make test-integration
make test-e2e
make test-live     # real-Internet research; skips without a contact address
make acceptance    # A01-A20 plus the seven gates, printed as a matrix
```

Two warnings, both of which cost time when ignored:

- **Never run two pytest processes at once on this machine.** They share the
  `ravel_test` database and both `TRUNCATE` on entry, so the second one's
  cleanup blocks on the first one's locks, and the failure surfaces as a
  timeout in a test that did nothing wrong.
- `make test` (`scripts/test_all.sh`) **fails without `DEEPSEEK_API_KEY`** by
  design: it sets `RAVEL_REQUIRE_DSH=1`, because a harness gate that can pass by
  not running is not a gate. See §4.

## 2. Results

| Suite | Passed | Skipped | Failed | Exit |
|---|---:|---:|---:|---|
| `tests/unit` | 752 | 0 | 0 | 0 |
| `tests/integration` | 537 | 0 | 0 | 0 |
| `tests/dsh` | 3 | 8 | 0 | 0 |
| `tests/e2e` | 20 | 0 | 0 | 0 |
| `tests/live_research` | 0 | 13 | 0 | 0 |
| `tests/acceptance` (`make acceptance`) | 41 | 2 | 0 | 0 |

The `tests/dsh` row is the one to read carefully: it is green because the
model-turn tests *skip*, not because they passed. `scripts/test_all.sh` sets
`RAVEL_REQUIRE_DSH=1`, which turns those skips into failures — see §4.

`tests/live_research` is entirely skipped, and that is the largest gap in this
report — see §4 as well.

`make acceptance` reports by *item* rather than by test, which is a different
question from the one a pytest summary answers — see §3.

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
  SKIP    A03  Real research                 1 skipped
  SKIP    A04  Evidence provenance           1 skipped
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

Skipped, in the suite's own words:
  A03, A04: RAVEL_RESEARCH_CONTACT_EMAIL is unset, and RAVEL will not fetch anonymously. Crossref, OpenAlex and NCBI route identified clients to a faster pool and ask for a contact address; set it in .env to run this suite.
  cover these by setting what they name in .env

27/27 demonstrated, 0 failed, 2 skipped, 0 missing (pytest exit 0)
```

## 4. What is skipped, and why that is reported rather than hidden

Two acceptance items and part of the harness suite do not run on this host.
Neither is folded into the pass count.

- **The entire live-research suite skips without
  `RAVEL_RESEARCH_CONTACT_EMAIL`.** `pytest tests/live_research` reports 13
  skipped and 0 passed on this host, and A03 and A04 skip with them. RAVEL
  fetches real sources and does not fetch anonymously, and a placeholder address
  would defeat the point of asking for one — so the suite stops instead of
  proceeding under a name nobody owns. The matrix prints the two items as `SKIP`
  with pytest's own sentence about what they are waiting for, so the row reads
  as "set this and it runs". This is `KNOWN_LIMITATIONS.md` L-20, and it is the
  largest gap in this build's repeatable evidence: a real fetch against
  Crossref, OpenAlex and arXiv was verified once, when `d647084` rewrote the
  connection pinning, but nothing re-runs it now.
- **The model-turn tests in `tests/dsh` skip without `DEEPSEEK_API_KEY`.**
  `pytest tests/dsh` reports 3 passed and 8 skipped,
  because the tests that need a turn skip rather than substitute — but
  `scripts/test_all.sh` sets `RAVEL_REQUIRE_DSH=1`, which turns that skip into a
  failure. The harness gate cannot pass by not running; it can only fail loudly
  or pass honestly. This is L-15.

## 5. The seven claims the development contract forbids declaring without evidence

`docs/15_AUTONOMOUS_DEVELOPMENT_CONTRACT.md` lists what a "no false completion"
claim must not rest on. Each is answered by a test rather than by an assurance:

| Claim | What demonstrates it |
|---|---|
| Web research is not mocked | gate 1 asserts every connector is pointed at a real service and that research cannot be answered offline; gate 2 asserts a source nobody read cannot enter the ledger, with no network call involved. **The live suite — A03, A04 and all 13 cases of `tests/live_research` — skips on this host** (`RAVEL_RESEARCH_CONTACT_EMAIL` unset), so no real fetch is repeated by a sweep here. See §4 |
| DSH is pinned and tested | gate 4 — the installed distributions match the pin, the bundled runtime self-reports `0.1.5-rc.1`, and the vendored checkout's `HEAD` is the pinned commit |
| DSH role presets and tool scope are verified | gate 5 — every role boots with its own contract and no other role's roster; `tests/integration/roles` holds the permission matrix for all five |
| Master recovery is tested | A15 — the project survives the Master session and is recovered from it |
| Temporal restart and wait recovery are tested | A16 — a waiting task survives the worker being killed; A10 — a waiting lab task is woken by its signal |
| Acceptance freeze is tested | A14, both cases: a frozen criterion cannot be edited in place, and new criteria are a new version naming the decision that authorized them. A08 records a verdict against the criteria as frozen |
| Unauthorized DAG mutation is tested | A19, all three cases: no non-Master role's server registers a DAG-mutating tool, the mutation service refuses a non-Master actor, and no HTTP route lets a member change the DAG |
| The TUI is not static | `tests/e2e/test_tui.py` drives the real console headlessly against a real uvicorn over real HTTP and a real WebSocket; A18 covers the three member-facing surfaces |

## 6. What the tests found

The suite is load-bearing, and the record of what it caught is the evidence for
that. One finding is worth writing down because of how it presented.

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

The path had no test at all before this, which is why it survived. It is a
reminder that "the acceptance items pass" and "the suite is green" are different
claims: the twenty items were green while this was broken.

## 7. What these numbers do not say

- A green suite is not a proof of correctness. It is a record of what was
  exercised, and `KNOWN_LIMITATIONS.md` is the record of what was not.
- The two skips in §4 are real gaps in coverage on this host, not
  formalities. Both are one environment variable away from being closed.
- The project loop has never been observed running against a live model here.
  Its sequencing is asserted end to end with the *policy* scripted, which is
  what those acceptance items are about; a real model in Master's and Review's
  seats is not covered. This is L-19.
