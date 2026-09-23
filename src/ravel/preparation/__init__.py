"""Execution preparation: the non-agent step between a contract and a run.

Master decides what is to be done and freezes the terms; a Worker executes
them; Review judges the result. Between the second and the third there was
nothing, and this package is that nothing filled in: something has to turn a
frozen contract into the directory a run happens in — the input files, the job
script, the manifest with their hashes — or refuse to, saying what was wrong
with the contract.

**Why this is not an agent.** The five roles are defined by what they decide,
and this layer decides nothing. It may read a contract, check it for
completeness, generate files, hash them, and refuse; it may not invent a
parameter, choose a method, change a temperature, add a control group, or touch
the DAG. When a contract does not say enough to be materialized, the answer is
a refusal — `MaterializationRefused` — which is recorded and answered by
Master, the role that holds the decision. The prohibition is structural rather
than a matter of instruction: nothing here has a session, a project scope, or
the ability to write to the DAG.

The pieces:

- `Workspace` — the directory, written file by file and hashed as it goes.
- `PreparedFile`, `PreparedExecution` — what a materializer built.
- `PreparationContext`, `PreparedInput` — what it was given: the frozen
  contract, the inputs already resolved to bytes, and nowhere else to look.
- `build_manifest` — the document that makes the workspace traceable.
- `MaterializationRefused` — the refusal, carrying its class.
- `AcceptanceTerms` — the frozen criteria a delivery is measured against, which
  a package that tells somebody what to produce has to state.
- `MaterializerRegistry` — which environments this deployment can build, and
  the one entry point a contract reaches them through.
- `RaspaMaterializer` — the one real compute stack V0 prepares for.
- `LabMaterializer` — the bench package, which is what an EXPERIMENT run is
  handed: files a person reads, fills in, and works from.

The record of all this — `PreparationRecord`, and the row it is stored in —
lives in `ravel.domain.preparation` and `ravel.state.repositories.preparations`,
because what RAVEL built for a run is Project State and this package is only
the code that builds it.
"""

from ravel.preparation.lab import LabMaterializer
from ravel.preparation.raspa import REQUIRED_TERMS, RaspaMaterializer
from ravel.preparation.registry import Materializer, MaterializerRegistry
from ravel.preparation.workspace import (
    AcceptanceTerms,
    FileRole,
    MaterializationRefused,
    PreparationContext,
    PreparedExecution,
    PreparedFile,
    PreparedInput,
    Workspace,
    build_manifest,
)

__all__ = [
    "REQUIRED_TERMS",
    "AcceptanceTerms",
    "FileRole",
    "LabMaterializer",
    "MaterializationRefused",
    "Materializer",
    "MaterializerRegistry",
    "PreparationContext",
    "PreparedExecution",
    "PreparedFile",
    "PreparedInput",
    "RaspaMaterializer",
    "Workspace",
    "build_manifest",
]
