from __future__ import annotations

import tomllib
from pathlib import Path

from mallard_gui.live import LiveControl, current_value, set_replies, settable_keys
from mallard_gui.mallard import Capabilities, Completed, capabilities_from_help

from .conftest import FIXTURES

PATTERNS = capabilities_from_help("", (FIXTURES / "ctl_help.txt").read_text()).settable
DATA = tomllib.loads((FIXTURES / "cylinder_input.toml").read_text())


def test_settable_keys_name_the_runs_own_writers_probes_and_forces() -> None:
    keys = settable_keys(PATTERNS, DATA)
    assert "write_data../solut/near_wake.time_interval" in keys
    assert "probes.wake_arc.interval" in keys
    assert "forces.cylinder.interval" in keys
    assert "t_stop" in keys
    assert not any("<" in k for k in keys)
    assert "integrals.interval" not in keys  # the case has no [integrals]


def test_current_values_come_from_the_run_input() -> None:
    assert current_value(DATA, "t_stop") == 160.0
    assert current_value(DATA, "check_interval") == 2000
    assert current_value(DATA, "probes.wake_point.time_interval") == 0.1
    assert current_value(DATA, "write_data../solut/wall.time_interval") == 1.0


class Ctl:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def ctl_set(self, run_dir: Path, settings: dict[str, str]) -> Completed:
        self.sent.append(dict(settings))
        return Completed(0, "set t_stop=200\n", "", 0.0)


def test_the_socket_wins_and_a_set_file_is_the_fallback(tmp_path: Path) -> None:
    ctl = Ctl()
    live = LiveControl(ctl, Capabilities(set_file=True))  # type: ignore[arg-type]
    allowed = settable_keys(PATTERNS, DATA)
    reply = live.apply(tmp_path, {"t_stop": "200"}, allowed)
    assert reply.channel == "SET file" and (tmp_path / "SET").read_text() == "t_stop = 200\n" and not ctl.sent
    (tmp_path / "mallard.sock").touch()
    reply = live.apply(tmp_path, {"t_stop": "200"}, allowed)
    assert reply.ok and reply.channel == "mallard ctl" and ctl.sent == [{"t_stop": "200"}]


def test_unsettable_keys_and_missing_channels_are_refused_here(tmp_path: Path) -> None:
    live = LiveControl(Ctl(), Capabilities())  # type: ignore[arg-type]
    allowed = settable_keys(PATTERNS, DATA)
    assert not live.apply(tmp_path, {"run.cfl": "2"}, allowed).ok
    reply = live.apply(tmp_path, {"t_stop": "200"}, allowed)
    assert not reply.ok and "SET file" in reply.text


def test_replies_come_from_set_events_and_refusals() -> None:
    events: list[dict[str, object]] = [
        {"kind": "set", "step": 10, "setting": "t_stop=200", "source": "SET file"},
        {"kind": "checkpoint"},
    ]
    warnings = [{"code": "W-SET-REFUSED", "step": 20, "message": "line 1: run.cfl is not settable"}]
    assert [(r["ok"], r["step"]) for r in set_replies(events, warnings)] == [(True, 10), (False, 20)]
