"""The materializers a deployment has, and how a contract reaches one.

A contract says what its work needs in the contract's own vocabulary —
`{"software": "raspa"}` for a calculation, `{"lab": "bench-chemistry"}` for
something a person does at a bench — and preparation is what turns that
sentence into a directory. Between the two sits this registry, and it is small
on purpose: it answers one question, "does this deployment have something that
can build that", and it answers by lookup rather than by trying and seeing.

**Why a materializer is keyed by kind *and* name.** The kind is the closed half
of the vocabulary — the contract model refuses a requirement of an unknown kind
before a contract can exist — and the name is the half that is a deployment
fact: RAVEL knows how to prepare a `software` environment, and which software
this installation can actually build one for is a question only the host
answers. Keying on the name alone would let `bench-chemistry`, spelled as a
software package by mistake, be served by a software materializer; keying on
the kind alone would say a single materializer could prepare every package,
which is the claim that makes the manifest meaningless.

**Why a missing materializer is a refusal and not an exception.** Master wrote
the requirement, so a contract naming an environment nobody can build is a
thing Master can act on — choose another environment, or decide the node does
not run. `UNSUPPORTED_ENVIRONMENT` is that answer, and it is different from
`ENVIRONMENT_UNAVAILABLE`, which is what a materializer that *does* exist says
when the host it is running on cannot do its job. The two are a different next
step for Master — one is about the plan, the other about the machine — which is
why both are in the closed list rather than one "cannot prepare" member.

**Registering the same environment twice is refused.** A second materializer
under one key would be a deployment where which code wrote a workspace depends
on the order two lines ran in, and the manifest records one version. A
deployment that wants a different materializer for the same environment builds
a registry that has that one in it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ravel.domain.preparation import PreparationRefusal
from ravel.preparation.workspace import (
    MaterializationRefused,
    PreparationContext,
    PreparedExecution,
)

__all__ = ["Materializer", "MaterializerRegistry"]


@runtime_checkable
class Materializer(Protocol):
    """One kind of environment RAVEL knows how to build.

    Four things, and each is here for a reader rather than for the machinery:
    `kind` and `name` are what a contract's requirement is matched against,
    `version` is what the manifest records so that a workspace can be told
    apart from one a later build of the same materializer would write, and
    `materialize` is the work.

    A materializer is not an agent and has no session. It reads the contract it
    is handed, writes files into a directory, and either returns what it built
    or raises `MaterializationRefused`. It may not decide anything the contract
    did not state — the temperature, the method, the sample count, which
    parameter to vary — because those are scientific choices and no role has
    delegated them to software.
    """

    # Read-only, and declared as properties rather than as attributes so that a
    # frozen dataclass satisfies the protocol: what a materializer *is* cannot
    # change between the registry looking it up and the manifest recording it.

    @property
    def kind(self) -> str:
        """Which half of the requirement vocabulary this serves.

        `"software"` or `"lab"` — the same closed list `ExecutionContract`
        validates a requirement's key against.
        """
        ...

    @property
    def name(self) -> str:
        """The environment's own name, as a contract spells it."""
        ...

    @property
    def version(self) -> str:
        """The materializer's own version. Bumped when what it writes changes."""
        ...

    def materialize(self, context: PreparationContext) -> PreparedExecution:
        """Build the environment this contract names.

        Raises:
            MaterializationRefused: The contract does not say enough, says
                something contradictory, or names something this host cannot
                build. The class and the sentence are what Master reads.
        """
        ...


@dataclass(frozen=True)
class MaterializerRegistry:
    """Which environments this deployment can build, and how to reach one.

    Explicit rather than discovered, like the backend registry: what a
    deployment can prepare is a deployment decision, and a registry that
    resolved it by import side effect would make the answer depend on what
    happened to be imported.
    """

    by_kind_and_name: dict[tuple[str, str], Materializer] = field(default_factory=dict)

    def register(self, materializer: Materializer) -> None:
        """Add a materializer for the environment it names.

        Raises:
            ValueError: This environment already has one. Two materializers
                under one key is a deployment where which code wrote a
                workspace depends on the order registrations ran in.
        """
        key = (materializer.kind, materializer.name)
        existing = self.by_kind_and_name.get(key)
        if existing is not None:
            raise ValueError(
                f"{key[0]}/{key[1]} already has a materializer "
                f"({existing!r}); one environment is built by one materializer, "
                "and a deployment that wants a different one builds a registry "
                "that has it instead"
            )
        self.by_kind_and_name[key] = materializer

    def resolve(self, kind: str, name: str) -> Materializer | None:
        """The materializer for one environment, or `None` if there is none."""
        return self.by_kind_and_name.get((kind, name))

    def names(self) -> tuple[str, ...]:
        """Every environment this deployment can build, as `kind/name`.

        Sorted, because the first thing a reader does with this is read it in
        a refusal sentence, and a list that comes out in a different order each
        process is a list nobody can diff.
        """
        return tuple(sorted(f"{kind}/{name}" for kind, name in self.by_kind_and_name))

    def materialize(self, context: PreparationContext) -> PreparedExecution:
        """Build the environment this contract names, or refuse to.

        The contract's requirement is a one-entry mapping in the ordinary case,
        and this is where that shape is checked rather than assumed: a contract
        that names two environments is a contract for two runs, and RAVEL
        prepares one workspace per run. Nothing is dropped silently — a
        requirement RAVEL quietly ignored would be work the plan said to do
        that no workspace was built for.

        Raises:
            ValueError: The contract requires no preparation, so nothing was
                asked for. A caller that reached here has misread the contract;
                `ExecutionContract.requires_preparation` is the question it
                should have asked first.
            MaterializationRefused: Nothing can build this environment, or the
                materializer that can brought its own refusal.
        """
        requirements = context.contract.execution_requirements
        if not requirements:
            raise ValueError(
                f"contract {context.contract.contract_id} names no environment, "
                "so there is nothing to prepare; a contract that requires no "
                "preparation is one no workspace is built for"
            )
        if len(requirements) > 1:
            named = ", ".join(
                f"{kind}/{name}" for kind, name in sorted(requirements.items())
            )
            raise MaterializationRefused(
                PreparationRefusal.INCONSISTENT_CONTRACT,
                f"the contract names {named}, and a run happens in one "
                "environment: a workspace holds the files one machine or one "
                "bench is given, so work that needs both is two nodes rather "
                "than one contract with two requirements",
            )
        ((kind, name),) = requirements.items()
        materializer = self.resolve(kind, name)
        if materializer is None:
            known = ", ".join(self.names()) or "none"
            raise MaterializationRefused(
                PreparationRefusal.UNSUPPORTED_ENVIRONMENT,
                f"the contract requires {kind}/{name} and this deployment has no "
                f"materializer for it; the environments it can prepare are "
                f"{known}",
            )
        return materializer.materialize(context)
