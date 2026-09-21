"""Phase 10 items about how a deployment is run, not what it decides.

Three of the twenty are about the shape of the thing an operator meets, and
none of them is about science:

- **P10-13** — normal operation must not require naming a project to a script.
  `scripts/run_project.py` survives as a debugging entry point, and a project
  created the ordinary way is picked up by the supervisor.
- **P10-19** — A01-A20 are not deleted or loosened. That is asserted by running
  them (`make acceptance`), and what is checked here is the thing a run cannot
  notice: that the items are still the twenty they were, and that none of them
  has been turned into a skip.
- **P10-20** — one command starts the whole server, and the console is a client
  of it rather than the thing that runs it.

The last one is why this module reads shell rather than Python. "Closing the
console does not stop RAVEL" is a claim about process lifetime, and the process
that outlives the console is a shell script's — so what is checked is the
script's structure, paired with P10-15's live demonstration that a project
survives a console that came and went.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.phase10]

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
ACCEPTANCE = REPO_ROOT / "acceptance"

#: The infra script that starts everything, named once.
RUN_V0 = SCRIPTS / "run_v0.sh"


def code_of(path: Path) -> str:
    """A shell script with its comments and blank lines removed.

    The scripts here explain their choices in prose, and the prose names the
    alternatives they rejected — `wait -n`, the manual driver — so a search over
    the whole file finds the decision and its explanation in the same haystack
    and cannot tell them apart. What runs is what these assertions are about.
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


# ── P10-13 ──────────────────────────────────────────────────────────────────


def test_p10_13_no_manual_project_driving_is_required() -> None:
    """P10-13: the default way to run a deployment does not name a project.

    `run_v0.sh` without arguments is what a person runs, and what it starts is
    the supervisor — the thing that reads PostgreSQL and finds the work. A
    script that drove a named project by default would be the Phase 9 workflow
    wearing Phase 10's supervisor, and the whole item is that the person does
    not have to know which project to name.

    `run_project.py` is allowed to exist, and this checks what it *is*: a
    debugging entry point, reachable only when somebody asks for it by name.
    """
    script = code_of(RUN_V0)

    # `PROJECT` starts empty, so the branch a deployment takes when nobody asks
    # for anything is the one that starts the supervisor. Read as the if/else it
    # is, because that is what decides which process a deployment gets — and
    # asserted on the branches rather than on line order, since the `if` branch
    # necessarily comes first whatever each one starts.
    assert 'PROJECT=""' in script, (
        "PROJECT does not start empty, so a deployment cannot be started without "
        "naming one"
    )
    decided = script[script.index('if [[ -n "$PROJECT" ]]') :]
    chosen, _, otherwise = decided.partition("else")
    assert "run_project.py" in chosen, (
        "the --project branch does not start the manual driver"
    )
    assert "run_supervisor.py" in otherwise, (
        "the supervisor is not what runs when no project was named, so the default "
        "path is something else"
    )

    # And what the manual script says about itself, since a debugging entry
    # point that reads as the supported path is how a requirement comes back.
    manual = (SCRIPTS / "run_project.py").read_text(encoding="utf-8")
    assert "debugging entry point" in manual, (
        "run_project.py does not say what it is for, so it reads as the way to "
        "run a project"
    )
    assert "run_supervisor.py" in manual, (
        "run_project.py does not point at what a deployment actually runs"
    )


def test_p10_13_the_supervisor_is_what_discovers_projects() -> None:
    """P10-13: discovery is the supervisor's, and it is the supervisor's alone.

    The other half of "no manual driving" is that something *does* the driving
    without being asked. That something is one process with one job: read the
    registry, decide which projects are still going, and give each one a loop.
    """
    source = (REPO_ROOT / "src/ravel/execution/supervisor.py").read_text(
        encoding="utf-8"
    )
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )

    assert "active_projects" in body or "ACTIVE" in body, (
        "the supervisor does not say which projects it considers its own"
    )
    assert "ProjectLoop" in body, "the supervisor builds no loop, so it drives nothing"

    # A supervisor that took a project identifier would need one from somewhere
    # — an argument, an environment variable, a request — and there is none.
    constructor = body[body.index("class ProjectSupervisor") :]
    assert "project_id" not in constructor.split("def ")[0], (
        "the supervisor is constructed with a project, so it is being told what "
        "to drive rather than finding it"
    )


# ── P10-19 ──────────────────────────────────────────────────────────────────


