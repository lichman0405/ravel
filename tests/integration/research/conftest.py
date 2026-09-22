"""The Research seat's tool-server fixtures, borrowed from the role suites.

`tests/integration/roles` is where driving a real tool server over real MCP
stdio was first needed, and its environment builder is not specific to that
package: it is "the mapping the runtime launches a `(project, role)` server
with", which is the same mapping whichever suite is asking. The deep-read tests
live here because they are about Research's reading surface rather than about
what a role may call, and they import the builder rather than writing a second
one — two definitions of how a server is launched would be two things to keep
in step, and the one that drifted would be the one a passing test used.
"""

from __future__ import annotations

from tests.integration.roles.conftest import RoleEnvironment, role_environment

__all__ = ["RoleEnvironment", "role_environment"]
