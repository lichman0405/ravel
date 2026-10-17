# Deploying RAVEL V0

RAVEL V0 runs on one Ubuntu 24.04 LTS x86_64 machine. That single machine runs
one DeepSeek Harness host and carries any number of projects; nothing here is
distributed, and nothing here is a cluster.

The commands below are the whole of it. `make up` is the one that starts the
system; everything before it is done once, and everything after it is how you
look at what it is doing.

---

## 1. What the machine needs

| | |
|---|---|
| OS | Ubuntu 24.04 LTS, x86_64 |
| Python | 3.12, with `python3.12-venv` |
| Containers | Docker Engine, with the `compose` v2 plugin, and a daemon the operator can reach |
| Model credential | A DeepSeek API key — see §4. Without it the system starts but no agent can think. |
| Disk | PostgreSQL and MinIO volumes, plus `runtime/` for harness homes, workspaces and snapshots |

`scripts/bootstrap_ubuntu.sh` checks each of these and installs what it can. It
refuses anything but Ubuntu and anything but x86_64, because the harness runtime
is shipped as a `manylinux_2_28_x86_64` wheel and a deployment that installed on
another platform would fail at the first agent turn rather than at install.

## 2. Bootstrap

```bash
git clone <this repository> ravel && cd ravel
scripts/bootstrap_ubuntu.sh          # or: make bootstrap
```

It is idempotent and safe to re-run. In order it: checks the platform, installs
the baseline apt packages it is missing, requires Python 3.12, creates `.venv`,
installs the project editable with its dev extras, **verifies the pinned harness
versions** (`deepseek-harness-sdk==0.1.5rc1` and
`deepseek-harness-runtime-bin==0.1.5rc1`, failing on drift from
`vendor/DSH_PIN.json`), installs a Chromium for browser research, requires a
working Docker daemon, and creates `.env` from `.env.example` at mode 600 if it
does not exist.

The Chromium step is the one that reports two different things, because it
installs two different things. `playwright install` fetches the browser into the
venv; `--with-deps` apt-installs the shared libraries it links against and needs
sudo. A browser without those libraries downloads fine and cannot launch, so a
host where the second half failed prints a warning saying so rather than
claiming success — see `KNOWN_LIMITATIONS.md` L-06 for the state of the machine
this was built on, where that is exactly what happened.

The harness is installed from PyPI. The `vendor/deepseek-harness` checkout is a
read-only copy of the pinned source, is git-ignored, and is **not** needed to
run or to test — a fresh clone that has never had it still passes the suite.

`bootstrap_ubuntu.sh --no-apt` skips the system packages when they are already
present and you have no sudo.

## 3. Configure

```bash
$EDITOR .env
```

`.env` is git-ignored and is the only place a secret belongs. The five values
that matter:

| Variable | Default | Notes |
|---|---|---|
| `DEEPSEEK_API_KEY` | *(empty)* | **Required for any agent turn.** |
| `RAVEL_POSTGRES_PASSWORD` | `ravel_dev_password` | Change for anything reachable by others. |
| `RAVEL_S3_ACCESS_KEY` / `RAVEL_S3_SECRET_KEY` | dev values | MinIO credentials. |
| `RAVEL_GATEWAY_JWT_SECRET` | `dev-only-change-me` | **Change it.** A known signing key is a forgeable session. |
| `RAVEL_RESEARCH_CONTACT_EMAIL` | *(empty)* | Optional; see below. |

`RAVEL_RESEARCH_CONTACT_EMAIL` is what RAVEL identifies itself with when it
fetches from Crossref, OpenAlex and NCBI, all of which route identified clients
to a faster pool and ask for a contact address. RAVEL will not fetch
anonymously, so **with this unset the whole live-research suite skips** — all
thirteen cases in `tests/live_research`, and acceptance items A03 and A04 with
them. Nothing is mocked in their place; the acceptance matrix prints them as
skipped and says why. Set it to an address you actually own: a placeholder
defeats the reason the services ask.

There is no search-API key to provide. RAVEL reaches real sources directly and
never falls back to a synthetic search — see `KNOWN_LIMITATIONS.md` L-05.

## 4. Start

```bash
make up
```

