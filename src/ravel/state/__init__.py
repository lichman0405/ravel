"""RAVEL's authoritative Project State.

PostgreSQL is where the truth lives. Temporal schedules work and DSH sessions
run agents, but neither is a record of what happened — a workflow that was
terminated and a session that died both leave the project exactly where the
tables say it is.

The package splits along one line:

- `tables` and `guards` define the schema and the rules PostgreSQL enforces
  itself.
- `repositories` is the only way anything reads or writes it, and every
  repository but the project registry is bound to one project.

`database.Database.transaction()` is the unit of work. Domain changes and the
events describing them are written in the same transaction, which is why a
rolled-back transaction emits nothing.
"""

from ravel.state.database import Database, create_db_engine, create_session_factory
from ravel.state.mapping import build_row, from_row, to_row_data
from ravel.state.outbox import emit, events_since, last_event_seq, next_event_seq
from ravel.state.repositories.base import NotFound, ProjectScopeError
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    AuthorityEnvelopeRepository,
    ExecutionContractRepository,
    ResearchContractRepository,
    SuccessContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.identity import (
    AgentIdentityRepository,
    ApprovalRepository,
    CheckpointRepository,
    MembershipRepository,
    UserRepository,
)
from ravel.state.repositories.projects import ProjectRegistry, RoadmapRepository
from ravel.state.repositories.records import (
    DecisionRepository,
    DeviationRepository,
    ExecutionRepository,
    RecordRepositories,
    ReviewRepository,
)
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceConflictRepository,
    EvidenceRepository,
    EvidenceSourceRepository,
    ResearchRecordRepository,
)
from ravel.state.store import (
    ArtifactImmutableError,
    ArtifactStore,
    ArtifactStoreError,
    S3ArtifactStore,
    StoredObject,
    hash_chunks,
)
from ravel.state.tables import Base

__all__ = [
    "AcceptanceContractRepository",
    "AgentIdentityRepository",
    "ApprovalRepository",
    "ArtifactImmutableError",
    "ArtifactRepository",
    "ArtifactStore",
    "ArtifactStoreError",
    "AuthorityEnvelopeRepository",
    "Base",
    "CheckpointRepository",
    "DagRepository",
    "Database",
    "DecisionRepository",
    "DeviationRepository",
    "EvidenceConflictRepository",
    "EvidenceRepository",
    "EvidenceSourceRepository",
    "ExecutionContractRepository",
    "ExecutionRepository",
    "MembershipRepository",
    "NotFound",
    "ProjectRegistry",
    "ProjectScopeError",
    "RecordRepositories",
    "ResearchContractRepository",
    "ResearchRecordRepository",
    "ReviewRepository",
    "RoadmapRepository",
    "S3ArtifactStore",
    "StoredObject",
    "SuccessContractRepository",
    "UserRepository",
    "build_row",
    "create_db_engine",
    "create_session_factory",
    "emit",
    "events_since",
    "from_row",
    "hash_chunks",
    "last_event_seq",
    "next_event_seq",
    "to_row_data",
]
