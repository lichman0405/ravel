# Implementation Report

What RAVEL V0 is made of, phase by phase, and what each phase was accepted
against. Written from the record rather than from the design: every gate below
is a command in the repository, and every claim is one the acceptance run
re-checks.

Started from an empty repository on 2026-09-19. 43 commits, 141 source modules
and 91 test modules later, `make acceptance` runs the twenty V0 items and the
seven extra gates and prints the matrix.

---

## The shape of it

RAVEL is an autonomous scientific research runtime. It takes a research need in
natural language, formalises it into a scientific question, performs real
web/API/database research, builds a dynamic Scientific DAG, schedules computation
and experiments, reviews results independently, and replans under evidence
constraints until the project reaches an ending.

The load-bearing decision is that **authority is separated three ways** and the
separation is enforced in software rather than asked for in a prompt:

| Power | Holder | Enforced by |
|---|---|---|
| Decision | Master | The only role whose MCP server registers a DAG-mutating tool |
| Review | Review | Holds `submit_review`; holds no tool that changes the plan |
| Execution | Workers | Act only under a frozen Execution Contract; hold no writing tool but `request_action` and `send_message` |

A user may pause, resume, approve, and set the Authority Envelope. A user may
not edit the DAG — not because the UI hides it, but because there is no route,
no tool, and no repository path that lets them.

## Module map

```
src/ravel/
  domain/      the records, the closed vocabularies, and the state machines
  state/       PostgreSQL: tables, guards, repositories, migrations
  dag/         handled by state/services + mcp/tools/dag.py
  research/    connectors, fetching, addressing, browser, evidence, sufficiency
  execution/   the loop, node runs, Temporal workflows and activities, backends
  backends/    the mock compute and laboratory, driven by a scenario catalogue
  review/      the checkpoints and the service that applies a verdict
  master/      what Master alone may decide, and the ending
  mcp/         the tool registry and the per-role servers
  dsh/         the harness: pool, composition, roles, agents
  gateway/     authentication, permissions, REST + WebSocket, the Master conversation
  tui/         the console
```

## Phase by phase

### Phase -1 — Ubuntu environment bootstrap

`scripts/bootstrap_ubuntu.sh`, `scripts/env_check.py`. Canonical platform
(Ubuntu 24.04 LTS x86_64, Python 3.12), the Docker stack, and an environment
gate that checks every dependency is usable rather than merely installed.

**Gate** `make env-check` passes.

### Phase 0 — DSH integration spike

The harness was fetched at its then-current release and pinned at
`dsh-v0.1.5-rc.1` / `183f08e9c6dde7e36cd2318eaee70b0da08fb35e`, with the
verification recorded in `vendor/DSH_PIN.json` and no patch required
(`vendor/DSH_PATCHES.md`). Five roles, each with a generated composition overlay
carrying its own prompt and only its own tools.

**Gate** `tests/dsh/test_spike.py` against a real runtime — see
`DSH_INTEGRATION_REPORT.md`, including the one check that failed at the pin and
what RAVEL does instead.

### Phase 1 — Core domain and persistence

Typed domain records with closed vocabularies and explicit state machines; a
PostgreSQL schema with the invariants pushed into the database — append-only
guards, check constraints, and trigger-enforced transitions — and 13 migrations
from an empty database to head.

The principle is the one in `CLAUDE.md`: *database constraints over "the model
should remember"*. A node cannot reach a status its state machine does not
allow, and the database refuses it even if the code is wrong.

**Gate** `tests/unit/domain`, `tests/integration/state`.

### Phase 2 — Scientific DAG

Typed nodes and edges, an 11-state lifecycle, rolling-horizon expansion, and
exactly one path by which the graph changes: Master's authorized mutations. A
dependency, once recorded, cannot be edited or deleted — changing the graph
means opening new work.

**Gate** `tests/unit/dag`, `tests/integration/dag`.

