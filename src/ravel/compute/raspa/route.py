"""The one scientific software route T10 supports, stated as data.

RAVEL's V0 scientific claim is deliberately narrow: **gas adsorption by grand
canonical Monte Carlo (GCMC) in RASPA 2.0.x**. That is not a preference, it is
the whole supported surface. A contract that asks for DFT, molecular dynamics,
conductivity, a Gibbs ensemble, or any of RASPA's other simulation types is not
"almost supported"; it is a different calculation with different inputs,
different outputs, and different review criteria, and no amount of input
generation here would make it true.

The route is a record rather than a sentence in a docstring because two other
layers read it. The contract validator refuses a method this record does not
name, and the package manifest carries the record so that a reviewer can see
which route a result claims to belong to. A support matrix that exists only in
documentation is one that drifts from the code the first time the code changes.

**The real run is T25, and it is external.** This repository can prove that a
package is deterministic, that a parser classifies output correctly, and that
the rows which would carry a result line up item by item. It cannot prove that
a RASPA binary exists, that Slurm allocated it, or that a calculation ran. No
test in T10 claims that, and `real_run_status` below says so in the record
rather than in a comment.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

__all__ = [
    "REAL_RUN_BLOCKER",
    "REAL_RUN_STATUS",
    "REAL_RUN_TASK",
    "REAL_RUN_UNLOCK",
    "ROUTE_ID",
    "SUPPORTED_METHOD",
    "SUPPORT_MATRIX",
    "UNSUPPORTED_METHODS",
    "RaspaSupportMatrix",
]

#: The identifier the package manifest and the result document carry. Stable:
#: a result that says it came from this route can be found by this string.
ROUTE_ID = "raspa/gcmc-gas-adsorption"

#: The only method this route accepts, in the spelling a contract should use.
#: Accepted case-insensitively and stored canonically.
SUPPORTED_METHOD = "GCMC"

#: RASPA's version series, pinned by major and minor rather than by patch. The
#: input syntax used here is 2.0's; a 2.1 binary is a different program whose
#: keywords this package has not been verified against. The patch number is
#: recorded by the environment probe, not required to match, because patch
#: releases do not change the input language.
SUPPORTED_VERSION_SERIES = "2.0"

#: Methods a contract might reasonably ask for and this route does not do.
#: Listed so a refusal can say "not this route" by name instead of only "no".
UNSUPPORTED_METHODS: tuple[str, ...] = (
    "DFT",
    "MD",
    "MOLECULAR DYNAMICS",
    "MOLECULARDYNAMICS",
    "NVT",
    "NPT",
    "GIBBS",
    "CFCMC",
    "MC",
    "MONTECARLO",
    "CONDUCTIVITY",
    "PORE SIZE DISTRIBUTION",
    "SURFACE AREA",
)

REAL_RUN_TASK = "T25"
REAL_RUN_STATUS = "BLOCKED_EXTERNAL"
REAL_RUN_BLOCKER = (
    "No host with a RASPA 2.0.x binary and an authorized Slurm allocation is "
    "available to this task, and T10 is not the task that owns the real run."
)
REAL_RUN_UNLOCK = (
    "T25 needs a Slurm account authorized for the project, a RASPA 2.0.x "
    "binary whose sha256 is recorded by the environment probe, the bundled MIT "
    "reference data whose digests match, and an operator-authorized submission."
)


@dataclass(frozen=True, slots=True)
class RaspaSupportMatrix:
    """What this route does, and what it explicitly does not.

    `supported_property` and `supported_output` are singular on purpose: one
    property, one output. A route that claimed a family of properties would
    need a family of parsers, and the parser is where the claim becomes a
    result.
    """

    route_id: str
    software: str
    version_series: str
    method: str
    supported_property: str
    supported_output: str
    unsupported_methods: tuple[str, ...]
    real_run_task: str
    real_run_status: str
    real_run_blocker: str
    real_run_unlock: str
    evidence_scope: str

    def to_document(self) -> dict[str, object]:
        """The matrix as the manifest records it."""
        return {
            "route_id": self.route_id,
            "software": self.software,
            "version_series": self.version_series,
            "method": self.method,
            "supported_property": self.supported_property,
            "supported_output": self.supported_output,
            "unsupported_methods": list(self.unsupported_methods),
            "real_run_task": self.real_run_task,
            "real_run_status": self.real_run_status,
            "real_run_blocker": self.real_run_blocker,
            "real_run_unlock": self.real_run_unlock,
            "evidence_scope": self.evidence_scope,
        }


SUPPORT_MATRIX = RaspaSupportMatrix(
    route_id=ROUTE_ID,
    software="RASPA",
    version_series=SUPPORTED_VERSION_SERIES,
    method=SUPPORTED_METHOD,
    supported_property="gas adsorption isotherm point (absolute loading)",
    supported_output=(
        "RASPA output_<framework>_<cells>_<temperature>_<pressure>.data, parsed "
        "for average absolute loading in molecules/unit cell and mol/kg"
    ),
    unsupported_methods=UNSUPPORTED_METHODS,
    real_run_task=REAL_RUN_TASK,
    real_run_status=REAL_RUN_STATUS,
    real_run_blocker=REAL_RUN_BLOCKER,
    real_run_unlock=REAL_RUN_UNLOCK,
    evidence_scope=(
        "implementation and deterministic verification only; no real RASPA "
        "execution is claimed by T10"
    ),
)


def canonical_method(method: str) -> str:
    """The route's spelling of a method name, or the input uppercased.

    Normalisation is only about spelling. It does not make an unsupported
    method supported: the caller compares the result against `SUPPORTED_METHOD`.
    """
    return " ".join(method.strip().split()).upper()


def only_supported_route() -> RaspaSupportMatrix:
    """The matrix, as an immutable value.

    A function rather than a bare module constant would still be a constant;
    it exists so that a future route with a separate matrix does not have to
    reach into this module's globals.
    """
    return replace(SUPPORT_MATRIX)
