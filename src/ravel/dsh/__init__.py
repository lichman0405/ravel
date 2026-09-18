"""The DeepSeek Harness boundary.

Everything that knows about the pinned harness lives here. Domain code imports
these names and never the SDK, so a re-pin touches one package rather than the
whole product.
"""

from ravel.dsh.binding import BindingError, SessionBinding, SessionBindingRegistry
from ravel.dsh.pool import DshRuntimePool, PoolStats, create_pool
from ravel.dsh.roles import ROLE_DEFINITIONS, AgentRole, RoleDefinition, definition_for
from ravel.dsh.runtime import HarnessRuntimeError, RoleRuntime, TurnOutcome

__all__ = [
    "ROLE_DEFINITIONS",
    "AgentRole",
    "BindingError",
    "DshRuntimePool",
    "HarnessRuntimeError",
    "PoolStats",
    "RoleDefinition",
    "RoleRuntime",
    "SessionBinding",
    "SessionBindingRegistry",
    "TurnOutcome",
    "create_pool",
    "definition_for",
]
