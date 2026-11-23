from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mallard_gui.project import Project, RunRecord
from mallard_gui.runlog import RunState
from mallard_gui.ui.health_panel import HealthPanel, HealthSettings

from .test_health import START, progress


class FakeSession:
    def __init__(self, root: Path) -> None:
        self.project = Project.open(root)
        self.project.save_run(RunRecord("r-1", home={"remote": "local", "directory": "runs/r-1"}))
        self.active: dict[str, Any] = {}
        self.stopped: list[str] = []

    def stop_run(self, name: str) -> None:
        self.stopped.append(name)


def panel(tmp_path: Path, channel: str = "web:granted") -> tuple[HealthPanel, FakeSession, list[dict[str, Any]]]:
    session = FakeSession(tmp_path)
    queued: list[dict[str, Any]] = []

    class State(SimpleNamespace):
        def __setattr__(self, key: str, value: Any) -> None:
            if key == "h_native":
                queued.append(value)
            super().__setattr__(key, value)

    state = State(run_sel="r-1", run={}, h_toasts=[])
    app = SimpleNamespace(state=state, session=session, server=SimpleNamespace(enable_module=lambda _: None))
    hp = HealthPanel(app, HealthSettings(tmp_path / "health.toml"))  # type: ignore[arg-type]
    hp.set_channel(channel)
    queued.clear()
    return hp, session, queued


def collapse(st: RunState) -> None:
    st.add(START)
    for i in range(1, 11):
        st.add(progress(i))
    st.add(progress(11, dt=1e-5))


def test_critical_alert_stops_the_run_once_when_set(tmp_path: Path) -> None:
    hp, session, queued = panel(tmp_path)
    hp.set_auto("critical")
    st = RunState()
    collapse(st)
    hp.observe("r-1", st, running=True)
    st.add(progress(12, cfl=1.5))  # another critical alert: no second stop
    hp.observe("r-1", st, running=True)
    assert session.stopped == ["r-1"]
    assert "auto" in hp.state.run["requested"]
    assert any(n["body"].startswith("Stopping with a restart") for n in queued)
    assert queued[0]["icon"].endswith("favicon.png")
    assert HealthSettings(tmp_path / "health.toml").get(hp._key("r-1"))["auto"] == "critical"  # kept per run


def test_off_by_default_and_quiet_when_notifications_are_off(tmp_path: Path) -> None:
    hp, session, queued = panel(tmp_path)
    hp.set_notify(False)
    st = RunState()
    collapse(st)
    hp.observe("r-1", st, running=True)
    assert session.stopped == [] and queued == []
    assert [t["severity"] for t in hp.state.h_toasts] == ["critical"]  # the toast still shows


def test_a_new_run_takes_the_last_settings_of_its_project(tmp_path: Path) -> None:
    settings = HealthSettings(tmp_path / "health.toml")
    settings.set(str(tmp_path / "runs" / "r-1"), auto="critical")
    assert settings.get(str(tmp_path / "runs" / "r-2"))["auto"] == "critical"
    assert settings.get(str(tmp_path / "other" / "runs" / "r-1"))["auto"] == "off"


def test_without_a_channel_only_the_toast_shows(tmp_path: Path) -> None:
    hp, _, queued = panel(tmp_path, channel="none")
    st = RunState()
    collapse(st)
    hp.observe("r-1", st, running=True)
    assert queued == [] and len(hp.state.h_toasts) == 1
