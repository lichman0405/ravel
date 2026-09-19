# DSH Integration Report

How RAVEL V0 uses DeepSeek Harness, what was verified against the pinned
release, and what the pin's own limits forced RAVEL to do differently.

The harness is not one dependency among several. It is the only one, and every
agent turn in RAVEL is a turn on it. That makes the pin, and the record of what
was true at the pin, part of the deliverable rather than a note about the build.

---

## 1. The pin

Recorded in `vendor/DSH_PIN.json`; the patch record is `vendor/DSH_PATCHES.md`.

| | |
|---|---|
| Release | `dsh-v0.1.5-rc.1` |
| Commit | `183f08e9c6dde7e36cd2318eaee70b0da08fb35e` (2026-09-10) |
| Python SDK | `deepseek-harness-sdk==0.1.5rc1` |
| Runtime | `deepseek-harness-runtime-bin==0.1.5rc1` |
| Platform wheel | `manylinux_2_28_x86_64`, Python ≥ 3.10 |
| Licence | MIT |
| Upstream stability | Developer Preview — compatibility-breaking changes expected |

**No patch to harness source is required, and none was made.** The vendored
checkout under `vendor/deepseek-harness` is a read-only copy of the pinned
commit, is git-ignored, and is not needed to run or to test RAVEL: the two
wheels come from PyPI and are a matched pair, and `bootstrap_ubuntu.sh` fails on
any drift from the versions above.

## 2. What was verified at the pin

Five checks, run on the canonical platform (Ubuntu 24.04.5 LTS x86_64, Python
3.12.7) and recorded with their commands in `vendor/DSH_PIN.json`:

| Check | Result |
|---|---|
| The wheels install and both packages import | PASS |
| The bundled runtime self-reports the pinned version (`dsh --version`) | PASS — `0.1.5-rc.1` |
| A real end-to-end turn: real model, real tool execution | PASS — the agent used its bash tool to create a file, verified on disk |
| Session log durability | PASS — `$DSH_HOME/sessions/--<workspace>--/<session>/session.v3.jsonl`, append-only JSONL |
| Cross-process session resume over the SDK | **FAIL — reproduced, and designed around** |

The last one is not a defect being reported; it is a property of the pinned
release that RAVEL's recovery design has to respect, and §4 is about it.

`make acceptance` re-checks the pin rather than quoting these results: gate 4
asserts the installed distributions are the pinned ones, that the bundled
runtime's self-reported version matches the pin, and — when the vendored
checkout is present — that its `HEAD` is the pinned commit. Gate 5 asserts every
role boots with its own contract and no other role's roster. Gate 6 asserts
there is no direct public harness endpoint.

## 3. How RAVEL drives the harness

### Composition: one generated overlay per role

`ravel.dsh.composition` generates the overlay a role runtime boots with, over
the shipped `sdk-minimal` profile. It contains exactly three decisions:

1. the role's behavioral contract (`prompts/<role>_role.md`) becomes the system
   prompt,
2. the shell is removed from the model's view,
3. one MCP server is mounted, serving only that role's tools.

The overlay is *generated* rather than templated because the harness evaluates
`!!js` expressions in bundle layers but not in `--patch` overlays. Every value is
written literally, which has a useful side effect: the authority a runtime was
launched with is a file on disk that can be read and diffed after the fact.

Only shipped extension mechanisms are used — profiles, patch layers, bundles,
agent presets, and MCP server rows. `vendor/DSH_PATCHES.md` records the priority
order and where each mechanism was used.

### Tools: MCP over stdio, not the SDK channel

The pinned SDK wire protocol declares exactly three client-to-server requests —
`initialize`, `session/prompt`, `shutdown` — with no tool-registration surface in
either direction, and the protocol README states that server-to-client requests
are a dormant capability at this release. An external process therefore cannot
host tools for an agent over the SDK channel.

RAVEL exposes its tools as **MCP servers over stdio** through
`@deepseek-ai/dsh-mcp-client`. That row is configuration-only, so no TypeScript
is written and all tool logic stays in Python. Each role's server registers only
that role's tools, and this is enforced twice: by the roster in
`ravel.mcp.registry` and by the MCP server itself checking the scope it was
launched under. A tool a role does not hold is not merely refused — it is not
registered, so the model cannot see it.

### Sessions: one profile per role, because per-session presets are unreachable

The SDK server composes sessions without joining an agent preset, so per-session
preset selection is not reachable at this pin. Tool scope is therefore enforced
by composing one DSH profile per agent role. This is a documented deviation —
see `docs/IMPLEMENTATION_DEVIATIONS.md` — and it is a narrower mechanism than
per-session presets: the scope is fixed when the runtime starts, which is also
why the brief describing a runtime's authority is written once and never
rewritten under a live agent.

### Runtime lifecycle

`DshRuntimePool` owns runtimes, one per `(project, role)` scope. A runtime is
started on first use with a generated `brief.json` describing the authority it
was launched under. Closing a scope closes the runtime *and* drops the session
bindings that named it, because a binding to a dead session is a falsified
record. `RoleSession` mints a fresh session id whenever the runtime changes, so a
reaped runtime cannot be mistaken for a live one.

## 4. The pin's limits, and what RAVEL does instead

### A session cannot be reopened across a runtime restart

The pinned SDK server exposes only `initialize` / `session/prompt` / `shutdown`
and creates a session implicitly; it has no `session/open` or `session/resume`.
A persisted session id collides on a fresh runtime process. An official resume
exists one layer down (`AgentFactory.resume`) but is not reachable over the SDK
wire protocol at this pin.

**Consequence for RAVEL.** Recovery never replays chat context. PostgreSQL
Project State plus the latest `MasterCheckpoint` is the source of truth, and a
replacement Master is built under the same Master identity with a fresh session
id. This is what `docs/07` §8 already prescribed, and it means the harness
holding no durable memory is not a risk RAVEL carries: nothing RAVEL needs is
only in a session. A15 asserts exactly this — a Master scope is closed, the
checkpoint and project state are read back by a process that never saw the dead
session, field for field, and the project then continues to an ending.

### There is no direct public harness endpoint

The harness is reached only through RAVEL — never served, never exposed. Gate 6
asserts this structurally: the Gateway's route table holds no harness route
except the health projection, no route module opens a harness session of its
own, and the MCP transport RAVEL launches is stdio, which has no listening
socket to reach.

## 5. What is still not verified

Stated plainly, because a report that lists only what passed is a report nobody
can plan around:

- **The DSH test suite does not run on this host.** `DEEPSEEK_API_KEY` is empty
  in this working tree. `pytest tests/dsh` reports 3 passed / 8 skipped; the
  tests that need a model turn skip rather than substitute, and
  `scripts/test_all.sh` sets `RAVEL_REQUIRE_DSH=1` so the gate fails rather than
  passes by not running. The Phase 0 evidence in §2 stands as the record of what
  was verified when a credential was available. See `KNOWN_LIMITATIONS.md` L-15.
- **The project loop has not been observed running against a live model on this
  host**, for the same reason. Its composition — `ProjectLoop` with
  `HarnessAgent` in Master's and Review's seats — is exercised end to end by the
  acceptance suite with the *policy* scripted, which is what those items are
  about; what no test here covers is a real model making those decisions.

Neither is a substitute for the other, and neither is claimed to be.
