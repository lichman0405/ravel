"""The three unit files, read as the contract they are.

A unit file is the only place in this repository where a decision is made about
what happens *after* a process dies, and it is a decision nothing in Python can
hold. So it is checked here rather than assumed: the restart policy, the signal
that gives the application a chance to record its own shutdown, the account and
the directory, and — the one worth a test of its own — that no credential is on
the command line, where `systemctl show` prints it to anyone who asks.

`systemd-analyze verify` is the authority on whether these are units at all.
Its exit status is not: it returns 0 for a unit whose `Restart=` is a word that
does not exist. The assertion is therefore on its output, which is the actual
finding, and the vocabulary it uses is asserted rather than the count of lines,
so a future systemd that adds a warning does not fail these tests.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ravel.domain.services import SERVICE_NAMES

ROOT = Path(__file__).resolve().parents[2]
UNITS = ROOT / "infra" / "systemd"
INSTALLER = ROOT / "scripts" / "install_services.sh"

#: What `systemd-analyze` says when it has not understood a line. Both were
#: observed from a unit carrying `Restart=whenever`, `Frobnicate=yes` and
#: `KillSignal=NOSUCHSIGNAL`; a third spelling appears for some directives, so
#: the match is on the stem both share.
PARSE_COMPLAINTS = ("Failed to parse", "Unknown key name", "Unknown lvalue")

#: A unit's name is its service's name with the project's prefix, and that is
#: the whole mapping. It is asserted rather than derived in the tests below so
#: that a rename on one side only fails here instead of quietly checking
#: nothing.
ANSI = re.compile(r"\033\[[0-9;]*m")


def unit_path(name: str) -> Path:
    """`name` is the unit's file name, which is how everything here refers to one.

    The installer's dry run prints them under those names and the directory
    listing is made of them, so a helper taking a bare service name would be a
    second spelling of the same thing that has to be kept in step.
    """
    return UNITS / name


def sections(name: str) -> dict[str, dict[str, str]]:
    """A unit file as `{section: {key: value}}`, with comments dropped.

    Repeated keys are not a thing these units do, and a mapping would silently
    keep the last of them, so the count is checked rather than hoped for.
    """
    text = unit_path(name).read_text(encoding="utf-8")
    parsed: dict[str, dict[str, str]] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            parsed.setdefault(section, {})
            continue
        key, _, value = line.partition("=")
        assert key not in parsed[section], f"{name}: {key} appears twice in [{section}]"
        parsed[section][key] = value
    return parsed


def service_of(name: str) -> dict[str, str]:
    parsed = sections(name)
    assert "Service" in parsed, f"{name} has no [Service] section"
    return parsed["Service"]


def test_there_is_one_unit_per_service_and_the_names_match() -> None:
    """The three processes, and nothing else claiming to be one of them.

    Both directions matter. A unit with no service name is a process the
    administrator's screen has no panel for, which is a process nobody will
    notice is down; a service name with no unit is a panel that is always
    empty. Neither is a state either side of this system can report.
    """
    on_disk = {path.name for path in UNITS.glob("*.service")}
    expected = {f"ravel-{name}.service" for name in SERVICE_NAMES}

    assert on_disk == expected


def test_a_unit_comes_back_from_anything_but_a_deliberate_stop() -> None:
    """`Restart=always`, and the two numbers that decide what "comes back" costs.

    `always` rather than `on-failure` is the load-bearing word: this system's
    failures are not all crashes. A supervisor whose loop raises and exits
    cleanly, or one killed by the OOM killer with a status systemd reads as
    ordinary, is a project that has stopped moving with nothing in the record
    to say why. `on-failure` would leave it there.

    `TimeoutStopSec` is asserted as a floor rather than a value, because the
    number is a deployment's judgement about how long its shutdown takes — it
    is the *presence* of a window before `SIGKILL` that the application needs,
    and a unit that escalated immediately would take the shutdown record with
    it.
    """
    for name in SERVICE_NAMES:
        service = service_of(f"ravel-{name}.service")

        assert service["Restart"] == "always", f"{name} would not come back"
        assert float(service["RestartSec"]) > 0, f"{name} would restart in a tight loop"
        assert float(service["TimeoutStopSec"]) >= 30, (
            f"{name} escalates to SIGKILL before its shutdown can finish"
        )


def test_a_unit_is_asked_to_stop_in_a_way_the_application_can_hear() -> None:
    """`SIGTERM`, which is the whole mechanism behind a legible deploy.

    The Gateway's lifespan and the Supervisor's shutdown path run on this
    signal and write the row that says the stop was deliberate. `SIGKILL` —
    what `KillSignal=SIGINT` on a process that has no handler degrades to after
    the timeout — leaves the row reading as a death for two poll intervals,
    which is exactly the window an operator is looking at the screen.
    """
    for name in SERVICE_NAMES:
        service = service_of(f"ravel-{name}.service")

        assert service["KillSignal"] == "SIGTERM", f"{name} is killed, not asked"
        assert service["Type"] == "simple", (
            f"{name} does not fork, so systemd must be told to treat its "
            "process as the service rather than wait for one to appear"
        )


def test_a_unit_runs_the_checkout_as_a_named_account(service_user: str = "ravel") -> None:
    """Who, where, and with which environment — the three that cannot be guessed.

    A `WorkingDirectory` of `/` would make every relative path in the
    application resolve somewhere else; the account is what keeps a compromise
    in one process from being a compromise of the host; and the
    `EnvironmentFile` is where the credential lives, which is the arrangement
    the next test depends on.
    """
    for name in SERVICE_NAMES:
        service = service_of(f"ravel-{name}.service")

        assert service["User"] == service_user, f"{name} runs as {service['User']!r}"
        assert service["Group"] == service_user, f"{name} runs in group {service['Group']!r}"
        assert service["WorkingDirectory"].startswith("/"), f"{name} has a relative cwd"
        assert service["EnvironmentFile"].endswith(".env"), (
            f"{name} does not read a deployment environment file, so there is "
            "nowhere for the Slurm credential to be that is not the unit"
        )


def test_a_unit_starts_the_runner_that_belongs_to_it() -> None:
    """The mapping, because a copy-pasted unit starts the wrong process.

    A gateway unit naming `run_supervisor.py` is a deployment with two
    supervisors and no API, and every screen that reads the Gateway's heartbeat
    would be reading a row written by a process that is not serving requests.
    """
    for name in SERVICE_NAMES:
        service = service_of(f"ravel-{name}.service")
        expected = f"run_{name.replace('-', '_')}.py"

        assert expected in service["ExecStart"], (
            f"ravel-{name}.service starts {service['ExecStart']!r}, not {expected}"
        )


def test_every_unit_names_an_entry_point_that_exists() -> None:
    """Against this checkout, not against the deployed one.

    `/opt/ravel` is where a deployment puts the repository, so the path is
    rewritten to here and the *script* is what is checked. A unit pointing at a
    file that was renamed is a service that fails to start at every boot, with
    a message in the journal nobody reads until a project stops moving.
    """
    for name in SERVICE_NAMES:
        argv = service_of(f"ravel-{name}.service")["ExecStart"].split()

        assert len(argv) == 2, f"{name} starts something other than one script: {argv}"
        program, script = argv
        assert program.endswith("/python"), f"{name} runs {program!r}"
        assert script.startswith("/opt/ravel/"), (
            f"{name} names {script!r}, which is not under the deployment prefix "
            "the installer rewrites"
        )

        relative = ROOT / script.removeprefix("/opt/ravel/")
        assert relative.is_file(), f"{name} names {script}, which is not in this checkout"


def test_no_credential_is_on_a_command_line() -> None:
    """`systemctl show` prints `ExecStart` to anyone who can ask, and it is logged.

    The Slurm password reaches the worker through `EnvironmentFile`, which
    systemd reads and does not report back. Anything on the command line is
    also in the journal's record of how the process was started and in `ps` for
    every user on the host.
    """
    forbidden = ("password", "passwd", "secret", "token", "apikey", "api_key")

    for name in SERVICE_NAMES:
        parsed = sections(f"ravel-{name}.service")
        for section, entries in parsed.items():
            for key, value in entries.items():
                if key == "EnvironmentFile":
                    continue
                lowered = value.lower()
                for word in forbidden:
                    assert word not in lowered, (
                        f"ravel-{name}.service has {key}={value!r} in [{section}], "
                        "which is a credential in a place systemd will repeat"
                    )


def test_systemd_understands_the_units_as_they_are_checked_in() -> None:
    """The file in the repository, read by the program that will read it.

    A unit systemd has not understood is not a unit: the directive it did not
    recognize is dropped and the default applies — and the default for a
    `Restart=` that was misspelled is *not* to restart, which is the failure
    this whole file exists to prevent.

    **The exit status is not asserted here, and could not be.** These files
    name `/opt/ravel`, which does not exist on a development host, so `verify`
    reports the interpreter as a missing binary and returns nonzero. That is a
    fact about the host. What it says about *parsing* is the signal, and the
    check that the status is clean belongs to the test below, which verifies a
    prefix that is really there.
    """
    _require("systemd-analyze")

    result = subprocess.run(
        ["systemd-analyze", "verify", *sorted(str(p) for p in UNITS.glob("*.service"))],
        capture_output=True,
        text=True,
        check=False,
    )

    assert not _complaints(result.stdout + result.stderr)


def test_systemd_accepts_the_units_a_default_install_would_write(tmp_path: Path) -> None:
    """The whole chain: installer renders, systemd verifies, and it is clean.

    This is the test the previous one deliberately is not. `--prefix` points at
    this checkout, so `ExecStart` names an interpreter and a script that are
    both really there, and `systemd-analyze` is then willing to say a plain
    yes — exit status and all. Everything else about the rendering is the
    default, including the account, so what is verified is one substitution
    away from what a deployment installs.
    """
    _require("systemd-analyze")

    written = _write_rendered(tmp_path, _dry_run("--prefix", str(ROOT)))

    result = subprocess.run(
        ["systemd-analyze", "verify", *sorted(str(p) for p in written.values())],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr

    assert not _complaints(output)
    assert result.returncode == 0, (
        "systemd refused the units a default install would write, with a prefix "
        f"that exists:\n{output}"
    )


def test_the_installer_changes_the_prefix_and_the_account_and_nothing_else() -> None:
    """The claim `install_services.sh` makes about itself, checked against it.

    The units are checked in as the concrete files they will be installed as,
    rather than as templates, so that what `systemd-analyze verify` reads is
    what runs. That only holds if the installer's rendering touches the two
    things that cannot be known when the file is written. If it rewrote
    anything else — a keyword, a timeout — the repository would be verifying a
    file that no deployment ever installs.
    """
    prefix, account = "/srv/relocated/ravel", "somebody"
    rendered = _rendered_units(_dry_run("--prefix", prefix, "--service-user", account))
    assert set(rendered) == {f"ravel-{name}.service" for name in SERVICE_NAMES}

    for name, lines in rendered.items():
        # Blank lines are dropped from both sides: the dry run's own framing is
        # made of them, and a unit's meaning does not depend on one. A line the
        # installer *added* would still be a directive, and so still counted.
        source = [
            line
            for line in unit_path(name).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        # Equality against the rendering this test computed itself, rather than
        # a rule about which lines may differ. A rule admits anything it did not
        # think of; this admits the two substitutions and nothing else, and a
        # third one is a failure whichever line it lands on.
        assert lines == [_substituted(line, prefix, account) for line in source], (
            f"{name} is not the checked-in unit with the prefix and the account "
            "substituted, so the file systemd verified is not the file that "
            "would be installed"
        )


def _substituted(line: str, prefix: str, account: str) -> str:
    """What `render()` in `install_services.sh` does to one line, in Python.

    Written out rather than imported, because there is nothing to import: the
    installer is a shell script and this is the specification of it that the
    test can hold it to.
    """
    if line.startswith("User="):
        return f"User={account}"
    if line.startswith("Group="):
        return f"Group={account}"
    return line.replace("/opt/ravel", prefix)


def _require(program: str) -> None:
    """Skip on a host that does not have the program, rather than fail there.

    Neither `systemd-analyze` nor a shell is a dependency of the application.
    A machine running the test suite without them is a machine that cannot
    answer the question, which is a skip and not a defect.
    """
    if shutil.which(program) is None:  # pragma: no cover - host dependent
        pytest.skip(f"{program} is not installed on this host")


def _dry_run(*extra: str) -> str:
    """What `install_services.sh` would write, captured before it writes it."""
    _require("bash")
    result = subprocess.run(
        ["bash", str(INSTALLER), "--dry-run", *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"the installer refused: {result.stderr}"
    return result.stdout


def _write_rendered(directory: Path, output: str) -> dict[str, Path]:
    """The dry run's output, as files, so systemd can be pointed at them."""
    written: dict[str, Path] = {}
    for name, lines in _rendered_units(output).items():
        path = directory / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written[name] = path
    return written


