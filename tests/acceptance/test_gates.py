"""The seven extra gates: the claims that hold across the whole of V0.

`acceptance/V0_ACCEPTANCE.md` ends with a list headed "Required extra gates —
these are part of the above acceptance and cannot be skipped". They are not a
twenty-first item. Each is a property of the system rather than of one project's
run through it, and they all fail the same way: quietly. A mocked search, a
harness that drifted off the pin, a fourth role holding a DAG mutation, an
authoritative-state cache — none of these break any single acceptance item's
assertions. They make those assertions be about something else.

So each gate is written against the thing that would have to change for it to
fail, and each says in its docstring which of A01-A20 carries the covered half
when a credential is what stands in the way. Where the live half cannot run
here — RAVEL will not fetch anonymously, so a live search needs a contact
address — the gate asserts the half that can and names the test that carries
the rest. It does not pass on a substitute, which is the one thing this project
does not do.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import fields
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from deepseek_harness import DeepSeekHarnessConfig
from sqlalchemy import select
from tests.dsh.mcp_probe import ProbeResult, ToolCall, probe
from tests.e2e.conftest import LiveGateway
from tests.integration.backends.conftest import FakeClock
from tests.integration.conftest import Prepared
from tests.integration.roles.conftest import RoleEnvironment
from tests.support.routes import EXPECTED_ROUTE_FLOOR, effective_routes

from ravel.backends import Catalogue, MockComputeBackend, MockLabBackend, catalogue
from ravel.config import REPO_ROOT, Settings
from ravel.domain.artifacts import is_simulated, simulated_provenance
from ravel.domain.clock import utcnow
from ravel.domain.enums import AccessStatus, NodeType
from ravel.domain.evidence import EvidenceSource
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.dsh.composition import BASE_PROFILE, MCP_SERVER_NAME, RoleComposition
from ravel.dsh.roles import ROLE_DEFINITIONS, definition_for
from ravel.gateway.routes.conversation import Turn
from ravel.mcp.registry import DAG_MUTATION_TOOLS, tools_for
from ravel.research.connectors import default_connectors
from ravel.research.search import SearchUnavailable, provider_for
from ravel.state.database import Database
from ravel.state.outbox import last_event_seq
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceSourceRepository,
    SimulatedEvidenceError,
)
from ravel.state.store import S3ArtifactStore
from ravel.state.tables import ArtifactRow

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]

#: Where the pin lives, and where a re-fetch of the harness lands.
PIN_PATH = REPO_ROOT / "vendor" / "DSH_PIN.json"
VENDORED_TREE = REPO_ROOT / "vendor" / "deepseek-harness"

#: The two PyPI distributions the pin names as a matched pair. Asserted as a
#: set rather than read out of the pin's own dict, because the dict also holds
#: prose keys and a check that iterated it would silently stop checking the day
#: somebody renamed one — `test_gate_4` asserts the pin still names both.
DISTRIBUTIONS = ("deepseek-harness-sdk", "deepseek-harness-runtime-bin")

#: Which service each shipped connector must be pointed at. A connector is the
#: one place in RAVEL where a hostname decides whether a claim is about the
#: literature or about a stub, so the host is pinned here by name.
CONNECTOR_HOSTS = {
    "crossref": "api.crossref.org",
    "openalex": "api.openalex.org",
    "arxiv": "export.arxiv.org",
    "pubchem": "pubchem.ncbi.nlm.nih.gov",
}

#: Modules whose presence anywhere near the research path would mean a source
#: can be answered rather than fetched.
_DOUBLE_MODULES = frozenset(
    {
        "unittest.mock",
        "mock",
        "respx",
        "responses",
        "requests_mock",
        "pytest_httpserver",
        "vcr",
        "betamax",
    }
)

#: A path a caller could use to reach the harness itself rather than RAVEL.
#: The one route that may carry one of these words is the administrator's
#: health read, and it is named separately so that a second one has to be a
#: deliberate edit here rather than a new route nobody notices.
_HARNESS_WORDS = ("harness", "dsh", "session", "completion", "chat", "model")
HARNESS_HEALTH_PATH = "/projects/{project_id}/runtime/harness"

#: The scenarios that end with the backend having delivered something, and so
#: with artifacts to mark. Named one by one rather than counted, because the
#: interesting change is *which* scenario stopped delivering: a failed or
#: timed-out run owes nothing, and a run that has quietly started owing nothing
#: is the failure the marking rule is written against.
PRODUCING_SCENARIOS = frozenset(
    {"COMPUTE_SUCCESS", "COMPUTE_MISSING_OUTPUT", "LAB_SUCCESS", "LAB_MISSING_RAW_DATA"}
)


def _payload(result: ProbeResult, index: int, what: str) -> dict[str, Any]:
    """One call's structured payload, with the ways it can be absent ruled out."""
    call: ToolCall = result.calls[index]
    assert not call.failed, f"{what} failed: {call.error}"
    assert call.payload is not None, f"{what} returned no structured payload"
    return call.payload


