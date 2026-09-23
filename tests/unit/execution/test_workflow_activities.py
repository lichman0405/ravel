"""Every activity the workflow asks for is one the worker answers to.

`NodeRunWorkflow` calls its activities **by name**, as strings, so that the
workflow sandbox never imports the module that opens database connections. The
price is that a name is checked at runtime and not at import: a typo here is not
an `ImportError` at startup, it is a run that dies halfway through, in Temporal,
holding a job that may already have been submitted to a lab.

So the two lists have to agree, and this is the test that says they do. It
reads the workflow's *source* rather than calling it — the names only exist as
string literals inside a workflow definition, and running one needs a Temporal
server. An AST is used rather than a regular expression because the module's
own docstring talks about `execute_activity("...")` at length, and a regex
would happily match the prose explaining the rule as an instance of it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ravel.config import Settings
from ravel.execution.backends import BackendRegistry
from ravel.execution.temporal.activities import NodeRunActivities
from ravel.execution.temporal.worker import node_run_activities
from ravel.state.database import Database

#: The workflow module, read as text. Its activity names are literals in a
#: function body, so the file is the only place they can be read from.
WORKFLOWS = (
    Path(__file__).parents[3] / "src" / "ravel" / "execution" / "temporal" / "workflows.py"
)


def _asked_for() -> set[str]:
    """Every name the workflow passes to `execute_activity`."""
    tree = ast.parse(WORKFLOWS.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        if not (
            isinstance(called, ast.Attribute)
            and called.attr == "execute_activity"
            and isinstance(called.value, ast.Name)
            and called.value.id == "workflow"
        ):
            continue
        if not node.args:
            raise AssertionError(
                f"an execute_activity call at line {node.lineno} names no "
                "activity; the workflow sandbox cannot resolve a function, so "
                "every call here is by name"
            )
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            raise AssertionError(
                f"the execute_activity call at line {node.lineno} does not name "
                "its activity as a plain string, and this test can only check "
                "what it can read"
            )
        names.add(first.value)
    assert names, "no activity calls were found, so the file is not being read"
    return names


def _answered_by() -> set[str]:
    """Every name the worker registers an activity under."""
    activities = NodeRunActivities(
        settings=Settings(),
        # An engine, not a connection: nothing here runs an activity, and
        # `node_run_activities` only reads the bound methods off the object.
        database=Database.from_settings(Settings()),
        registry=BackendRegistry(),
    )
    return {function.__name__ for function in node_run_activities(activities)}


def test_the_workflow_and_the_worker_name_the_same_activities() -> None:
    """The sets are equal, and both directions of that matter.

    A name the workflow asks for and no worker answers is a run that dies
    mid-flight. A registration no workflow calls is the other half — dead
    surface that the next reader will take as evidence some workflow uses it.
    """
    assert _asked_for() == _answered_by()


def test_preparation_is_one_of_them() -> None:
    """Named on its own so that this file fails if the path is ever removed.

    The equality above would still hold if `prepare_execution` were dropped
    from *both* sides — which is exactly the change that would silently turn
    every contract naming an environment back into a run that starts work in no
    workspace at all.
    """
    assert "prepare_execution" in _asked_for()
    assert "prepare_execution" in _answered_by()
