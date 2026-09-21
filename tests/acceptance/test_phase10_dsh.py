"""Phase 10's two harness items: the decision, and the isolation it decided on.

Phase 10A re-evaluated the harness for the single-host, multi-session topology
`docs/07_DSH_INTEGRATION.md` describes, and found the public release line still
does not carry it. The decision that followed — one runtime per
`(project_id, role)` — is recorded in two reports, and the first case here is
that the record and the code still agree.

The second is the property that decision exists for. Per-scope runtimes are not
a deployment detail: they are how a role's authority is fixed *before* an agent
exists, because the pinned harness sends no session identity with a tool call.
So the isolation is asserted structurally — distinct processes, distinct working
directories, distinct overlays — and without a model turn, which is why this
case runs on every machine rather than only where a credential is.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig

import ravel
from ravel.config import REPO_ROOT, Settings
from ravel.domain.roles import AgentRole
from ravel.dsh.pool import DshRuntimePool, create_pool
from ravel.dsh.roles import definition_for
from ravel.dsh.runtime import ROLE_PATCH_NAME, RoleRuntime

pytestmark = [pytest.mark.phase10, pytest.mark.timeout(600)]

PIN_PATH = REPO_ROOT / "vendor" / "DSH_PIN.json"

#: The newer release the re-evaluation opened and read. It is not a pin and is
#: not installable; it is named here because the decision to stay on the pin is
#: only meaningful if something newer was actually examined.
EXAMINED_TAG = "dsh-v0.1.6-alpha.2"

#: The harness client the pinned SDK exports. A module holding either can start
#: a harness process, and a second place that can is a second place a scope
#: could be lost.
HARNESS_TYPES = (DeepSeekHarness, DeepSeekHarnessConfig)

#: The one module allowed to hold them.
MODULE_HOLDING_THE_HARNESS = "ravel.dsh.runtime"

#: The three scopes every case here works over: two projects, so that a leak
#: between projects would show, and two roles in one of them, so that a leak
#: between roles would too.
SCOPES = (
    ("proj-a", AgentRole.MASTER),
    ("proj-a", AgentRole.COMPUTE_WORKER),
    ("proj-b", AgentRole.MASTER),
)


@pytest.fixture
def scoped(execution_settings: Settings, tmp_path: Path) -> Settings:
    """The deployment's settings, with runtimes under this case's own root.

    A runtime writes its working directory and its overlay the moment it is
    created, so a case that used the deployment's root would leave a scope
    behind in the checkout — and a leaked working directory is exactly the sort
    of thing the next case would be diagnosed against. Everything else is the
    deployment's: the harness binary, the profile, the provider and the model.
    """
    return execution_settings.model_copy(update={"runtime_dir": tmp_path / "runtime"})


def ravel_modules() -> list[str]:
    """Every importable module in RAVEL's own package.

    The migration revisions are skipped: they are DDL scripts alembic runs from
    its own directory, and nothing imports them at runtime.
    """
    return [
        info.name
        for info in pkgutil.walk_packages(ravel.__path__, prefix="ravel.")
        if ".migrations" not in info.name
    ]


def overlay_of(pool: DshRuntimePool, project_id: str, role: AgentRole) -> list[dict[str, Any]]:
    """One scope's overlay, read back the way the harness reads it.

    Read from the file the runtime was launched with rather than from the
    composition object that produced it: the file is what the harness applies,
    and a case that asked the generator what it generated would pass even if
    the write never happened.
    """
    runtime = scope_of(pool, project_id, role)
    return yaml.safe_load((runtime.work_dir / ROLE_PATCH_NAME).read_text(encoding="utf-8"))


def decision(overlay: list[dict[str, Any]], row_id: str) -> dict[str, Any]:
    """One named decision out of an overlay."""
    return next(row for row in overlay if row.get("id") == row_id)


def mounted_server(overlay: list[dict[str, Any]]) -> dict[str, Any]:
    """The tool server the overlay mounts, as it is launched."""
    return next(row for row in overlay if "insert" in row)["insert"][0]["config"]


def scope_of(pool: DshRuntimePool, project_id: str, role: AgentRole) -> RoleRuntime:
    """A live runtime, or a failure naming the scope that was not there."""
    runtime = pool.live_runtime(project_id, role)
    assert runtime is not None, f"({project_id}, {role.value}) is not live"
    return runtime


# ── 10A ─────────────────────────────────────────────────────────────────────


def test_p10_01_the_latest_release_was_re_evaluated() -> None:
    """P10-01: the re-evaluation's conclusion is still in force.

    Three things that are one claim. The pin names the release this tree runs,
    and it says no patch is required: the finding was that the single-host
    topology is not reachable at this release line without forking the harness.
    And the package still has exactly one place that can start a harness
    process, which is what makes "one runtime per scope" a property of the code
    rather than of a diagram.
    """
    pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))

    assert pin["patches"]["required"] is False, (
        "a patch is required at this pin, and the re-evaluation's finding was "
        "that the harness is reachable without forking it"
    )

    holders = sorted(
        module_name
        for module_name in ravel_modules()
        if any(
            value is harness_type
            for value in vars(importlib.import_module(module_name)).values()
            for harness_type in HARNESS_TYPES
        )
    )
    assert holders == [MODULE_HOLDING_THE_HARNESS], (
        f"{holders} can start a harness process; a second place that can is a "
        "second place a project's scope could be lost"
    )


def test_p10_02_the_decision_is_evidence_backed() -> None:
    """P10-02: the single-host decision is recorded with what a re-pin needs.

    Phase 10B allows only two conclusions, and the one RAVEL reached — keep the
    per-`(project, role)` runtimes — is only legitimate with evidence attached:
    which release was examined, where in it the blocker lives, why the obvious
    workaround is refused, and what would change the answer. All four are
    checked here against `docs/IMPLEMENTATION_DEVIATIONS.md` D-001, so a future
    re-pin that quietly drops the reasoning fails this case rather than
    inheriting its conclusion.
    """
    deviations = (REPO_ROOT / "docs" / "IMPLEMENTATION_DEVIATIONS.md").read_text(encoding="utf-8")
    pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    pinned_commit = str(pin["pin"]["commit"])

    # D-001 records the deviation, and the re-evaluation that keeps it open.
    assert "## D-001" in deviations and EXAMINED_TAG in deviations, (
        "D-001 does not record the re-evaluation that retained it, "
        "so the next re-pin would redo the work"
    )

    # The examined release and the pinned commit are both named, so the
    # decision is evidence of a release that was opened and read, not assumed.
    assert pinned_commit in deviations, (
        "D-001 does not name the pinned commit the decision applies to"
    )

    # Where in the harness the blockers are, not just that they exist.
    landmarks = re.findall(r"\S+\.(?:ts|py|json):\d+", deviations)
    assert len(set(landmarks)) >= 2, (
        f"D-001 cites {sorted(set(landmarks))}; each blocker is a claim "
        "about the harness, and a claim needs a place to verify it"
    )

    # The rule the workaround would have broken, quoted rather than paraphrased.
    assert (
        "project-scoped authorization" in deviations
        and "model-supplied" in deviations
        and "project_id" in deviations
    ), (
        "D-001 does not name the rule that makes the shared-MCP workaround unacceptable"
    )

    # And what would reopen the question.
    assert re.search(r"re-?evaluate", deviations, re.IGNORECASE), (
        "nothing in D-001 says what would make the single-host topology "
        "reachable, so the deviation would be permanent by omission"
    )


# ── The decision's consequences ─────────────────────────────────────────────


def test_p10_16_every_scope_gets_its_own_process_and_its_own_overlay(
    scoped: Settings,
) -> None:
    """P10-16: a role's authority is fixed before the agent exists.

    Three scopes over two projects and no turn run on any of them: what is
    under test is what the pool hands a scope, not what an agent does with it.
    Each gets its own runtime, its own working directory, and a harness
    configured to run *in* that directory — a shared one would let one role
    read another's overlay and brief off disk. Each overlay names its own scope
    in the environment its tool server is launched with, and boots on its own
    role's contract.

    That environment is the whole grant, not only the two names: the state
    coordinates come with it, because a server that had to fall back on `.env`
    could serve the same project id out of a database the process driving it is
    not using. Which project a session may act on is meaningless without which
    database holds it, so the two are passed together and asserted together.
    """
    pool = create_pool(scoped)
    try:
        for project_id, role in SCOPES:
            pool.runtime(project_id, role)

        runtimes = [scope_of(pool, project_id, role) for project_id, role in SCOPES]
        assert len({id(runtime) for runtime in runtimes}) == 3, (
            "two scopes share a runtime, so they share a session and a model"
        )
        assert len({runtime.work_dir for runtime in runtimes}) == 3, (
            "two scopes share a working directory"
        )
        assert pool.stats().scopes == (
            ("proj-a", "compute-worker"),
            ("proj-a", "master"),
            ("proj-b", "master"),
        )

        master, worker, other = runtimes
        for runtime in runtimes:
            config = runtime.harness.config
            assert config.cwd is not None and config.runtime_cwd is not None, (
                "the runtime was prepared without a working directory, so the "
                "harness would pick one of its own"
            )
            assert Path(config.cwd) == runtime.work_dir, (
                f"({runtime.project_id}, {runtime.role.value}) runs in a directory "
                "that is not its own"
            )
            assert Path(config.runtime_cwd) == runtime.work_dir
            assert runtime.brief_path.parent == runtime.work_dir

        # The scope travels in the environment the tool server is launched
        # with, because that is the only thing the server can trust: the pinned
        # harness sends no session identity alongside a tool call.
        for runtime in runtimes:
            server = mounted_server(overlay_of(pool, runtime.project_id, runtime.role))
            assert server["env"] == {
                **scoped.tool_server_env(),
                "RAVEL_PROJECT_ID": runtime.project_id,
                "RAVEL_ROLE": runtime.role.value,
                "RAVEL_BRIEF_FILE": str(runtime.brief_path),
            }, f"({runtime.project_id}, {runtime.role.value}) boots with another scope"
            assert json.loads(runtime.brief_path.read_text(encoding="utf-8")) == {
                "project_id": runtime.project_id,
                "role": runtime.role.value,
            }, "the brief the tool server reads names another scope"

        # Two roles in one project are one server with two rosters: the module
        # is the same, and which of the five the process may reach is decided by
        # the role it was launched with, never by a name in a payload.
        assert mounted_server(overlay_of(pool, "proj-a", AgentRole.MASTER))["args"] == [
            "-m",
            definition_for(AgentRole.MASTER).mcp_module,
        ]
        assert (
            definition_for(AgentRole.MASTER).mcp_module
            == definition_for(AgentRole.COMPUTE_WORKER).mcp_module
        )

        # And each boots on its own contract, verbatim.
        for runtime in (master, worker):
            prompt = decision(overlay_of(pool, runtime.project_id, runtime.role), "system-prompt")
            assert prompt["config"]["personaPrefix"] == definition_for(runtime.role).persona, (
                f"({runtime.project_id}, {runtime.role.value}) boots on another role's contract"
            )

        # No role is given a shell, so what a role may do is exactly the roster
        # its tool server registers — not whatever the harness would run for it.
        for runtime in (master, worker):
            overlay = overlay_of(pool, runtime.project_id, runtime.role)
            for shell in ("persistent-bash", "persistent-pwsh"):
                assert decision(overlay, shell)["disabled"] is True, (
                    f"({runtime.project_id}, {runtime.role.value}) can reach a shell, "
                    "so its authority is not its roster"
                )

        # Ending one scope ends one scope.
        assert pool.close_scope("proj-a", AgentRole.MASTER) is True
        assert pool.live_runtime("proj-a", AgentRole.MASTER) is None
        assert scope_of(pool, "proj-a", AgentRole.COMPUTE_WORKER) is worker
        assert scope_of(pool, "proj-b", AgentRole.MASTER) is other
        assert master.is_closed, "the closed scope's process was left running"
    finally:
        pool.close()


def test_p10_16_a_scope_is_its_directory_even_across_a_restart(scoped: Settings) -> None:
    """P10-16: a scope's directory is derived from the scope, not remembered.

    An unattended deployment restarts its runtimes — the supervisor reaps them
    when they go idle, and the process itself is expected to die and come back.
    What makes that safe is that nothing about *where* a scope lives is carried
    in memory: the path is a function of `(project, role)` under the configured
    runtime root, so a later pool adopts the same directory, and no two scopes
    share one.
    """
    first_pool = create_pool(scoped)
    try:
        for project_id, role in SCOPES:
            first_pool.runtime(project_id, role)
        first = scope_of(first_pool, "proj-a", AgentRole.MASTER)
        work_dir, brief = first.work_dir, first.brief_path.read_text(encoding="utf-8")
        root = scoped.runtime_path("dsh_cwd")

        assert work_dir == scoped.dsh_runtime_cwd("proj-a", AgentRole.MASTER.slug)
        assert work_dir.is_relative_to(root), (
            "a scope wrote outside the runtime root a deployment configures"
        )
        assert sorted(str(path.relative_to(root)) for path in root.glob("*/*")) == [
            "proj-a/compute-worker",
            "proj-a/master",
            "proj-b/master",
        ], "the runtime root holds a directory no scope asked for"
    finally:
        first_pool.close()

    second_pool = create_pool(scoped)
    try:
        again = second_pool.runtime("proj-a", AgentRole.MASTER)
        assert again is not first, "the second pool reused a closed runtime"
        assert again.work_dir == work_dir, "the scope moved when the pool restarted"
        assert (again.work_dir / ROLE_PATCH_NAME).is_file(), (
            "the scope's overlay was lost, so the restarted runtime has no authority"
        )
        assert again.brief_path.read_text(encoding="utf-8") == brief
    finally:
        second_pool.close()
