"""The half of a long-running process that is about being a process.

RAVEL runs three things that do not end on their own: the Gateway, the
supervisor, and the Temporal worker. Each of them is a unit an operator starts
and forgets, and each of them has the same three obligations that have nothing
to do with what it computes — say what it is doing in a form something else can
read, say that it is still here, and stop when it is told to.

`ravel.domain.services` is the vocabulary for the second of those and
`ravel.state.repositories.services` is its storage. This package is what a
process does with them: `logs` is how a service writes a line, and `heartbeat`
is how it reports itself and retires.
"""

from __future__ import annotations

from ravel.service.heartbeat import Heartbeat
from ravel.service.logs import JsonFormatter, configure_logging

__all__ = ["Heartbeat", "JsonFormatter", "configure_logging"]
