"""How a contract's environment requirement reaches the thing that builds it.

The registry is the only place that decides whether a contract can be prepared
at all, and every way that decision can go is here: an environment this
deployment has, one it does not, one whose *kind* is a misspelling of a kind it
does have, and a contract that asks for two environments at once. The last two
are the ones a name-only registry would get wrong, so they are tested as
themselves rather than as variations on the first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from ravel.domain.contracts import ExecutionContract
from ravel.domain.preparation import PreparationRefusal
from ravel.preparation import (
    MaterializationRefused,
    MaterializerRegistry,
    PreparationContext,
    PreparedExecution,
    PreparedInput,
)


@dataclass
class RecordingMaterializer:
    """A materializer that builds nothing and remembers being asked.

    A unit test's stand-in for the real thing: what is under test here is which
    materializer a contract reaches, and a real one would put its own behaviour
    between the question and the answer. The bytes a real materializer writes
    are tested against that materializer, not this one.
    """

    kind: str = "software"
    name: str = "example"
    version: str = "1"
    asked: list[PreparationContext] | None = None

    def materialize(self, context: PreparationContext) -> PreparedExecution:
        if self.asked is None:
            self.asked = []
        self.asked.append(context)
        return PreparedExecution(
            workspace_path=str(context.workspace_root),
            materializer=self.name,
            materializer_version=self.version,
            files=(),
            manifest={"materializer": self.name},
        )


def a_contract(**overrides: object) -> ExecutionContract:
    fields: dict[str, object] = {
        "project_id": "proj-a",
        "node_id": "n1",
        "objective": "Simulate the adsorption isotherm.",
        "execution_requirements": {"software": "example"},
    }
    fields.update(overrides)
    return ExecutionContract(**fields).freeze()  # type: ignore[arg-type]


def a_context(root: Path, **overrides: object) -> PreparationContext:
    return PreparationContext(
        project_id="proj-a",
        node_id="n1",
        node_display_id="N-1A2B3C4D",
        contract=a_contract(**overrides),
        workspace_root=root,
        inputs=(PreparedInput(name="framework.cif", data=b"", source="artifact:v1"),),
    )


def test_a_materializer_is_found_by_the_environment_it_names() -> None:
    registry = MaterializerRegistry()
    registered = RecordingMaterializer()
    registry.register(registered)

    assert registry.resolve("software", "example") is registered
    assert registry.resolve("software", "raspa") is None


def test_the_kind_is_part_of_the_key(tmp_path: Path) -> None:
    """The same name under another kind is another environment.

    `bench-chemistry` is a lab package a person assembles; a software package of
    the same name is a program somebody installs. A registry keyed on the name
    alone would hand a lab requirement to the software materializer, and the
    manifest it wrote would say the work was prepared when the bench had never
    been told anything.
    """
    registry = MaterializerRegistry()
    registry.register(RecordingMaterializer(kind="software", name="bench"))

    assert registry.resolve("software", "bench") is not None
    assert registry.resolve("lab", "bench") is None

    with pytest.raises(MaterializationRefused) as refused:
        registry.materialize(a_context(tmp_path, execution_requirements={"lab": "bench"}))
    assert refused.value.refusal is PreparationRefusal.UNSUPPORTED_ENVIRONMENT
    assert "lab/bench" in str(refused.value)


def test_one_environment_is_built_by_one_materializer() -> None:
    """Two would make which code wrote a workspace depend on registration order."""
    registry = MaterializerRegistry()
    registry.register(RecordingMaterializer())
    with pytest.raises(ValueError) as refused:
        registry.register(RecordingMaterializer())
    assert "software/example" in str(refused.value)


def test_the_environments_are_named_the_way_a_refusal_names_them() -> None:
    """Sorted and spelled `kind/name`, because this is read in a sentence."""
    registry = MaterializerRegistry()
    registry.register(RecordingMaterializer(kind="software", name="raspa"))
    registry.register(RecordingMaterializer(kind="lab", name="bench-chemistry"))
    assert registry.names() == ("lab/bench-chemistry", "software/raspa")


def test_a_contract_with_no_environment_is_not_prepared(tmp_path: Path) -> None:
    """Asking is a misreading of the contract, so it is an error rather than a refusal.

    A refusal would be recorded as a fact about a contract and put in front of
    Master; this is a caller that did not ask `requires_preparation` before
    asking for preparation, and the answer is to fix the caller.
    """
    registry = MaterializerRegistry()
    registry.register(RecordingMaterializer())
    with pytest.raises(ValueError) as refused:
        registry.materialize(a_context(tmp_path, execution_requirements={}))
    assert "names no environment" in str(refused.value)


def test_a_contract_naming_two_environments_is_refused(tmp_path: Path) -> None:
    """One run, one workspace — and nothing is dropped silently.

    A workspace holds what one machine or one bench is given. A contract asking
    for both is work that needs two nodes, and preparing one of them while
    saying nothing about the other would be RAVEL running part of a plan and
    recording it as the whole of one.
    """
    registry = MaterializerRegistry()
    registry.register(RecordingMaterializer())
    registry.register(RecordingMaterializer(kind="lab", name="bench-chemistry"))

    with pytest.raises(MaterializationRefused) as refused:
        registry.materialize(
            a_context(
                tmp_path,
                execution_requirements={"software": "example", "lab": "bench-chemistry"},
            )
        )

    assert refused.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "software/example" in str(refused.value)
    assert "lab/bench-chemistry" in str(refused.value)


def test_a_contract_reaches_the_materializer_named_for_it(tmp_path: Path) -> None:
    """The context is passed through unchanged, contract and inputs together."""
    registry = MaterializerRegistry()
    registered = RecordingMaterializer()
    registry.register(registered)
    context = a_context(tmp_path)

    prepared = registry.materialize(context)

    assert registered.asked == [context]
    assert prepared.materializer == "example"
    assert prepared.manifest == {"materializer": "example"}


def test_an_unknown_environment_names_the_ones_that_exist(tmp_path: Path) -> None:
    """Master reads the refusal and chooses; a list it can choose from is the point."""
    registry = MaterializerRegistry()
    registry.register(RecordingMaterializer(kind="software", name="raspa"))

    with pytest.raises(MaterializationRefused) as refused:
        registry.materialize(
            a_context(tmp_path, execution_requirements={"software": "vasp"})
        )

    assert refused.value.refusal is PreparationRefusal.UNSUPPORTED_ENVIRONMENT
    message = str(refused.value)
    assert "software/vasp" in message
    assert "software/raspa" in message


def test_a_deployment_with_no_materializers_says_so(tmp_path: Path) -> None:
    """`none` rather than an empty list, because it is read in a sentence."""
    with pytest.raises(MaterializationRefused) as refused:
        MaterializerRegistry().materialize(a_context(tmp_path))
    assert "are none" in str(refused.value)