def _complaints(output: str) -> list[str]:
    """The lines where systemd said it did not understand what it was reading.

    Its vocabulary, not a count of lines: a future systemd that adds an
    unrelated warning to a healthy unit must not fail these tests, and a
    misspelling must.
    """
    return [line for line in output.splitlines() if any(p in line for p in PARSE_COMPLAINTS)]


def _rendered_units(output: str) -> dict[str, list[str]]:
    """The dry run's stdout, back into `{unit name: lines}`.

    The dry run is what the installer would write, printed. Reading it back
    through the same parser the tests use for the source is the point: it makes
    the comparison a comparison of units rather than of text.
    """
    units: dict[str, list[str]] = {}
    current: str | None = None
    for raw in output.splitlines():
        line = ANSI.sub("", raw)
        if not line.strip():
            continue
        header = re.fullmatch(r"── (ravel-.+\.service) ──", line.strip())
        name = header.group(1) if header is not None else None
        if name is not None:
            units[name] = []
            current = name
        elif current is not None:
            units[current].append(line)
    return units


def test_the_dry_run_reader_would_notice_a_unit_it_could_not_read() -> None:
    """Because every assertion above passes on an empty dict.

    `_rendered_units` returning nothing, or returning one unit, would make the
    installer test compare nothing to nothing. This is the case that fails if
    the dry run's format moves.
    """
    assert _rendered_units("") == {}
    assert list(_rendered_units("── ravel-gateway.service ──\nExecStart=/bin/true\n")) == [
        "ravel-gateway.service"
    ]
    assert _rendered_units("no headers here\n") == {}