def _module_imports(root: Path) -> list[tuple[Path, str]]:
    """Every module named by an import under `root`, with the file naming it."""
    found: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                found.append((path, node.module or ""))
            elif isinstance(node, ast.Import):
                found.extend((path, alias.name) for alias in node.names)
    return found


def _substitution_parameters(root: Path) -> list[tuple[Path, str]]:
    """Functions under `root` that take pytest's `monkeypatch` fixture.

    Named `monkeypatch` and taken as a parameter, because that is the only way
    a pytest suite can replace a collaborator, and a suite that never does is a
    suite whose results are about the world.
    """
    found: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if any(argument.arg == "monkeypatch" for argument in arguments):
                found.append((path, node.name))
    return found


def _entry(patch: list[dict[str, object]], entry_id: str) -> dict[str, Any]:
    """One entry of a role overlay, by its identifier."""
    for entry in patch:
        if entry.get("id") == entry_id:
            return dict(entry)
    raise AssertionError(f"the overlay has no {entry_id!r} entry: {[e.get('id') for e in patch]}")


def _mounted_server(patch: list[dict[str, object]]) -> dict[str, Any]:
    """The one tool server a role's overlay mounts.

    Kept together with its assertion, because "exactly one" is half the claim:
    a role whose overlay mounted a second server would have a roster that is
    the union of two, and the registry would no longer be the whole of what the
    agent can reach.
    """
    mounts = [entry["insert"] for entry in patch if "insert" in entry]
    assert len(mounts) == 1, f"the overlay mounts {len(mounts)} server entries"
    servers = mounts[0]
    assert isinstance(servers, list), f"the insert holds a {type(servers).__name__}"
    assert len(servers) == 1, f"the overlay mounts {len(servers)} tool servers"
    server = servers[0]
    assert isinstance(server, dict), f"a server entry is a {type(server).__name__}"
    return server


def _composition(role: AgentRole, project_id: str, tmp_path: Path) -> RoleComposition:
    """One role's runtime composition, built the way the pool builds it."""
    return RoleComposition.for_scope(
        project_id=project_id,
        role=role,
        mcp_command=sys.executable,
        brief_path=tmp_path / f"{role.value}.brief.json",
    )


def _pin() -> dict[str, Any]:
    """`vendor/DSH_PIN.json`, parsed."""
    return json.loads(PIN_PATH.read_text(encoding="utf-8"))


# ── Gate 1: live research must reach real original sources ──────────────────


def test_gate_1_research_is_pointed_at_real_services_and_cannot_be_answered_offline(
    integration_settings: Settings,
) -> None:
    """*live Research test must reach real original sources.*

    Two halves, because the claim has two ways of going wrong. A connector can
    be repointed at something that answers without a library behind it, and a
    search can be *satisfied* by a fallback rather than by a service. The first
    is checked by pinning each shipped connector's host by name — a change of
    host is then a deliberate edit to this table, not a quiet reconfiguration.
    The second is checked by there being no fallback to find: with no provider
    configured, `provider_for` raises and names the setting, and no code path
    turns that into an empty list of leads.

    The third check is on the suite rather than on RAVEL. `tests/live_research/`
    is the suite whose whole value is that it is never mocked, and the day
    somebody mocks a fetch there to make it pass on a train, this gate is the
    thing that says so. It scans for the two ways a pytest suite substitutes —
    a double import and the `monkeypatch` fixture — rather than trusting the
    module docstrings that say it does not.

    The live reach itself is A03's, which opens Crossref and asks it about the
    DOI RAVEL returned, and A04's, which hashes bytes it fetched. Both are in
    this suite and both skip when `RAVEL_RESEARCH_CONTACT_EMAIL` is unset,
    because RAVEL will not fetch anonymously and a placeholder address would be
    a claim about who is asking.
    """
    shipped = default_connectors(integration_settings)
    assert set(shipped) == set(CONNECTOR_HOSTS), (
        "the connectors RAVEL builds and the hosts this gate pins have drifted "
        f"apart: it builds {sorted(shipped)}"
    )
    for name, host in CONNECTOR_HOSTS.items():
        endpoint = _endpoint_of(f"ravel.research.connectors.{name}")
        assert host in endpoint, (
            f"the {name} connector no longer reaches {host} but {endpoint}. A "
            "connector pointed anywhere else returns leads whose identifiers no "
            "service will confirm, which is the thing A03 exists to catch."
        )

    unconfigured = integration_settings.model_copy(
        update={"search_provider": None, "search_api_key": None}
    )
    with pytest.raises(SearchUnavailable) as refused:
        provider_for(unconfigured)
    assert "RAVEL_SEARCH_PROVIDER" in str(refused.value), (
        "the refusal has to name the setting that would fix it, or the operator "
        "reading it learns only that something is missing"
    )
    assert "memory" in str(refused.value), (
        "the refusal no longer says why RAVEL will not work around it"
    )

    doubled = [
        (path, module)
        for path, module in _module_imports(REPO_ROOT / "tests" / "live_research")
        if module in _DOUBLE_MODULES or module.split(".")[0] in _DOUBLE_MODULES
    ]
    assert doubled == [], (
        f"the suite that must reach real sources imports a test double: {doubled}"
    )
    assert _substitution_parameters(REPO_ROOT / "tests" / "live_research") == [], (
        "the live suite takes `monkeypatch`, so one of its results is about a "
        "replacement rather than about a service"
    )
    assert _module_imports(REPO_ROOT / "src" / "ravel" / "research") != [], (
        "the scan found no modules under the research package, so it checked nothing"
    )
    production_doubles = [
        (path, module)
        for path, module in _module_imports(REPO_ROOT / "src" / "ravel" / "research")
        if module in _DOUBLE_MODULES
    ]
    assert production_doubles == [], (
        f"the research path itself imports a test double: {production_doubles}"
    )