That is `scripts/run_v0.sh`, and it: starts the containers and waits for real
readiness, applies migrations to head, runs the Gateway, runs the execution
worker, runs the supervisor, and opens the console. **Closing the console does
not stop RAVEL** — that is the point of the supervisor, and leaving the console
is what a person does after creating a project. Ctrl-C stops the four processes
the script started; the containers are left running, and `make dev-down` stops
those.

Variants:

```bash
scripts/run_v0.sh --no-tui              # a server with no console attached
scripts/run_v0.sh --project <id>        # drive only that project, by hand
make gateway                            # the Gateway alone, with reload
make worker                             # the execution worker alone
make tui                                # the console alone (needs the Gateway)
```

The three services, and what each is for:

- **Gateway** — the only HTTP surface. Serves the console, authenticates people,
  enforces project membership, and holds the one route a person has to Master.
  It creates no DSH runtime until somebody needs one. Start it through
  `scripts/run_gateway.py`: `uvicorn`'s own logging configuration replaces the
  root handler, which would drop the `service` field from every line.
- **Execution worker** — polls the Temporal task queue and runs nodes. It holds
  nothing in memory that matters: kill it mid-run and a replacement resumes the
  same workflow, because the history is in Temporal and the job is in
  PostgreSQL. **V0 registers `MockComputeBackend` and `MockLabBackend`**; both
  are mocks and everything they produce is recorded as simulated. Pass
  `--compute-backend slurm` (with the settings in §3 filled in) to register the
  real one instead; it refuses to start without a host and a username rather
  than failing at the first node that reaches the queue.
- **Supervisor** — discovers every active project in PostgreSQL and gives each
  one a loop: reads state, starts runs, notices endings, and asks Master or
  Review to decide what needs deciding. A replacement takes over the projects a
  dead one was driving, from the database and Temporal alone, and re-submitting
  work an unfinished attempt already submitted returns the same backend job
  rather than starting a second one.

### Running them under systemd

The same three entry points are what `infra/systemd/*.service` start, and
`scripts/install_services.sh` writes them:

```bash
scripts/install_services.sh --dry-run              # what would be written
scripts/install_services.sh --user                 # ~/.config/systemd/user, this checkout
sudo scripts/install_services.sh                   # /etc/systemd/system, /opt/ravel, user ravel
```

The installer substitutes two things — where the checkout is, and which account
the processes run as — and nothing else. `Restart=always`, `KillSignal=SIGTERM`,
`TimeoutStopSec` and the journal are the repository's, and
`tests/unit/test_service_units.py` reads them out of the files rather than
restating them. `Restart=always` rather than `on-failure` because not every
failure is a crash: a supervisor whose loop raised and exited cleanly is a
project that has stopped moving.

The units are installed, not started: whether a deployment wants these three
running is that deployment's decision.

```bash
systemctl start ravel-gateway ravel-supervisor ravel-temporal-worker
systemctl stop ravel-supervisor      # says so on the way out, see below
journalctl -u ravel-supervisor -f    # the same lines the console's panel reads
```

Set `RAVEL_LOG_FORMAT=json` for a collector; the default is `text` for a person.
Each line carries `ts`, `level`, `service`, `logger` and `message`, so one
journal holding all three services is still readable.

**A stop and a kill are told apart in the record.** A process that receives
`SIGTERM` writes `stopped_at` as it goes and stops beating; one that is killed
says nothing and its last beat simply recedes. Both are silence, and for two
poll intervals the console cannot tell them apart — so if you are looking at a
supervisor that is not beating, read `GET /projects/{id}/runtime/services` or
the operator's panel before concluding it crashed. A service that has never
reported is a third answer, and it means the deployment never started it.

A user unit runs only while its owner has a session. For a machine that should
keep running after the last login: `sudo loginctl enable-linger <user>`.

Ports, all bound to loopback:

| Service | Port |
|---|---|
| Gateway | 8000 |
| PostgreSQL | 55432 |
| Temporal frontend | 7233 |
| Temporal UI | 8088 |
| MinIO S3 API | 9100 |
| MinIO console | 9101 |

