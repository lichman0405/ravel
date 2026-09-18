"""The only code that reads or writes Project State.

Every repository except `ProjectRegistry` is constructed with a `project_id`
and filters every statement by it. That is the authorization boundary: a
`project_id` supplied by a model is a value to be checked, never an authority
to be trusted. See `base.ProjectScopedRepository`.
"""

from ravel.state.repositories.base import (
    NotFound,
    ProjectScopedRepository,
    ProjectScopeError,
)
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

__all__ = [
    "AcceptanceContractRepository",
    "AgentIdentityRepository",
    "ApprovalRepository",
    "ArtifactRepository",
    "AuthorityEnvelopeRepository",
    "CheckpointRepository",
    "DagRepository",
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
    "ProjectScopedRepository",
    "RecordRepositories",
    "ResearchContractRepository",
    "ResearchRecordRepository",
    "ReviewRepository",
    "RoadmapRepository",
    "SuccessContractRepository",
    "UserRepository",
]