def _endpoint_of(module_name: str) -> str:
    """The service a connector's module says it talks to.

    Read off the module rather than off a constructed connector, because the
    base URL is what decides where a lead came from, and a connector built with
    settings would answer the same question with more moving parts.
    """
    module = importlib.import_module(module_name)
    for attribute in ("API", "BASE"):
        value = getattr(module, attribute, None)
        if isinstance(value, str) and value.startswith("https://"):
            return value
    raise AssertionError(f"{module_name} declares no https endpoint to check")




# ── Gate 2: a fabricated source is an automatic failure ─────────────────────


async def test_gate_2_a_source_nobody_read_cannot_enter_the_ledger(
    project: Project,
    research_task: Prepared,
    integration_settings: Settings,
    database: Database,
    tmp_path: Path,
) -> None:
    """*fabricated DOI/source => automatic failure.*

    "Automatic" is the whole of it: the failure has to be the system's, not a
    reviewer's. Three ways to state a source that was never read are tried, and
    each is refused by the tool that would have written it, and the ledger is
    read afterwards and is empty.

    The refusal is structural rather than a rule anybody has to remember.
    `register_source` takes a *retrieval reference* — a handle to bytes the
    server is holding — and there is no argument for a URL, a title or a hash,
    so a model cannot describe a source into existence; it can only point at
    one. `record_evidence` resolves every `source_refs` entry against the
    ledger and raises `NotFound` on one that is not there, so a claim cannot
    cite a source nobody registered. And a reference that came from another
    session is refused too, which is the half that matters most: those are the
    references a model would have seen and could plausibly replay.
    """
    environment = RoleEnvironment(settings=integration_settings, brief_dir=tmp_path / "briefs")
    env = environment.for_project(project, AgentRole.RESEARCH)
    node_id = research_task.node_id

    attempts = await probe(
        env,
        calls=(
            # A reference shaped like the ones the server issues, and issued by
            # nobody. This is the fabrication a model would attempt first.
            (
                "register_source",
                {"retrieval_ref": "retrieval:0000000000000000", "node_id": node_id},
            ),
            # The same URL and title RAVEL would read, supplied directly. There
            # is no argument here that accepts either, and that is the point:
            # the tool's language has no sentence for "I have read this".
            (
                "register_source",
                {
                    "url": "https://doi.org/10.9999/ravel.fabricated",
                    "title": "Conductivity of niobium-doped titania",
                    "content_hash": "sha256:" + "0" * 64,
                    "node_id": node_id,
                },
            ),
            # A claim citing a source that is not in the ledger.
            (
                "record_evidence",
                {
                    "node_id": node_id,
                    "statement": "Doping raises conductivity by 22%.",
                    "claim_class": "FACT",
                    "source_refs": ["src-0000000000000000"],
                },
            ),
        ),
    )

    for index, what in enumerate(
        ("a made-up retrieval reference", "a stated URL and hash", "a claim citing nothing")
    ):
        assert attempts.calls[index].failed, (
            f"the server accepted {what}, which is a source RAVEL never read"
        )

    with database.read_only() as session:
        ledger = EvidenceSourceRepository(session, project.project_id).all()
    assert ledger == [], (
        f"{len(ledger)} row(s) entered the ledger from attempts that read nothing. "
        "The ledger is the one place a source is written, and a source in it is a "
        "statement that RAVEL read something."
    )


