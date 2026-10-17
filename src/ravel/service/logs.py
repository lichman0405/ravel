"""How a long-running RAVEL process writes a line.

The three services log to a terminal a person is watching, to a file
`scripts/run_v0.sh` opens for them, and to init's own collector when they are
units — and those three want different things. A person wants a sentence; a
collector wants a record it can index without a regular expression that breaks
the next time somebody edits a format string.

So there are two shapes and one switch. `RAVEL_LOG_FORMAT=text` is the default
and is what a person reads; `RAVEL_LOG_FORMAT=json` is one object per line, with
the keys a collector queries on — `ts`, `level`, `service`, `logger`, `message`
— and the rest of the record's fields beside them.

**What is deliberately absent.** Nothing here ships a log *file*: the units send
their output to journald and `scripts/run_v0.sh` redirects to `runtime/logs/`,
and a library that opened its own file would be a third destination that a
deployment's rotation does not know about. There is also no request logging and
no access log — those belong to the thing serving requests, and the Gateway
already gets uvicorn's.

**The service name is not optional.** It is the one field that makes a line
attributable when three processes write into one journal, and a line that
cannot be attributed is one an operator has to guess about. It is attached by a
filter rather than demanded at each call site, because a call site that forgot
it would produce a line that looks exactly like every other line.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

#: The two shapes a deployment can ask for.
TEXT = "text"
JSON = "json"
LOG_FORMATS: tuple[str, ...] = (TEXT, JSON)

#: The keys every JSON line carries, in the order they are written. Fixed so
#: that two lines from different services can be read by one query, and so that
#: a field added to `extra` cannot take a name that already means something.
RESERVED_KEYS: tuple[str, ...] = ("ts", "level", "service", "logger", "message")


class ServiceFilter(logging.Filter):
    """Puts the name of the process on every record that goes through it.

    A filter rather than a `LoggerAdapter` at each call site because the call
    sites are everywhere and the process is one thing: a library module logs
    `logger.info("reaped %d", n)` and has no idea which service it is running
    inside, which is the correct amount for it to know.
    """

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self.service
        return True


class JsonFormatter(logging.Formatter):
    """One line of JSON per record, with the fields a collector queries on.

    `exc_info` is rendered into the same object under `exception`, so that a
    traceback stays part of the record it belongs to rather than becoming
    lines a collector reads as separate events with no service on them.

    Anything a call site passed as `extra` is copied in beside the reserved
    keys. It is *not* nested under a `context` key: the whole point of the
    format is that a query can name a field, and a field one level down is a
    field every query has to know the shape of.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self._timestamp(record),
            "level": record.levelname,
            "service": getattr(record, "service", ""),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in payload or key in _STANDARD_ATTRIBUTES or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)

    @staticmethod
    def _timestamp(record: logging.LogRecord) -> str:
        """When the record was made, in UTC and in a shape a query can order.

        From the record's own creation time rather than from the clock as the
        line is written, so that a burst that was queued behind a slow handler
        keeps the order it happened in.
        """
        return datetime.fromtimestamp(record.created, tz=UTC).isoformat(
            timespec="milliseconds"
        )


def configure_logging(
    service: str, *, level: str = "INFO", log_format: str = TEXT, stream: Any = None
) -> None:
    """Point the root logger at this process's own shape.

    Called once, by a process's `main`, before anything that logs has had a
    chance to. `force=True` because the alternative is a deployment that
    inherited a handler from something else — uvicorn installs its own, and a
    service that logged twice per line because of it would be a puzzle nobody
    enjoys.

    Args:
        service: Which process this is, from `ravel.domain.services`. It lands
            on every record.
        level: The threshold, as a logging level name.
        log_format: `text` or `json`. An unknown value is refused rather than
            silently treated as text: a deployment that asked for JSON logs and
            got prose would find out at the collector, long afterwards.
        stream: Where the records go. Defaults to stderr, which is where a
            unit's output is read from.

    Raises:
        ValueError: `log_format` is not one of `LOG_FORMATS`.
    """
    if log_format not in LOG_FORMATS:
        raise ValueError(
            f"unknown log format {log_format!r}; expected one of "
            f"{', '.join(LOG_FORMATS)}"
        )
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.addFilter(ServiceFilter(service))
    if log_format == JSON:
        handler.setFormatter(JsonFormatter())
    else:
        # The pid is in the text format and not in the JSON one, because the
        # JSON one's collector already knows which process it read from and a
        # person watching a terminal does not.
        handler.setFormatter(
            logging.Formatter(
                fmt=f"%(asctime)s %(levelname)-7s {service}[{os.getpid()}] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)


#: What `logging` puts on every record. Skipped when copying a record's fields
#: into the JSON object, so that a line holds the message and what the call
#: site meant to say and not the machinery that carried it.
_STANDARD_ATTRIBUTES = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

__all__ = [
    "JSON",
    "LOG_FORMATS",
    "RESERVED_KEYS",
    "TEXT",
    "JsonFormatter",
    "ServiceFilter",
    "configure_logging",
]
