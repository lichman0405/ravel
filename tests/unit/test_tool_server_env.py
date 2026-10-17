"""The environment a tool server is launched with, which is the whole of what it knows.

A tool server is a separate process the harness spawns. It does not inherit the
object that configured the process which launched the runtime; it reads the
environment, and every coordinate it acts on has to be in there. A server left
to read `.env` answers with whatever that file says, which is right only while
every process in a deployment happens to read the same file.

Both defects this file exists for were found by running the stack rather than by
reading it. A live session's tools read a different database than the supervisor
driving it — every authority rule in RAVEL read the wrong way round, with the
same project id and no error saying which database answered. And a live Worker's
`start_execution` started its run on the deployment's task queue while the
worker listening for it polled the queue that deployment had been configured
with: the run was accepted, nobody executed it, and the node waited on a
workflow that would never move it. The second is the quieter failure, which is
why the tests below are about the *rule* and not about the two coordinates.
"""

from __future__ import annotations

import os

import pytest

from ravel.config import Settings

#: Settings the launcher holds and a tool server has no use for. Named here as
#: a list of examples rather than as the rule itself, so that a change to the
#: rule has to disagree with a written-down expectation.
#:
#: `slurm_` joined the rule in P11-05 and is the one family here excluded for
#: more than uselessness. A tool server has no cluster to reach — the backend
#: that speaks to one lives in the worker process — so every `RAVEL_SLURM_*`
#: value is one no tool acts on. The password is the one that must not travel:
#: the Compute Worker's session is a model, and "the model never sees the
#: cluster password" is a property of the environment it is launched with
#: rather than of the model choosing not to look.
LAUNCHER_ONLY = (
    "dsh_model",
    "dsh_bin",
    "dsh_home",
    "deepseek_api_key",
    "deepseek_base_url",
    "gateway_jwt_secret",
    "gateway_port",
    "slurm_host",
    "slurm_password",
    "slurm_jobs_root",
)

#: Settings a tool server does act on, one per family it reaches.
TRAVELLING = (
    "temporal_task_queue",
    "postgres_db",
    "s3_bucket",
    "search_provider",
    "research_contact_email",
)


def env_name(field_name: str) -> str:
    """The environment variable a field is read from, spelled as `.env` spells it."""
    field = Settings.model_fields[field_name]
    return field.alias or f"RAVEL_{field_name.upper()}"


def launched_with(mapping: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The settings a process launched with `mapping` would read back.

    Every `RAVEL_` and `DEEPSEEK_` variable is cleared first, so what this reads
    is the mapping and not whatever the suite's own environment holds. That is
    the question a tool server answers at startup, and it is the only question
    that matters here: a mapping that is *nearly* the deployment's is a mapping
    that reads some other deployment's coordinates.

    The deployment's `.env` is still readable from here, and deliberately left
    so: it is the file the mapping exists to outrank, and every field the
    mapping carries is set as an environment variable, which is the layer that
    wins. A test that removed the file would not be testing that.
    """
    for name in list(os.environ):
        if name.startswith(("RAVEL_", "DEEPSEEK_")):
            monkeypatch.delenv(name, raising=False)
    for name, value in mapping.items():
        monkeypatch.setenv(name, value)
    return Settings()


def test_the_execution_coordinates_travel_with_the_state_coordinates() -> None:
    """Where a run lands is as much a coordinate as where the state lives.

    A session whose `start_execution` reaches a different task queue than the
    process driving it is a session whose work is accepted and never done. The
    DAG then shows a node that is READY, the Worker that started it sees
    `already_under_way`, and no part of the stack says the two halves are
    talking to different Temporal deployments.
    """
    settings = Settings().model_copy(
        update={
            "temporal_host": "127.0.0.1:17233",
            "temporal_namespace": "deployment",
            "temporal_task_queue": "ravel-v0-test-abc123",
        }
    )
    env = settings.tool_server_env()
    assert env["RAVEL_TEMPORAL_HOST"] == "127.0.0.1:17233"
    assert env["RAVEL_TEMPORAL_NAMESPACE"] == "deployment"
    assert env["RAVEL_TEMPORAL_TASK_QUEUE"] == "ravel-v0-test-abc123", (
        "a run started by a live session would land on whatever queue `.env` "
        "names, and no worker listening on the deployment's queue would take it"
    )


def test_every_setting_but_the_launchers_own_travels() -> None:
    """The set is derived, and this is the rule it is derived by.

    Written as the rule rather than as a list because a list is what failed: the
    coordinates travelled were the ones somebody remembered when the method was
    written, and each failure since has been a coordinate added to `Settings`
    after it. A field that is added tomorrow travels unless it is deliberately
    excluded, and one that is excluded has to disagree with the names below.
    """
    env = Settings().tool_server_env()
    derived = {
        env_name(name)
        for name in Settings.model_fields
        if not name.startswith(("dsh_", "deepseek_", "gateway_", "slurm_"))
    }
    assert set(env) == derived

    for field_name in TRAVELLING:
        assert env_name(field_name) in env, f"{field_name} does not reach a tool server"
    for field_name in LAUNCHER_ONLY:
        assert env_name(field_name) not in env, (
            f"{field_name} is the launcher's, and a tool server spawns no model "
            "request and serves no HTTP"
        )


def test_the_environment_is_read_back_as_the_settings_it_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What is written can be read, which is the only thing that makes it a grant.

    Not a formality: the values are rendered by hand — secrets unwrapped,
    booleans spelled, an absent optional written as the empty string — and a
    rendering that a settings object cannot read back would leave a tool server
    starting on defaults, which is the failure this whole method is against.
    """
    settings = Settings().model_copy(
        update={
            "s3_bucket": "artifacts-for-this-deployment",
            "search_provider": "brave",
            "playwright_headless": False,
            "job_poll_seconds": 0.25,
        }
    )
    read_back = launched_with(settings.tool_server_env(), monkeypatch)

    assert read_back.s3_bucket == "artifacts-for-this-deployment"
    assert read_back.search_provider == "brave"
    assert read_back.playwright_headless is False
    assert read_back.job_poll_seconds == 0.25
    assert read_back.temporal_task_queue == settings.temporal_task_queue
    assert read_back.postgres_db == settings.postgres_db
    assert read_back.postgres_password.get_secret_value() == (
        settings.postgres_password.get_secret_value()
    ), "the password is rendered as itself, not as `**********`"


def test_an_absent_dsn_override_travels_as_the_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stated as the effective value, so an ambient override cannot outrank it.

    A deployment with no DSN override of its own would otherwise be at the mercy
    of one in the shell that launched it, and the tool server would connect to a
    database nobody else in the deployment is using.
    """
    settings = Settings().model_copy(update={"postgres_dsn_override": None})
    env = settings.tool_server_env()
    assert env["RAVEL_POSTGRES_DSN"] == ""
    assert launched_with(env, monkeypatch).postgres_dsn_override is None