# ── Gate 3: mock data is marked, and the mark is enforced ───────────────────


def test_gate_3_every_scenario_a_mock_can_play_marks_what_it_produces(
    database: Database,
    artifact_store: S3ArtifactStore,
    prepare: Callable[..., Prepared],
    compute: Callable[..., MockComputeBackend],
    lab: Callable[..., MockLabBackend],
    mock_clock: FakeClock,
) -> None:
    """*mock compute/lab data must be explicitly marked simulated.*

    Every scenario in `acceptance/MOCK_SCENARIOS.yaml`, not the two a
    hand-written test happens to name. The sweep is over the catalogue because
    the catalogue is what a future scenario is added to, and a marking rule
    that held for the scenarios somebody remembered to test would be a rule
    about those two.

    A scenario that waits for a signal is skipped — it produces nothing until
    something outside RAVEL answers it, which is A10's subject — and the count
    of scenarios that did produce artifacts is asserted against a floor, so a
    catalogue that stopped delivering anything cannot turn this into a test
    that ran ten loops and examined nothing.

    A second test carries the half that makes the marker mean something: the
    ledger refuses a source resting on marked bytes. A marker nothing reads is
    a comment.
    """
    scenarios: Catalogue = catalogue()
    produced: set[str] = set()

    for scenario_id in sorted(scenarios.experiment):
        scenario = scenarios.lab_scenario(scenario_id)
        if scenario.waits_for_signal:
            continue
        _sweep(
            database,
            prepare(
                node_type=NodeType.EXPERIMENT,
                required_outputs=("experiment_log", "raw_data"),
            ),
            lab(scenario_id),
            "mock-lab",
            scenario_id,
            mock_clock,
            produced,
        )

    for scenario_id in sorted(scenarios.compute):
        _sweep(
            database,
            # Two outputs rather than one, so that the scenario that delivers an
            # incomplete result delivers *something*: the mock drops the last
            # required output, and a node that owes one file would owe nothing.
            prepare(
                node_type=NodeType.COMPUTATION,
                required_outputs=("conductivity.csv", "notes.json"),
            ),
            compute(scenario_id),
            "mock-compute",
            scenario_id,
            mock_clock,
            produced,
        )

    assert produced == PRODUCING_SCENARIOS, (
        f"the scenarios that delivered something are {sorted(produced)} and this "
        f"gate expects {sorted(PRODUCING_SCENARIOS)}"
    )


def _sweep(
    database: Database,
    prepared: Prepared,
    backend: MockComputeBackend | MockLabBackend,
    backend_name: str,
    scenario_id: str,
    clock: FakeClock,
    produced: set[str],
) -> None:
    """Run one scenario to its end and check the mark on everything it wrote.

    A scenario that owes nothing writes nothing: a failed run, and a run whose
    lab has not answered yet, both deliver no outputs, and `collect` writes a
    file per delivered output rather than one per required output. Those are
    recorded in `produced` as absent, so that the caller can assert the set
    rather than a count.
    """
    handle = backend.submit(prepared.request())
    for _ in range(20):
        if backend.status(handle.backend_job_ref).state.is_terminal:
            break
        clock.advance(1.0)
    outputs = backend.collect(handle.backend_job_ref)
    if not outputs.artifacts:
        return
    produced.add(scenario_id)

    with database.read_only() as session:
        rows = list(
            session.execute(
                select(ArtifactRow).where(
                    ArtifactRow.project_id == prepared.project_id,
                    ArtifactRow.artifact_id.in_(outputs.artifacts),
                )
            ).scalars()
        )

    assert len(rows) == len(outputs.artifacts), (
        f"{scenario_id} produced {len(outputs.artifacts)} artifact(s) and "
        f"{len(rows)} rows are readable back"
    )
    for row in rows:
        assert is_simulated(row.kind), (
            f"artifact {row.name!r} came out of {scenario_id} with kind {row.kind!r}; "
            "simulated output that is not marked is indistinguishable from a "
            "measurement"
        )
        assert row.provenance == simulated_provenance(backend_name, scenario_id), (
            f"the mark on {row.name!r} says {row.provenance!r}, which does not name "
            "the backend and the scenario a reader would need to find out what was "
            "simulated"
        )