### Phase 3 — DSH role profiles and tools

Five per-role MCP servers over stdio, each registering only that role's tools,
with the roster enforced in two places: the registry that decides what is
mounted, and the server that checks the scope it was launched under.

**Gate** `tests/integration/roles` — the permission matrix for all five roles,
including that no non-Master role's server registers a DAG-mutating tool.

### Phase 4 — Real research source gateway

Real retrieval from Crossref, OpenAlex, arXiv, PubChem and publisher pages.
Address policy runs before every connection; the fetcher connects to the address
it validated rather than to the name, which closes the DNS-rebinding race. A
source is registered only after it has actually been read, and a lead is not
evidence.

**Gate** `tests/live_research/` — written against the real Internet and never
mocked. On the machine this was built on it reports 13 skipped, because it
refuses to fetch anonymously and no contact address was supplied; gate 1 and
gate 2 cover the structural half without a network call. See
`KNOWN_LIMITATIONS.md` L-20.

### Phase 5 — Temporal durable execution

Workflows and activities, long waits with timers, external signals, retry and
timeout policy. Temporal is durable execution and *only* that: the scientific
record is in PostgreSQL, and the two are never confused.

**Gate** `tests/integration/temporal` — kill the worker mid-activity, restart,
observe the run complete.

### Phase 6 — Mock execution backends

`WorkBackend` as a port, with mock compute and laboratory implementations driven
by the acceptance catalogue's named scenarios. `submit` is required to be
idempotent in `(project, node, attempt)`, which is what makes crash recovery
correct rather than lucky.

**Gate** `tests/integration/backends` — each scenario produces exactly its
specified transitions and artifacts.

### Phase 7 — Review and the closed loop

Pre-flight, runtime and final checkpoints; PASS / FAIL / PARTIAL against
criteria frozen *before* the node ran; Master's replanning decisions; deviation
escalation and resolution; and the loop that sequences it all.

**Gate** `tests/e2e/test_headless_loop.py` — a full project from creation to a
terminal status.

### Phase 8 — Gateway and console

Argon2id authentication with refresh-token rotation that revokes the chain on
replay; project-scoped authorization; REST and WebSocket; artifact upload;
owner/lab/admin screens; and the one route a person has to Master.

**Gate** `tests/e2e/test_tui.py`, driving the real console headlessly against a
real uvicorn over real HTTP and a real WebSocket.

### Phase 9 — Resilience and V0 acceptance

`tests/acceptance/` — one module per group of items, case names mapped to item
names; and `tests/acceptance/test_gates.py`, the seven extra gates.

**Gate** `make acceptance`, which runs the suite and prints the A01–A20 and gate
matrix. A pytest summary says how many tests passed, not which items they were
about, and says nothing about an item nobody wrote a test for — the matrix says
that, and calls an item with no case at all a failure rather than a blank.

## What was deliberately not built

- **No new agent types.** Every deterministic responsibility is software. The
  five roles are fixed, and the loop, the scheduler, the guards and the
  state machines are all ordinary code.
- **No Web Dashboard.** The local Textual console is the interaction surface.
- **No POST integration.** POST is a separate future project and V0 does not
  depend on it.
- **No Kubernetes, Neo4j, Kafka, or Redis.** One CVM, one PostgreSQL, one
  Temporal, one MinIO.
- **No artifact bytes in PostgreSQL.** Metadata is a row; the bytes are in
  S3-compatible storage, and a read is authorized on the way out.
- **No mock web evidence.** Compute and the laboratory are mocked and labelled;
  research never is.

## Where the deviations are

`docs/IMPLEMENTATION_DEVIATIONS.md` records each place the implementation
departs from the specification, with the reason and the consequence. The
security posture and its open risks are in `SECURITY_NOTES.md`; the limits are
in `KNOWN_LIMITATIONS.md`, written as each was found rather than reconstructed at
the end.