Every one of these is bound to `127.0.0.1` deliberately. Temporal's and MinIO's
APIs are unauthenticated at the transport layer and the development credentials
are in the repository, so publishing them would hand the project database to
anything that can reach the host. **To use the console or the Temporal UI from
another machine, forward the port over SSH** — do not widen the bind:

```bash
ssh -N -L 8088:127.0.0.1:8088 -L 9101:127.0.0.1:9101 user@cvm
```

## 5. First account and first project

The Gateway authenticates people and cannot create them. Membership is the one
thing in RAVEL that manufactures authority, so it is deliberately not reachable
over HTTP by whoever happens to be logged in; provisioning happens at a terminal
on the host:

```bash
# an owner, with a project to direct
.venv/bin/python scripts/create_account.py --username ada \
    --new-project \
    --title "Catalyst screen" \
    --objective "Find a dopant that raises conductivity by 15%."

# a bench user on that project — the granter must already hold the authority
.venv/bin/python scripts/create_account.py --username bench \
    --project <project_id> --role LAB_USER --granted-by ada

# an administrator, for the runtime-health screen
.venv/bin/python scripts/create_account.py --username root --no-membership
```

The password is read from the terminal and hashed with RAVEL's own Argon2id
parameters; it is never printed and never stored. `--password` exists for
scripted use and puts the password in your shell history.

Then drive it:

```bash
make project PROJECT=<project_id>
```

Master plans the first stage, the worker executes it, Review judges the result,
and the loop continues until the project ends — SUCCESS, FAILED, INCONCLUSIVE or
TERMINATED — or stops moving, which it reports rather than hiding.

## 6. Check that it is healthy

```bash
make env-check        # Phase -1's gate: platform, settings, PostgreSQL, MinIO,
                      # Temporal, and the pinned harness distributions
```

`make up` prints the Gateway URL and the log directory. Each service logs to
`runtime/logs/<name>.log`.

## 7. Stop

```bash
# the console: q, or Ctrl-C — stops the Gateway, worker and loop it started
make dev-down                # stop the containers, keep the data
scripts/dev_down.sh -v       # stop the containers AND DELETE the data
```

The `-v` form also removes `runtime/dsh_home`, `runtime/workspaces` and
`runtime/snapshots`, because harness state that outlived the database
describing it would be a home whose sessions refer to projects that no longer
exist. It asks for confirmation first.

## 8. Test

```bash
make test              # lint + unit + integration + the harness gate
make test-unit         # no services required
make test-integration  # needs make dev-up
make test-dsh          # the Phase 0 harness gate, against a real pinned runtime
make test-live         # real-Internet research; never mocked
make test-e2e          # the headless loop and the TUI
make acceptance        # A01-A20 and the seven extra gates, with the matrix
```

`make acceptance` is the one that answers the question the acceptance document
asks. A pytest summary says how many tests passed, not which of the twenty items
they were about, and says nothing about an item nobody wrote a test for; the
matrix says which item each case demonstrates, and calls an item with no case at
all a failure rather than a blank.

Two things to know before running the suites:

- **Never run two pytest processes at once.** They share one test database and
  each empties it on entry.
- **`make test` fails without `DEEPSEEK_API_KEY`**, at the harness gate, by
  design: that gate must not be able to pass by not running. Every other suite
  passes without it. See `KNOWN_LIMITATIONS.md` L-15.

## 9. What a V0 deployment is not

- **Compute and the laboratory are mocks.** Real compute and a real LIMS are
  out of V0's scope; the backends are interfaces with mock implementations, and
  every artifact a mock produces is marked simulated and refused by the
  Evidence Ledger as real evidence.
- **Research is real.** Web, API and database retrieval goes to the actual
  sources. A fabricated source cannot enter the ledger — the registration path
  has no argument for "I have read this".
- **There is no scheduler.** A project is driven by running the loop for it, and
  the loop is a process, not a service. Nothing in V0 restarts it for you.
- **There is no unattended recovery of Master.** A Master session that dies is
  replaced under the same identity with a fresh session id and recovers from
  PostgreSQL and the last checkpoint, never from chat history — but something
  has to run the loop again.

`KNOWN_LIMITATIONS.md` is the full list, written as each limit was found.