def test_gate_3_the_ledger_refuses_a_source_resting_on_simulated_bytes(
    database: Database,
    artifact_store: S3ArtifactStore,
    prepare: Callable[..., Prepared],
    compute: Callable[..., MockComputeBackend],
    mock_clock: FakeClock,
) -> None:
    """The marker, read by something that acts on it.

    Both directions, because a guard that refused everything would pass the
    first half: a source resting on a simulated artifact raises, and the same
    source resting on a real one is stored. The artifact that succeeds is
    registered by hand with no `kind`, which is what a real fetch produces.
    """
    prepared = prepare(
        node_type=NodeType.COMPUTATION, required_outputs=("conductivity.csv",)
    )
    backend = compute("COMPUTE_SUCCESS")
    handle = backend.submit(prepared.request())
    for _ in range(20):
        if backend.status(handle.backend_job_ref).state.is_terminal:
            break
        mock_clock.advance(1.0)
    simulated = backend.collect(handle.backend_job_ref).artifacts
    assert simulated, "the scenario produced nothing, so the refusal is untested"

    with database.transaction() as session:
        repository = EvidenceSourceRepository(session, prepared.project_id)
        with pytest.raises(SimulatedEvidenceError) as refused:
            repository.record(
                _source(prepared.project_id, "https://example.invalid/sim.csv", simulated[0])
            )
    assert simulated[0] in str(refused.value), (
        "the refusal does not say which artifact it is about, so a reader cannot "
        "go and look at it"
    )
    assert "simulated" in str(refused.value)

    with database.transaction() as session:
        measured, _ = ArtifactRepository(session, prepared.project_id, artifact_store).register(
            name="measured.csv",
            chunks=[b"two theta, intensity\n0.1, 41.2\n"],
            created_by="research-worker",
            filename="measured.csv",
        )
    with database.transaction() as session:
        stored = EvidenceSourceRepository(session, prepared.project_id).record(
            _source(
                prepared.project_id,
                "https://example.invalid/measured.csv",
                measured.artifact_id,
            )
        )
    assert stored.source_id


def _source(project_id: str, url: str, artifact_ref: str) -> EvidenceSource:
    """One source row resting on an artifact, for the guard to judge."""
    return EvidenceSource(
        project_id=project_id,
        url=url,
        title="A series",
        access_status=AccessStatus.OK,
        retrieved_at=utcnow(),
        content_hash="sha256:" + "0" * 64,
        artifact_ref=artifact_ref,
    )


# ── Gate 4: DSH must be pinned ──────────────────────────────────────────────


def test_gate_4_the_harness_running_here_is_the_one_the_pin_names() -> None:
    """*DSH must be pinned.*

    Checked against three independent things rather than against the pin file
    alone, because a pin file is a document and this gate is about what is
    installed. The pin must name a harness, a tag and a commit; the *installed*
    distributions must be exactly the versions it names; and the bundled
    runtime, asked directly, must report the tag. The vendored checkout — the
    tree the pin file points at — is checked against the commit it names, when
    it is there to check: it is git-ignored and re-fetched on demand, so its
    absence is a note in the matrix rather than a failure, and its *presence*
    at another commit is a failure.

    `patches.required` is asserted false as well. That is the field whose being
    true would mean this is not the release that was verified, and it is the
    one thing `docs/15_AUTONOMOUS_DEVELOPMENT_CONTRACT.md` forbids outright: a
    fork of a harness still in Developer Preview is a project whose next
    upstream change is a rewrite.
    """
    pin = _pin()

    assert pin["harness"] == "DeepSeek Harness"
    assert pin["pin"]["tag"] and pin["pin"]["commit"], "the pin names no release"
    assert pin["patches"]["required"] is False, (
        "the pin says a patch is required, so the harness RAVEL runs is not the "
        "one that was verified"
    )
    assert set(DISTRIBUTIONS) <= set(pin["python_distributions"]), (
        "the pin no longer names both distributions this gate checks"
    )

    for distribution in DISTRIBUTIONS:
        installed = version(distribution)
        pinned = str(pin["python_distributions"][distribution])
        assert installed == pinned, (
            f"{distribution} {installed} is installed and the pin names {pinned}. "
            "A harness that drifted off the pin is a harness nobody verified."
        )

    # The runtime reports a bare version and the tag carries the `dsh-v`
    # prefix, so the two are tied by asserting the prefix rather than by
    # trimming one of them into agreement with the other.
    reported = _runtime_version()
    expected = str(pin["pin"]["runtime_self_reported_version"])
    assert reported == expected, (
        f"the bundled runtime reports {reported!r} and the pin names {expected!r}"
    )
    assert pin["pin"]["tag"] == f"dsh-v{expected}", (
        f"the pin's tag {pin['pin']['tag']!r} is not the release the runtime "
        f"reports ({expected!r})"
    )

    # The failure the pin records has to stay recorded. This is a gate about
    # honest reporting as much as about versions: the day the FAIL disappears
    # from the pin file without the limitation being fixed, the limitation has
    # been deleted rather than resolved.
    checks = json.dumps(pin["verification"]["checks"])
    assert "FAIL" in checks and "resume" in checks, (
        "the pin's verification record no longer carries the cross-process resume "
        "limitation it was written with; see vendor/DSH_PATCHES.md and KNOWN_LIMITATIONS.md"
    )

    assert BASE_PROFILE == "sdk-minimal", (
        f"the roles boot from {BASE_PROFILE!r}, which is not the profile the pin "
        "was verified against"
    )

    head = _vendored_head()
    if head is None:
        pytest.skip(
            "vendor/deepseek-harness is not checked out here. It is git-ignored and "
            "re-fetched from the pinned commit, so this half of the gate has "
            "nothing to compare against; the installed distributions and the "
            "runtime's own version report have already been checked above."
        )
    assert head == pin["pin"]["commit"], (
        f"the vendored checkout is at {head} and the pin names {pin['pin']['commit']}"
    )


