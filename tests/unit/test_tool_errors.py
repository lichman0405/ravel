"""What a refusal looks like by the time the model reads it.

The transport draws a hard line between a failure a handler *reports* and one
that merely escapes it: a reported failure arrives with its message, a crash
arrives as `Error executing tool <name>` and nothing else. Everything RAVEL
writes into a refusal — which stages are in the horizon, which confidence
levels exist, which node was not found — is only useful if it lands on the
right side of that line, so the conversion is tested rather than assumed.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from ravel.domain.planning import HorizonError
from ravel.domain.state_machines import TransitionError
from ravel.mcp.errors import REFUSALS, report_refusals
from ravel.mcp.scope import ScopeError
from ravel.state.repositories.base import NotFound, ProjectScopeError


def _raised_by(handler: Any, **arguments: object) -> BaseException:
    """Run a wrapped handler to completion, returning what it raised."""

    async def run() -> BaseException:
        try:
            await handler(**arguments)
        except BaseException as exc:  # the point is to inspect it
            return exc
        raise AssertionError("the handler returned; this test is about failures")

    return asyncio.run(run())


def _handler(raising: BaseException) -> Any:
    async def handler(**_arguments: object) -> dict[str, object]:
        raise raising

    return handler


def test_a_domain_refusal_reaches_the_model_with_its_reason() -> None:
    reported = _raised_by(
        report_refusals(
            _handler(HorizonError("'Stage 4' is beyond the planning horizon; try 'Stage 1'"))
        )
    )
    assert isinstance(reported, ToolError)
    assert "'Stage 4' is beyond the planning horizon" in str(reported)
    assert "'Stage 1'" in str(reported)
    assert isinstance(reported.__cause__, HorizonError)


@pytest.mark.parametrize(
    "refusal",
    [
        ScopeError("this process has no usable scope"),
        NotFound("no node with that id"),
        ProjectScopeError("that record belongs to another project"),
        PermissionError("only Master decides"),
        TransitionError("PLANNED may not become RUNNING"),
        ValueError("confidence must be one of LOW, MEDIUM, HIGH"),
    ],
)
def test_every_anticipated_failure_is_reported(refusal: BaseException) -> None:
    assert isinstance(_raised_by(report_refusals(_handler(refusal))), ToolError)


def test_a_crash_is_left_to_crash() -> None:
    """A defect must stay a defect.

    Reporting a `KeyError` as though it were a refusal would let RAVEL tell a
    model something it made up, and the model would act on it. The wrapper's
    job is to widen the boundary by exactly the set of failures the domain has
    words for, and no further.
    """
    crash = KeyError("node_id")
    result = _raised_by(report_refusals(_handler(crash)))
    assert result is crash


def test_an_already_reported_failure_is_not_wrapped_twice() -> None:
    original = ToolError("say this once")
    result = _raised_by(report_refusals(_handler(original)))
    assert result is original


def test_a_validation_error_is_flattened_to_its_fields() -> None:
    """Pydantic's own rendering spans a dozen boxed lines written for a terminal."""

    class Node(BaseModel):
        join_threshold: int

    # `model_validate` rather than the constructor: this test is about a value
    # arriving from the wire, which is untyped by the time it gets here.
    with pytest.raises(ValidationError) as raised:
        Node.model_validate({"join_threshold": "two"})

    result = _raised_by(report_refusals(_handler(raised.value)))
    assert isinstance(result, ToolError)
    message = str(result)
    assert "join_threshold" in message
    assert "\n" not in message
    assert "pydantic.dev" not in message


def test_the_wrapper_preserves_the_signature_the_transport_reads() -> None:
    """The input schema is derived from the handler; a wrapper must not hide it."""

    async def handler(
        node_id: str, rationale: str, confidence: str = "MEDIUM"
    ) -> dict[str, object]:
        return {"node_id": node_id, "rationale": rationale, "confidence": confidence}

    wrapped = report_refusals(handler)
    assert inspect.signature(wrapped) == inspect.signature(handler)
    assert wrapped.__name__ == "handler"


def test_the_refusal_set_is_exactly_the_domains_vocabulary() -> None:
    """Adding a domain error class without deciding about it should be visible."""
    assert set(REFUSALS) == {
        ScopeError,
        ProjectScopeError,
        NotFound,
        PermissionError,
        ValueError,
    }