def test_p10_19_the_original_items_are_still_the_original_twenty() -> None:
    """P10-19: A01-A20 are intact, and none of them has become a skip.

    "Not deleted or loosened" is a claim about a document and about the code
    that demonstrates it, so both are read. The acceptance catalogue must still
    name twenty items, and every one of them must still have a case: an item
    whose test was renamed out of existence, or marked `skip`, would leave the
    catalogue claiming something nothing demonstrates.

    Whether the twenty actually *pass* is not a question this can answer — that
    is what running them is for, and `make acceptance` is what runs them. This
    is the part a passing run cannot see.
    """
    catalogue = (ACCEPTANCE / "V0_ACCEPTANCE.md").read_text(encoding="utf-8")
    items = re.findall(r"^##\s+A(\d{2})\b", catalogue, flags=re.MULTILINE)
    assert len(items) == 20, (
        f"the V0 catalogue names {len(items)} items; A01-A20 is the invariant"
    )
    assert sorted(items) == [f"{n:02d}" for n in range(1, 21)], (
        f"the catalogue's items are not A01-A20: {sorted(items)}"
    )

    for path in sorted((REPO_ROOT / "tests/acceptance").glob("test_a*.py")):
        source = path.read_text(encoding="utf-8")
        assert "pytest.mark.skip" not in source, f"{path.name} skips an acceptance item"
        assert "pytest.mark.xfail" not in source, (
            f"{path.name} marks an acceptance item as expected to fail"
        )


# ── P10-20 ──────────────────────────────────────────────────────────────────


def test_p10_20_one_command_starts_the_whole_server() -> None:
    """P10-20: `scripts/run_v0.sh` is the one command, and it starts everything.

    Four processes, and the item is that no fifth step exists: no second
    terminal, no manual driver, no ordering a person has to remember. What the
    script starts is asserted from the script, because the alternative — starting
    it here and reading `ps` — tests the machine this happens to run on.
    """
    script = RUN_V0.read_text(encoding="utf-8")

    for service in (
        "ravel.gateway.app:create_app",  # the Gateway the console signs in to
        "run_temporal_worker.py",  # the activities' host
        "run_supervisor.py",  # what finds and drives projects
    ):
        assert service in script, f"run_v0.sh does not start {service}"

    assert "alembic" in script, (
        "run_v0.sh does not bring the schema to head, so the first run against a "
        "fresh database fails for a reason unrelated to the project"
    )
    assert "dev_up.sh" in script, "run_v0.sh does not bring up the infrastructure"


def test_p10_20_the_console_is_a_client_and_not_the_server() -> None:
    """P10-20's real claim: quitting the console leaves RAVEL running.

    This is the definition of done's headline, and it is a statement about the
    shell: the TUI is started in the foreground and, when it exits, the script
    does not. `wait` is what it blocks on instead — and the difference between
    `wait` and `wait -n` is the whole item, because `wait -n` returns as soon as
    *any* child exits and would take the deployment down with the console.

    A structural assertion rather than a live one, deliberately: what the live
    demonstration proves is that a *project* survives (P10-15), and what this
    proves is that the process holding the supervisor is not the console. Faking
    a terminal to check the other end of the same fact would test `pexpect`.
    """
    script = code_of(RUN_V0)

    # The console's branch alone. The `--no-tui` branch is on the other side of
    # the `else` and *does* use `wait -n`, deliberately: a server with no
    # console has nobody watching it, so exiting when a service dies is how a
    # supervisor restarts it. Scoping the search is what keeps the two apart.
    block = script[script.index("if [[ $WITH_TUI -eq 1 ]]") :]
    console, _, without_console = block.partition("\nelse\n")
    assert "ravel.tui" in console, "the console is not in the branch for it"

    after = console[console.index("ravel.tui") :]
    assert "wait || true" in after, (
        "the script does not block after the console exits, so closing the "
        "console stops the supervisor too"
    )
    assert "wait -n" not in after, (
        "the script waits for the first child to exit after the console closes, "
        "so one service dying — or one already dead — tears down the deployment"
    )
    assert "wait" in without_console, (
        "the --no-tui branch does not block at all, so it would exit immediately"
    )

    assert "stop_children" in script, (
        "nothing stops the children, so Ctrl-C would leave the services orphaned"
    )
    assert "trap stop_children EXIT INT TERM" in script, (
        "the children are not stopped on the way out"
    )


def test_p10_20_the_temporal_execution_worker_is_not_an_agent() -> None:
    """P10-20, and P10-I's rename: three names that must not be conflated.

    `Temporal Execution Worker` hosts activities. `Compute Worker Agent` and
    `Experimental Worker Agent` are DSH sessions that act under a contract and
    decide nothing about science. A `Backend` is the thing that does the work —
    a mock, in V0. The old name for the first was `run_worker.py`, which read as
    "the worker", and that is the confusion the rename exists to end.

    So the script says what it is in its own first line, and no document or
    script calls it an agent.
    """
    temporal_worker = (SCRIPTS / "run_temporal_worker.py").read_text(encoding="utf-8")
    opening = temporal_worker.split('"""')[1]

    assert "not a RAVEL Agent" in opening, (
        "the Temporal worker script does not say it is not an agent, so the "
        "rename is only a filename"
    )
    assert "Temporal Execution Worker" in opening

    assert not (SCRIPTS / "run_worker.py").exists(), (
        "run_worker.py is back, and its name is the ambiguity this item ends"
    )

    # And what a person reads first: the deployment's own documentation.
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    if "Temporal" in readme:
        assert "Temporal Execution Worker" in readme, (
            "the README mentions Temporal's worker without the name that "
            "distinguishes it from the two Worker agents"
        )
