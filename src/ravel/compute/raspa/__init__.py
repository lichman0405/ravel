"""The first supported scientific software route: RASPA 2.0.x gas adsorption.

The package is deliberately small and split by question:

- `route` — what is supported, and what is explicitly not.
- `provenance` — which official files the package copies, with digests and
  licenses.
- `contract` — the canonical scientific terms read from a frozen contract.
- `package` — the immutable input package and its deterministic manifest.
- `environment` — whether a host may start the route at all.
- `parser` — the three outcomes a RASPA output file can have.
- `ledger` — following a result back to PostgreSQL, item by item.

Nothing here executes RASPA. The real run is T25 and is `BLOCKED_EXTERNAL`;
`route.SUPPORT_MATRIX` records that in the package manifest rather than only in
a document.
"""

from __future__ import annotations

from ravel.compute.raspa.contract import REQUIRED_TERMS, RaspaScientificContract
from ravel.compute.raspa.environment import (
    CommandResult,
    EnvironmentProbe,
    ProbeRefusal,
    RaspaStartRefused,
    probe_environment,
)
from ravel.compute.raspa.ledger import (
    RaspaLedgerTrace,
    RaspaTraceRefused,
    TraceItem,
    TraceRefusal,
    trace_result,
)
from ravel.compute.raspa.package import (
    CONVERGENCE_POLICY,
    MATERIALIZER,
    MATERIALIZER_VERSION,
    PACKAGE_SCHEMA,
    PARSER_SCHEMA_SHA256,
    RaspaInputPackage,
    build_input_package,
    verify_input_package,
)
from ravel.compute.raspa.parser import (
    Estimate,
    EvidenceClass,
    RaspaResult,
    ResultCheck,
    ResultStatus,
    parse_raspa_output,
)
from ravel.compute.raspa.provenance import (
    OFFICIAL_COMMIT,
    OFFICIAL_REPOSITORY,
    OFFICIAL_TAG,
    SOURCE_KEYS,
    SOURCE_RECORDS,
    BundledDataError,
    SourceKind,
    SourceRecord,
    read_bundled,
    source_record,
)
from ravel.compute.raspa.route import (
    REAL_RUN_BLOCKER,
    REAL_RUN_STATUS,
    REAL_RUN_TASK,
    REAL_RUN_UNLOCK,
    ROUTE_ID,
    SUPPORT_MATRIX,
    SUPPORTED_METHOD,
    SUPPORTED_VERSION_SERIES,
    RaspaSupportMatrix,
)

__all__ = [
    "CONVERGENCE_POLICY",
    "MATERIALIZER",
    "MATERIALIZER_VERSION",
    "OFFICIAL_COMMIT",
    "OFFICIAL_REPOSITORY",
    "OFFICIAL_TAG",
    "PACKAGE_SCHEMA",
    "PARSER_SCHEMA_SHA256",
    "REAL_RUN_BLOCKER",
    "REAL_RUN_STATUS",
    "REAL_RUN_TASK",
    "REAL_RUN_UNLOCK",
    "REQUIRED_TERMS",
    "ROUTE_ID",
    "SOURCE_KEYS",
    "SOURCE_RECORDS",
    "SUPPORTED_METHOD",
    "SUPPORTED_VERSION_SERIES",
    "SUPPORT_MATRIX",
    "BundledDataError",
    "CommandResult",
    "EnvironmentProbe",
    "Estimate",
    "EvidenceClass",
    "ProbeRefusal",
    "RaspaInputPackage",
    "RaspaLedgerTrace",
    "RaspaResult",
    "RaspaScientificContract",
    "RaspaStartRefused",
    "RaspaSupportMatrix",
    "RaspaTraceRefused",
    "ResultCheck",
    "ResultStatus",
    "SourceKind",
    "SourceRecord",
    "TraceItem",
    "TraceRefusal",
    "build_input_package",
    "parse_raspa_output",
    "probe_environment",
    "read_bundled",
    "source_record",
    "trace_result",
    "verify_input_package",
]