def _runtime_version() -> str:
    """Ask the bundled harness runtime which release it is.

    Through `--version` rather than through a Python attribute, because the
    version a runtime *reports* is the version a deployment logs, and a wheel
    whose metadata said one thing while its binary said another is exactly the
    drift this gate exists to catch. It is given a `DSH_HOME` of its own, since
    a runtime with nowhere to put its state is entitled to refuse to answer.
    """
    import deepseek_harness_runtime

    bundled = Path(deepseek_harness_runtime.__file__).parent / "runtime"
    binaries = [
        binary
        for binary in sorted(bundled.glob("deepseek-harness-sdk-runtime-*"))
        if not binary.name.endswith("-rg")
    ]
    assert binaries, f"no harness runtime binary is bundled at {bundled}"
    with tempfile.TemporaryDirectory() as home:
        completed = subprocess.run(
            [str(binaries[0]), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            env={**os.environ, "DSH_HOME": home},
        )
    return completed.stdout.strip()


def _vendored_head() -> str | None:
    """The commit the vendored checkout is at, or None when it is not there."""
    if not (VENDORED_TREE / ".git").exists():
        return None
    completed = subprocess.run(
        ["git", "-C", str(VENDORED_TREE), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return completed.stdout.strip()


# ── Gate 5: all five roles use the intended preset and tool scope ───────────


async def test_gate_5_every_role_boots_with_its_own_contract_and_no_other_roster(
    project: Project, integration_settings: Settings, tmp_path: Path
) -> None:
    """*all 5 roles use intended DSH preset/tool scope.*

    Two halves, and the split is the harness seam. What a role *boots with* is
    the overlay RAVEL generates: the role's own behavioral contract as the
    system prompt, the shell removed from the model's view, and exactly one MCP
    server mounted with the project and role it serves. What a role can
    *reach* is what its server registers, which is read off the wire by
    launching the same `python -m ravel.mcp.server` the harness launches.

    The five are asserted one at a time rather than as a set, because "the
    rosters differ" is not the claim — "each role has the roster its contract
    implies" is. And exactly one of them holds a tool that changes the DAG,
    which is checked here over the wire and not in the table: the table is
    where the grant is written, and this is where it is observed.
    """
    project_id = project.project_id
    personas: dict[AgentRole, str] = {}

    assert set(ROLE_DEFINITIONS) == set(AgentRole), (
        "the roles RAVEL composes and the roles the domain fixes have diverged"
    )

    for role in AgentRole:
        definition = definition_for(role)
        composition = _composition(role, project_id, tmp_path)
        patch = composition.as_patch()

        assert definition.prompt_file.is_file(), f"{role.value} has no behavioral contract"
        # The file is named for the role it belongs to, underscores rather than
        # the hyphens the role's value carries. A role booting from another
        # role's contract is the failure this is here for, and a name is the
        # only thing that says whose a file is.
        assert definition.prompt_file.name == f"{role.value.replace('-', '_')}_role.md", (
            f"{role.value} boots from {definition.prompt_file.name}"
        )
        assert definition.mcp_module == "ravel.mcp.server"

        system = _entry(patch, "system-prompt")["config"]
        assert isinstance(system, dict)
        assert system["personaPrefix"] == definition.persona
        assert system["includeHarnessIdentity"] is False, (
            "the agent is told it is inside a harness, which is RAVEL's business "
            "and not the role's"
        )
        assert system["includeRuntimeContext"] is False

        for shell in ("persistent-bash", "persistent-pwsh"):
            assert _entry(patch, shell)["disabled"] is True, (
                f"{role.value} can still reach a shell, so its tools are not the "
                "whole of what it may do"
            )

        server = _mounted_server(patch)
        config = server["config"]
        assert server["id"] == "ravel-tools", f"{role.value} mounts {server['id']!r}"
        assert config["serverName"] == MCP_SERVER_NAME
        assert config["transport"] == "stdio"
        assert config["failOnStartupError"] is True, (
            "a tool server that failed to start would leave the agent with no "
            "tools and no error"
        )
        assert config["env"]["RAVEL_PROJECT_ID"] == project_id, (
            f"{role.value} is launched against {config['env']['RAVEL_PROJECT_ID']!r}"
        )
        assert config["env"]["RAVEL_ROLE"] == role.value
        assert composition.tools == tools_for(role)
        assert composition.as_dump()["dag_mutation_tools"] == list(
            tool for tool in tools_for(role) if tool in DAG_MUTATION_TOOLS
        )

        personas[role] = definition.persona

    assert len(set(personas.values())) == len(AgentRole), (
        "two roles boot with the same behavioral contract, so one of them is "
        "being asked to do another's job"
    )
    assert all(len(persona) > 500 for persona in personas.values()), (
        "a role's contract is too short to be one: "
        + str({role.value: len(persona) for role, persona in personas.items()})
    )

    environment = RoleEnvironment(settings=integration_settings, brief_dir=tmp_path / "briefs")
    holders: list[str] = []
    for role in AgentRole:
        observed = await probe(environment.for_project(project, role))
        assert set(observed.tools) == set(tools_for(role)), (
            f"{role.value}'s server registered {sorted(observed.tools)} and its "
            f"grant is {sorted(tools_for(role))}"
        )
        assert observed.whoami is not None
        assert observed.whoami["role"] == role.value
        assert observed.whoami["project_id"] == project_id
        assert observed.whoami["may_mutate_dag"] is (role is AgentRole.MASTER), (
            f"{role.value} reports may_mutate_dag={observed.whoami['may_mutate_dag']}"
        )
        if set(observed.tools) & DAG_MUTATION_TOOLS:
            holders.append(role.value)

    assert holders == [AgentRole.MASTER.value], (
        f"the roles holding a DAG mutation are {holders}; the plan is Master's to "
        "change and nobody else's"
    )


# ── Gate 6: no direct public DSH endpoint ───────────────────────────────────


def test_gate_6_the_harness_is_reached_through_ravel_and_never_served(
    live_gateway: LiveGateway, tmp_path: Path
) -> None:
    """*no direct public DSH endpoint.*

    Four ways the harness could become publicly reachable, each ruled out
    where it would happen. The Gateway's route table is enumerated through
    `effective_routes`, which is the only honest way to ask a FastAPI
    application what it serves — a loop over `app.routes` finds the docs and
    nothing else, and passes. No route module imports the harness package, so
    there is no code path from a request to a session. The transport RAVEL
    composes is stdio with no address, so nothing listens. And the model the
    Gateway hands a caller carries no session identifier: a harness handle that
    left the process would be a caller able to use it as one, which is what
    `docs/09` forbids.

    The administrator's health read survives all four because it is a *read of
    the pin* — what version is running — and not a way to reach a session. It
    is named here rather than described, so a second route touching the harness
    has to be an edit to this file.
    """
    routes = effective_routes(live_gateway.app)
    assert len(routes) >= EXPECTED_ROUTE_FLOOR, (
        f"the enumeration found {len(routes)} routes, which is fewer than the "
        "floor — it has broken rather than the application having shrunk"
    )

    paths = {route.path for route in routes}
    assert HARNESS_HEALTH_PATH in paths, (
        "the harness health read is gone, so this gate no longer knows which route "
        "it is making an exception for"
    )
    for word in _HARNESS_WORDS:
        named = {path for path in paths if word in path}
        assert named <= {HARNESS_HEALTH_PATH}, (
            f"the Gateway serves {sorted(named - {HARNESS_HEALTH_PATH})}, which "
            f"names the harness ({word})"
        )

    health = next(route for route in routes if route.path == HARNESS_HEALTH_PATH)
    assert health.methods == frozenset({"GET"}), (
        f"the harness health read answers {sorted(health.methods)}; a write here "
        "would be a way to act on a runtime from outside RAVEL"
    )

    imports = _module_imports(REPO_ROOT / "src" / "ravel" / "gateway" / "routes")
    assert imports, "the scan found no route modules, so it checked nothing"
    reachable = [
        (path, module)
        for path, module in imports
        if module == "ravel.dsh" or module.startswith("ravel.dsh.")
    ]
    assert reachable == [], (
        f"a route module imports the harness directly: {reachable}. Every arrow to "
        "a session goes through RAVEL, which is what makes the authority check "
        "unskippable."
    )

    # The transport: stdio, with no address for anything to connect to.
    patch = _composition(AgentRole.MASTER, "prj-gate", tmp_path).as_patch()
    config = _mounted_server(patch)["config"]
    assert config["transport"] == "stdio"
    assert "command" in config and config["args"] == ["-m", "ravel.mcp.server"]
    assert not {"host", "port", "url", "endpoint", "listen"} & set(config), (
        f"the tool server is configured with an address: {sorted(config)}"
    )

    # And the harness's own configuration, which RAVEL builds per scope.
    names = {field.name for field in fields(DeepSeekHarnessConfig)}
    assert not names & {"host", "port", "bind", "listen", "address"}, (
        f"the harness is configured to serve somewhere: {sorted(names)}"
    )

    assert set(Turn.model_fields) == {"turn_id", "question", "answer"}, (
        "the Gateway's conversation reply carries something other than the "
        f"exchange: {sorted(Turn.model_fields)}. A session identifier here is a "
        "handle a caller could use as one."
    )


# ── Gate 7: Postgres remains authoritative after restarts ───────────────────


def test_gate_7_the_record_outlives_the_process_that_read_it(
    database: Database,
    integration_settings: Settings,
    prepare: Callable[..., Prepared],
    project: Project,
) -> None:
    """*Postgres remains authoritative after restarts.*

    A restart is taken literally: every pooled connection is closed and a new
    `Database` — a new engine, a new pool, new sessions — is built from
    settings, which is what a process coming back up does. The state read
    before and after is compared field by field, and it is state that was
    *written by the real repositories* rather than by hand, so the comparison
    is about the record and not about a fixture.

    The second half is the claim underneath it. A record read out of
    PostgreSQL and edited in memory does not change the store: the copy is a
    value, and the row is the fact. A system where the two could be confused is
    one where the last process to hold an object decides what the project is,
    which is exactly the failure "authoritative state > session context" is
    written against. A15 and A16 carry the same claim from the two other sides
    — a dead Master session, and a killed Temporal worker.
    """
    prepared = prepare(node_type=NodeType.COMPUTATION, required_outputs=("conductivity.csv",))
    before = _state(database, project.project_id, prepared.node_id)
    assert before["status"] == "READY", "the node this reads was not built by prepare()"

    database.dispose()
    revived = Database.from_settings(integration_settings)
    assert revived is not database
    assert revived.engine is not database.engine, (
        "the second Database shares the first one's engine, so nothing was restarted"
    )

    after = _state(revived, project.project_id, prepared.node_id)
    assert after == before, (
        "the project reads differently through a process that just started, so "
        "something authoritative was living in the old one"
    )
    assert after["event_seq"] > 0, "the event stream an audit trail is built on is empty"

    # An edit to the copy is not an edit to the project.
    with revived.read_only() as session:
        read = ProjectRegistry(session).get(project.project_id)
    edited = read.model_copy(update={"title": "Something the process decided"})
    assert edited.title != read.title
    with revived.read_only() as session:
        assert ProjectRegistry(session).get(project.project_id).title == read.title, (
            "a value held in memory changed what PostgreSQL says the project is"
        )


def _state(database: Database, project_id: str, node_id: str) -> dict[str, Any]:
    """Everything about one node that a restart has to preserve."""
    with database.read_only() as session:
        node = DagRepository(session, project_id).node(node_id)
        contract = ExecutionContractRepository(session, project_id).for_node(node_id)
        return {
            "project_status": ProjectRegistry(session).get(project_id).status.value,
            "project_title": ProjectRegistry(session).get(project_id).title,
            "status": node.status.value,
            "objective": node.objective,
            "dependencies": sorted(node.dependencies),
            "contract_version": contract.version,
            "contract_id": contract.contract_id,
            "allowed_actions": sorted(contract.allowed_actions),
            "required_outputs": sorted(contract.required_outputs),
            "event_seq": last_event_seq(session, project_id),
        }
