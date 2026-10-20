"""T13-AC2 in deployment: the replay window is configuration, not a test argument.

The resynchronization path is only reachable when the running Gateway has
declared a window. Both production launchers start `create_app` as a uvicorn
factory, which takes no arguments, so the window is named by
`RAVEL_EVENT_RETENTION` and resolved by the application from the process
environment. These cases assert that wiring: a declared window reaches
`app.state.event_retention`, and a malformed one stops the entry point before
uvicorn serves anything rather than silently running unbounded.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from tests.premium.T13.conftest import FAST, ScriptedDeliveries, ScriptedMaster

from ravel.config import Settings
from ravel.gateway.app import (
    EVENT_RETENTION_ENV,
    create_app,
    event_retention_from_environment,
)
from ravel.gateway.auth.tokens import TokenService
from ravel.state.database import Database

pytestmark = pytest.mark.integration


def _entrypoint() -> ModuleType:
    """Load the deployment script as a module without running its main."""
    path = Path(__file__).resolve().parents[3] / "scripts" / "run_gateway.py"
    spec = importlib.util.spec_from_file_location("ravel_run_gateway_t13", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_application_takes_its_replay_window_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    gateway_settings: Settings,
    database: Database,
    tokens: TokenService,
    deliveries: ScriptedDeliveries,
    master: ScriptedMaster,
) -> None:
    """A deployment that declares a window gets exactly that window."""
    assert event_retention_from_environment({}) is None
    assert event_retention_from_environment({EVENT_RETENTION_ENV: " "}) is None
    assert event_retention_from_environment({EVENT_RETENTION_ENV: "7"}) == 7
    for malformed in ("0", "-1", "four", "1.5"):
        with pytest.raises(ValueError, match=EVENT_RETENTION_ENV):
            event_retention_from_environment({EVENT_RETENTION_ENV: malformed})

    monkeypatch.setenv(EVENT_RETENTION_ENV, "7")
    app = create_app(
        settings=gateway_settings,
        database=database,
        tokens=tokens,
        master_of=lambda _project_id: master,
        deliver_external=deliveries,
        cadence=FAST,
        heartbeat_seconds=None,
    )
    assert app.state.event_retention == 7


def test_the_entrypoint_refuses_a_malformed_window_before_serving(
    monkeypatch: pytest.MonkeyPatch,
    gateway_settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A mistyped window exits 2, and uvicorn is never started."""
    module = _entrypoint()
    monkeypatch.setenv(EVENT_RETENTION_ENV, "not-a-number")
    monkeypatch.setattr(module, "Settings", lambda: gateway_settings)
    monkeypatch.setattr(module, "configure_logging", lambda *args, **kwargs: None)
    served: list[str] = []
    monkeypatch.setattr(
        module.uvicorn, "run", lambda *args, **kwargs: served.append("served")
    )

    assert module.main([]) == 2
    assert served == []
    assert EVENT_RETENTION_ENV in capsys.readouterr().err
