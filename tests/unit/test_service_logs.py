"""What a service's log line has to carry, and what it must not.

Two shapes for two readers. A person at a terminal wants a sentence with the
process and its pid in it; a collector wants one object per line whose keys it
can query without a regular expression that breaks the next time a format
string changes. Both are checked here by reading what the handler actually
wrote, rather than by asserting that a certain formatter class was installed —
the class is an implementation detail and the line is the product.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from ravel.domain.services import SUPERVISOR
from ravel.service import JsonFormatter, configure_logging
from ravel.service.logs import LOG_FORMATS, RESERVED_KEYS, ServiceFilter


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Put the root logger back, so one test's handlers do not leak into the next.

    `configure_logging` uses `force=True`, which removes handlers it did not
    install — including pytest's own capture handler. Leaving it that way would
    make every later test's output disappear in a way that reads as a test that
    logged nothing.
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _line(stream: io.StringIO) -> dict[str, Any]:
    written = stream.getvalue().strip().splitlines()
    assert len(written) == 1, f"expected one line, got {written}"
    return json.loads(written[0])


def test_a_json_line_carries_the_keys_a_collector_queries_on(stream: io.StringIO) -> None:
    """The five fields, and they are there whether or not the call site helped.

    A line whose service is missing is a line an operator has to guess about
    when three processes write into one journal, so the name is attached by the
    handler rather than passed by the caller — the assertion is that a call
    site which said nothing about itself still lands attributable.
    """
    configure_logging(SUPERVISOR, log_format="json", stream=stream)
    logging.getLogger("ravel.execution.loop").info("reaped %d runtime(s)", 3)

    line = _line(stream)

    assert set(RESERVED_KEYS) <= set(line)
    assert line["level"] == "INFO"
    assert line["service"] == SUPERVISOR
    assert line["logger"] == "ravel.execution.loop"
    assert line["message"] == "reaped 3 runtime(s)"
    assert line["ts"].endswith("Z") or "+00:00" in line["ts"], (
        "the timestamp must say which zone it is in; a bare local time is one "
        "that reads differently on two hosts looking at one journal"
    )


def test_a_field_a_call_site_added_is_a_key_beside_the_reserved_ones(
    stream: io.StringIO,
) -> None:
    """`extra` is copied in flat, because the point of the format is naming fields.

    A `context` object would mean every query has to know the shape of a value
    one level down, which is the arrangement the JSON format exists to avoid.
    """
    configure_logging(SUPERVISOR, log_format="json", stream=stream)
    logging.getLogger(__name__).warning(
        "project halted", extra={"project_id": "p-1", "rounds": 7}
    )

    line = _line(stream)

    assert line["project_id"] == "p-1"
    assert line["rounds"] == 7
    assert line["level"] == "WARNING"


def test_a_traceback_stays_inside_the_record_it_belongs_to(stream: io.StringIO) -> None:
    """One exception is one line, not a paragraph of unattributed ones.

    A traceback written as separate lines is a traceback a collector reads as
    several events with no service name on them — which is exactly the line an
    operator most needs to attribute to a process.
    """
    configure_logging(SUPERVISOR, log_format="json", stream=stream)
    try:
        raise ValueError("the cluster said no")
    except ValueError:
        logging.getLogger(__name__).exception("submission failed")

    line = _line(stream)

    assert "the cluster said no" in str(line["exception"])
    assert "Traceback" in str(line["exception"])
    assert line["message"] == "submission failed"
    assert line["service"] == SUPERVISOR


def test_the_text_shape_names_the_process_and_its_pid(stream: io.StringIO) -> None:
    """What a person reading a terminal needs that a collector does not.

    The pid is in this format and not in the JSON one on purpose: a collector
    read the line from a known process, and a person watching three services'
    output interleaved has nothing else to tell them apart with.
    """
    configure_logging(SUPERVISOR, log_format="text", stream=stream)
    logging.getLogger("ravel.dsh.pool").info("closed 2 runtime(s)")

    written = stream.getvalue().strip()

    assert f"{SUPERVISOR}[" in written
    assert "ravel.dsh.pool: closed 2 runtime(s)" in written


def test_an_unknown_log_format_is_refused_rather_than_read_as_text(
    stream: io.StringIO,
) -> None:
    """A deployment that asked for JSON and got prose finds out at the collector.

    Which is to say, long afterwards and in a different system. Refusing at
    start-up puts the failure where somebody is watching the service come up.
    """
    with pytest.raises(ValueError, match="unknown log format"):
        configure_logging(SUPERVISOR, log_format="logfmt", stream=stream)

    assert LOG_FORMATS == ("text", "json")


def test_the_filter_marks_every_record_that_passes_through_it() -> None:
    """Including records the filter's own logger never made.

    The filter hangs off the handler, not off a logger, which is what makes it
    apply to a library module that has no idea which service it is running
    inside — and that is the correct amount for a library to know.
    """
    record = logging.LogRecord(
        name="ravel.state.database",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="connected",
        args=(),
        exc_info=None,
    )

    assert ServiceFilter(SUPERVISOR).filter(record) is True
    assert getattr(record, "service", None) == SUPERVISOR


def test_the_json_formatter_leaves_the_logging_machinery_out_of_the_line() -> None:
    """A line holds the message and what the call site meant to say.

    `lineno`, `threadName`, `relativeCreated` and the rest are how `logging`
    moves a record around inside one process. A line that carried them would be
    a line whose keys a collector has to filter out of every query.
    """
    record = logging.LogRecord(
        name="ravel.execution.loop",
        level=logging.INFO,
        pathname=__file__,
        lineno=417,
        msg="tick",
        args=(),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "tick"
    assert "lineno" not in payload
    assert "threadName" not in payload
    assert "msg" not in payload
