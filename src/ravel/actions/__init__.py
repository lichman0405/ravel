"""Executable Actions: the only route through which RAVEL causes an effect.

The package holds the lifecycle service and the persistence it needs. The
domain types live in `ravel.domain.actions`, because an Action is a record the
schema, the gateway and the MCP tools all read, and a record that only this
package could construct would be a private vocabulary rather than the
contract's.

Nothing here reaches an external system. A service method moves an Action to
`READY` or `CLAIMED`; the caller that holds the external client performs the
submission and reports what happened. That split is deliberate: the checks
that must happen before a submission — identity, lease, envelope, approval,
budget — are testable without a scheduler or a mail server, and the external
client cannot run them out of order because it cannot claim an Action whose
checks have not passed.
"""

from ravel.actions.service import ActionService

__all__ = ["ActionService"]
