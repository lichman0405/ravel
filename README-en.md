# RAVEL V0 — Autonomous Scientific Research Runtime

**[中文](README.md) | English**

**RAVEL = Research Autonomous Validation & Execution Loop**

RAVEL V0 is an autonomous scientific research runtime that runs on a single Ubuntu 24.04 CVM. It takes natural-language research or industry needs, formalizes them into scientific questions, performs real web/API/database research, builds a dynamic Scientific DAG, schedules computation and experiment execution, reviews results independently, and lets Master continuously replan under evidence constraints until the Project reaches `SUCCESS`, `FAILED`, `INCONCLUSIVE`, or `TERMINATED`.

**RAVEL is not a code-development agent.** Code, shell, and HPC scripts are only means of scientific execution.

The only underlying harness is **DeepSeek Harness (DSH)**, pinned at `dsh-v0.1.5-rc.1` (see `vendor/DSH_PIN.json`).

---

## Table of Contents

- [Project Status](#project-status)
- [System Requirements](#system-requirements)
- [Installation and Initialization](#installation-and-initialization)
- [Starting and Deploying](#starting-and-deploying)
- [Daily Use](#daily-use)
- [Running Tests](#running-tests)
- [CI / GitHub Actions](#ci--github-actions)
- [Security and Known Limitations](#security-and-known-limitations)
- [Directory Structure](#directory-structure)
- [Document Index](#document-index)
- [FAQ / Troubleshooting](#faq--troubleshooting)

---

## Project Status

**V0 implementation is complete and fully accepted: 27/27 items passed, 0 skipped.**

After filling in `DEEPSEEK_API_KEY` and `RAVEL_RESEARCH_CONTACT_EMAIL` in `.env`, this machine produced:

| Verification | Result |
|---|---|
| `make acceptance` (A01–A20 + 7 extra gates) | 43 passed, 0 skipped |
| Real model in Master/Review seats (`tests/dsh`) | 11 passed, 0 skipped |
| Real literature / web retrieval (`tests/live_research`) | 13 passed, 0 skipped |
| Structural verification (unit / integration / e2e) | 752 / 537 / 20 passed |

The two things that distinguish RAVEL from an ordinary orchestrator — **a model that really makes scientific decisions** and **real reading of primary sources** — have been verified on this machine. See `TEST_REPORT.md` for details.

**However, V0 is not yet a production-ready artifact for public deployment.** `SECURITY_NOTES.md` and `KNOWN_LIMITATIONS.md` record gaps that are not yet fixed (e.g., L-08 DNS rebinding, L-14 browser path). Please read them before deciding where to expose RAVEL. By default all ports bind to `127.0.0.1`.

> **The only remaining optional item:** the DSH spike has already been run with a real model, but a long-running autonomous stress test in which every Master replanning decision and every Review verdict is driven by a live model turn until the project ends has not been run specifically. This is the remaining part of `KNOWN_LIMITATIONS.md` L-19 and does not affect V0 functional completeness.

---

## System Requirements

The **only standard environment** for RAVEL V0 is:

| Item | Requirement |
|---|---|
| OS | Ubuntu 24.04 LTS x86_64 |
| Python | 3.12 (`python3.12-venv` required) |
| Containers | Docker Engine + Docker Compose v2 |
| Network | Reachable DeepSeek API, Crossref/OpenAlex/arXiv, etc. |
| Model credential | `DEEPSEEK_API_KEY` (required for Master/Review) |

The DSH runtime is a `manylinux_2_28_x86_64` wheel, so non-x86_64 platforms will fail at the first agent turn. Windows and macOS are not canonical V0 development or deployment environments.

---

## Installation and Initialization

### 1. Clone the repository

```bash
git clone <this repository> ravel && cd ravel
```

### 2. One-command environment bootstrap

```bash
scripts/bootstrap_ubuntu.sh
# or: make bootstrap
```

`bootstrap_ubuntu.sh` is idempotent and will:

- Check Ubuntu 24.04 x86_64;
- Install/verify baseline apt packages;
- Create `.venv` and install project dependencies (prefers `uv`, falls back to `pip`);
- Verify the pinned DSH versions (`deepseek-harness-sdk==0.1.5rc1`, `deepseek-harness-runtime-bin==0.1.5rc1`);
- Install Playwright Chromium (warns if system libraries are missing; see `KNOWN_LIMITATIONS.md` L-06);
- Check the Docker daemon;
- If `.env` does not exist, copy it from `.env.example` with mode `600`.

To skip system packages when you have no sudo:

```bash
scripts/bootstrap_ubuntu.sh --no-apt
```

### 3. Configure `.env`

```bash
cp .env.example .env
$EDITOR .env
```

`.env` is git-ignored and is the only place secrets belong. Key variables:

| Variable | Default | Description |
|---|---|---|
| `DEEPSEEK_API_KEY` | empty | **Required for Master/Review.** Get one from https://platform.deepseek.com/. |
| `RAVEL_POSTGRES_PASSWORD` | `ravel_dev_password` | Change if externally reachable. |
| `RAVEL_S3_ACCESS_KEY` / `RAVEL_S3_SECRET_KEY` | dev values | MinIO credentials. |
| `RAVEL_GATEWAY_JWT_SECRET` | `dev-only-change-me` | **Change this.** A known signing key means forgeable sessions. |
| `RAVEL_RESEARCH_CONTACT_EMAIL` | empty | Optional but recommended. Crossref/OpenAlex/NCBI use it to identify clients and route to a faster pool; without it `tests/live_research` and A03/A04 skip. |

Use a real email address you own for `RAVEL_RESEARCH_CONTACT_EMAIL`; a placeholder defeats the purpose of the services asking for it. No search API key is needed for real research — RAVEL accesses public sources directly and never falls back to mock search.

---

## Starting and Deploying

### Start V0 with one command

```bash
make up
```

This is `scripts/run_v0.sh`. It will:

1. Start PostgreSQL, Temporal, and MinIO and wait for health checks;
2. Apply database migrations to head;
3. Start the Gateway (HTTP/WebSocket);
4. Start the execution worker;
5. Start the Project Supervisor (discovers and drives every active Project);
6. Open the local Textual TUI.

Quitting the TUI (`q` or `Ctrl-C`) stops the Gateway, worker, supervisor, and loop started by this script, but leaves the containers running. Stop containers with `make dev-down`.

If `DEEPSEEK_API_KEY` is empty, services still start, but Master and Review cannot take any turn, so a Project will not plan or review — this is by design, not a degraded mode.

### Common variants

```bash
# No TUI, run as a background-like server
scripts/run_v0.sh --no-tui

# Start the unattended Project Supervisor (no project argument)
.venv/bin/python scripts/run_supervisor.py

# Also drive a specific project on startup (no supervisor)
scripts/run_v0.sh --project <project_id>

# Start individual components
make gateway     # Gateway with hot-reload
make worker      # Temporal execution worker
make supervisor  # Unattended Project Supervisor
make tui         # TUI client only (requires Gateway already running)
```

### Services and ports (all bound to 127.0.0.1)

| Service | Port | Description |
|---|---|---|
| Gateway | 8000 | HTTP + WebSocket, the only user entry point |
| PostgreSQL | 55432 | Authoritative Project State |
| Temporal frontend | 7233 | Durable execution |
| Temporal UI | 8088 | Observe workflow runs |
| MinIO S3 API | 9100 | Artifact byte storage |
| MinIO console | 9101 | Object storage management UI |

**These ports are deliberately bound to loopback only.** Temporal and MinIO APIs are unauthenticated at the transport layer, and the development credentials live in the repo, so publishing them on `0.0.0.0` would hand the project database to anything that can reach the host. To access from another machine, use an SSH tunnel:

```bash
ssh -N -L 8088:127.0.0.1:8088 -L 9101:127.0.0.1:9101 user@cvm
```

### Stopping

```bash
make dev-down                 # Stop containers, keep data
scripts/dev_down.sh -v        # Stop containers AND delete data (asks for confirmation)
```

`-v` also removes `runtime/dsh_home`, `runtime/workspaces`, and `runtime/snapshots`, so that harness state does not outlive the database describing it.

---

## Daily Use

### Create the first account and Project

The Gateway can authenticate users but cannot create them. Membership is the only thing that manufactures authority, so provisioning must happen at a terminal on the host:

```bash
# Create an Owner and a new Project at the same time
make account ARGS="--username ada --new-project \
  --title 'Catalyst screen' \
  --objective 'Find a dopant that raises conductivity by 15%.'"

# Or call the script directly
.venv/bin/python scripts/create_account.py --username ada \
  --new-project --title "Catalyst screen" \
  --objective "Find a dopant that raises conductivity by 15%."
```

The password is prompted and confirmed at the terminal, then hashed with Argon2id; it is never printed or stored. The `--password` flag exists for scripting but puts the password in shell history.

### Add other members to the same Project

```bash
make account ARGS="--username bench --project <project_id> \
  --role LAB_USER --granted-by ada"
```

Only a member already holding at least the target authority can grant it. The **first** membership of a project may omit `--granted-by`.

### Drive a Project

V0 now defaults to driving every active Project through the unattended **Project Supervisor**:

```bash
make supervisor
# or
.venv/bin/python scripts/run_supervisor.py
```

The Supervisor will:

- Poll PostgreSQL periodically and discover every Project that is not ended and not paused;
- Create DSH sessions for Master, Review, Compute Worker, and Experimental Worker for each Project;
- Enter the loop: start runs, wait for results, and call the right Agent when a decision, review, or Worker communication is needed;
- Continue until the Project ends or stalls for multiple rounds.

To drive a single Project manually (for debugging or CI):

```bash
make project PROJECT=<project_id>
# or
.venv/bin/python scripts/run_project.py --project <project_id>
```

Common parameters:

```bash
.venv/bin/python scripts/run_project.py --project <project_id> \
  --max-rounds 200 --poll-seconds 0.5
```

### Using the TUI

`make up` opens the TUI by default. To start it separately:

```bash
make tui
```

After logging in, a Project Owner / Research User can:

- Select or create a project;
- Chat with Master in natural language;
- View current focus, DAG, decisions, reviews, evidence;
- Handle approvals, pause/resume;
- Adjust the Authority Envelope;
- Upload user artifacts.

A Lab User sees only assigned experiment tasks. An Admin sees DSH/Temporal/Runtime health and does not make scientific decisions.

### Worker backend scenarios

V0 compute and lab are mocks; their behavior is controlled by scenarios:

```bash
make worker ARGS="--compute-scenario COMPUTE_SCIENTIFIC_FAILURE --lab-scenario LAB_DEVIATION_PRESSURE"
```

Available scenarios are listed at the top of `scripts/run_temporal_worker.py`, or run:

```bash
.venv/bin/python scripts/run_temporal_worker.py --help
```

Defaults are `COMPUTE_SUCCESS` / `LAB_SUCCESS`. Every artifact produced by a mock is marked as simulated and cannot be used as real evidence in the Evidence Ledger.

---

## Running Tests

### Environment check

```bash
make env-check
```

Phase -1 gate: checks the platform, settings, PostgreSQL, MinIO, Temporal, and the pinned DSH distributions.

### Common test commands

```bash
make lint             # ruff check + pyright (lint is the code gate)
make typecheck        # pyright only
make test-unit        # No services required
make test-integration # Requires make dev-up first
make test-e2e         # Headless loop + TUI
make test-dsh         # Phase 0 harness gate, requires DEEPSEEK_API_KEY
make test-live        # Real network research, requires RAVEL_RESEARCH_CONTACT_EMAIL
make acceptance       # A01–A20 + 7 extra gates, prints pass/fail matrix
```

`make lint` uses `pyright`, which is a Node tool and not in the venv. If it is missing:

```bash
npm install -g pyright
```

`make acceptance` is the final V0 gate: it not only reports how many tests passed, but also prints a matrix by A01–A20 item, and marks an item with no test as a failure rather than blank.

### Full default test chain

```bash
make test
# Equivalent to scripts/test_all.sh: lint → unit → integration → DSH gate
```

**Notes:**

- **Never run two pytest processes concurrently on the same machine** — they share `ravel_test` and each TRUNCATES on entry.
- **`make test` fails without `DEEPSEEK_API_KEY`** (at the DSH gate) by design: that gate must not pass by not running. Every other suite passes without it.

---

## CI / GitHub Actions

The repository includes `.github/workflows/ci.yml`.

- On every push/PR to `main`: runs `lint`, `unit`, `integration`, `e2e`, and `acceptance`.
- Only on `main` push and only when the corresponding secret exists: runs `make test-dsh` and `make test-live`.

This way PRs do not consume API credits; real model / real network verification happens after merge.

To run the full suite in CI, add these Repository secrets under **Settings → Secrets and variables → Actions**:

- `DEEPSEEK_API_KEY`
- `RAVEL_RESEARCH_CONTACT_EMAIL`

---

## Security and Known Limitations

Before putting RAVEL in any environment reachable by others, read in full:

- `SECURITY_NOTES.md` — security posture and open risks.
- `KNOWN_LIMITATIONS.md` — known limitations, with explicit notes on what not to infer from each.

Key reminders:

- All service ports bind to `127.0.0.1` by default; do not change them to `0.0.0.0`.
- `.env` is git-ignored; never commit it.
- V0 compute/lab are mocks; research is real — mock web evidence is never allowed.
- There is no user-level deactivation/revocation path; once authority is granted it lasts until the project ends.
- V0 has no scheduler; the loop is a process, not a service.

---

## Directory Structure

```text
src/ravel/                 # Runtime
  domain/                  # Domain records, closed enums, state machines
  state/                   # PostgreSQL tables, constraints, repositories, migrations
  dag/                     # Scientific DAG service and Master's mutation tools
  research/                # Real-source connectors, fetching, evidence, sufficiency
  execution/               # Loop, node runs, Temporal workflows/activities
  backends/                # Mock compute / mock lab
  master/                  # Master decisions and ending
  review/                  # Review checkpoints and verdict application
  mcp/                     # Tool registry and five per-role MCP servers
  dsh/                     # Harness pool, composition, role bindings
  gateway/                 # Auth, permissions, REST + WebSocket
  tui/                     # Textual console client

tests/
  unit/                    # In-memory tests
  integration/             # Requires PostgreSQL/Temporal/MinIO
  dsh/                     # Real DSH runtime + real model turn
  e2e/                     # Headless loop + TUI
  live_research/           # Real network research, never mocked
  acceptance/              # A01–A20 and 7 extra gates

scripts/                   # Bootstrap, start/stop, accounts, matrix, worker/loop entrypoints
infra/postgres/init/       # Initial databases needed by Temporal
docker-compose.yml         # PostgreSQL + Temporal + MinIO
prompts/                   # Five agent role presets (read at runtime)
schemas/                   # Machine-readable schemas / contracts (read at runtime)
acceptance/MOCK_SCENARIOS.yaml  # Mock scenario catalogue (read at runtime)
docs/                      # Product and technical specifications
```

---

## Document Index

| File | Content |
|---|---|
| `START_PROMPT.md` | Initial prompt used when the repository was created (archive) |
| `CLAUDE.md` | Mandatory rules for development agents |
| `IMPLEMENTATION_REPORT.md` | Implementation report, phase by phase with gates |
| `TEST_REPORT.md` | Test report and real numbers |
| `KNOWN_LIMITATIONS.md` | Known limitations |
| `SECURITY_NOTES.md` | Security posture and open risks |
| `DSH_INTEGRATION_REPORT.md` | DSH integration and pin verification record |
| `DEPLOYMENT.md` | Complete deployment, configuration, run, and test instructions |
| `docs/IMPLEMENTATION_DEVIATIONS.md` | Records where implementation deviates from spec |
| `docs/00_PRODUCT_AND_SCOPE.md` | Highest priority Source of Truth |
| `docs/01_ARCHITECTURE.md` | Architecture |
| `docs/02_AGENT_MODEL.md` | Agent model and five roles |
| `docs/03_SCIENTIFIC_DAG.md` | Scientific DAG |
| `docs/04_STATE_AND_DATA.md` | State and data |
| `docs/05_RESEARCH_AND_EVIDENCE.md` | Research and evidence |
| `docs/06_EXECUTION_AND_REVIEW.md` | Execution and review |
| `docs/07_DSH_INTEGRATION.md` | DSH integration |
| `docs/08_GATEWAY_AND_TUI.md` | Gateway and TUI |
| `docs/09_SECURITY_AND_IDENTITY.md` | Security and identity |
| `docs/10_IMPLEMENTATION_PLAN.md` | Implementation plan |
| `docs/14_DEVELOPMENT_ENVIRONMENT.md` | Standard development environment |
| `docs/15_AUTONOMOUS_DEVELOPMENT_CONTRACT.md` | Autonomous development contract |

---

## FAQ / Troubleshooting

**Q: `make up` says `.env missing`.**  
A: Run `cp .env.example .env` and fill in at least `DEEPSEEK_API_KEY` and `RAVEL_GATEWAY_JWT_SECRET`.

**Q: `make lint` reports `pyright: command not found`.**  
A: `npm install -g pyright`. Bootstrap does not install Node/pyright.

**Q: `make test-dsh` skips everything or fails.**  
A: Check that `.env` contains a non-empty `DEEPSEEK_API_KEY`. `make test` is designed to fail here if the key is missing.

**Q: `make test-live` skips everything.**  
A: Set `RAVEL_RESEARCH_CONTACT_EMAIL` to an address you actually own.

**Q: Two test processes running at the same time give unstable results.**  
A: Do not run pytest concurrently. They share `ravel_test` and each TRUNCATES on entry.

**Q: Browser research tests fail / Chromium won't start.**  
A: See `KNOWN_LIMITATIONS.md` L-06. On a machine missing system libraries, run:

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

**Q: I want to access Temporal UI / MinIO console from another machine.**  
A: Use an SSH tunnel; do not change the port bindings in `docker-compose.yml`.

---

License: MIT — see `LICENSE`.
