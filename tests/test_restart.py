"""Continue a stopped run from its last restart, and start a new run from another run's restart."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mallard_gui.backends.local import LocalBackend
from mallard_gui.session import RestartError, Session

from .conftest import FakeMallard

# Each part writes a restart and stops, as a STOP request would; a continuation waits a second first, so
# the GUI sees the earlier part's end record before the new start record.
FAKE_RUN = r"""#!/bin/sh
restart=""
while [ $# -gt 0 ]; do [ "$1" = --restart ] && restart=$2; shift; done
n=$(cat mallard.jsonl 2>/dev/null | grep -c '"type":"start"')
[ -n "$restart" ] && sleep 1
step=$(( (n + 1) * 10 ))
f="restart_$step.restart"
printf 'state %s\n' "$step" > "$f"
sha=$(shasum -a 256 "$f" | cut -d' ' -f1)
ro=null
[ -n "$restart" ] && ro="{\"file\":\"$restart\",\"step\":$((step - 10))}"
printf '%s\n' "$restart" > "args-$n.txt"
printf '{"type":"start","schema":"mallard-run-log","version":2,"run_id":"r%s","restart_of":%s,"units":{}}\n' \
  "$n" "$ro" >> mallard.jsonl
printf '{"type":"output","step":%s,"kind":"restart","path":"./%s","files":[{"path":"./%s","sha256":"%s"}]}\n' \
  "$step" "$f" "$f" "$sha" >> mallard.jsonl
printf '{"type":"end","outcome":"stopped","step":%s,"last_restart":{"step":%s,"path":"./%s"}}\n' \
  "$step" "$step" "$f" >> mallard.jsonl
exit 3
"""


@pytest.fixture
def session(cylinder_project: Path, tmp_path: Path) -> Session:
    fake = tmp_path / "bin" / "mallard"
    fake.parent.mkdir()
    fake.write_text(FAKE_RUN)
    fake.chmod(0o755)
    s = Session(cylinder_project, FakeMallard())  # type: ignore[arg-type]
    s.backend = LocalBackend(fake)
    s.check()
    return s


def finish(s: Session, name: str) -> None:
    run = s.active[name]
    assert isinstance(s.backend, LocalBackend)
    s.backend.wait(run.job, timeout=30)
    s.poll_run(name)


def test_continue_appends_a_part_from_the_verified_restart(session: Session) -> None:
    first = session.start_run()
    name = first.record.name
    finish(session, name)
    record = session.project.run(name)
    assert record.outcome == "stopped"
    assert record.last_restart["path"] == "./restart_10.restart" and record.last_restart["sha256"]

    run = session.continue_run(name)
    st = session.poll_run(name)  # the first part's end record is still the last one
    assert session.project.run(name).outcome == ""
    assert not run.started and st.starts == 1
    finish(session, name)

    run_dir = Path(run.job.run_dir)
    assert (run_dir / "args-1.txt").read_text().strip() == str(run_dir / "restart_10.restart")
    st = session.active[name].follower.state
    assert st.starts == 2
    assert st.start["restart_of"]["file"] == str(run_dir / "restart_10.restart")
    record = session.project.run(name)
    assert record.outcome == "stopped"
    assert record.last_restart["path"] == "./restart_20.restart"


def test_continue_refuses_a_restart_that_changed(session: Session) -> None:
    name = session.start_run().record.name
    finish(session, name)
    (Path(session.active[name].job.run_dir) / "restart_10.restart").write_text("other\n")
    with pytest.raises(RestartError, match="SHA-256"):
        session.continue_run(name)


def test_continue_needs_a_stopped_run(session: Session) -> None:
    name = session.start_run().record.name
    finish(session, name)
    record = session.project.run(name)
    record.outcome = "finished"
    session.project.save_run(record)
    with pytest.raises(RestartError, match="finished"):
        session.continue_run(name)


def test_restart_from_starts_a_new_run_of_the_case_input(session: Session) -> None:
    source = session.start_run().record.name
    finish(session, source)
    run = session.start_run(restart_from=source)
    finish(session, run.record.name)
    assert run.record.name != source
    restart = session.project.root / "runs" / source / "restart_10.restart"
    assert (Path(run.job.run_dir) / "args-0.txt").read_text().strip() == str(restart.resolve())


def test_restart_from_takes_any_restart_the_run_wrote(session: Session) -> None:
    source = session.start_run().record.name
    finish(session, source)
    session.continue_run(source)
    finish(session, source)
    assert [r["path"] for r in session.restarts(source)] == ["./restart_20.restart", "./restart_10.restart"]
    run = session.start_run(restart_from=source, restart_path="./restart_10.restart")
    finish(session, run.record.name)
    restart = session.project.root / "runs" / source / "restart_10.restart"
    assert (Path(run.job.run_dir) / "args-0.txt").read_text().strip() == str(restart.resolve())
    assert session.lineage(run.record.name) == [{"file": str(restart.resolve()), "step": 0, "run": ""}]


def test_restart_check_gives_mallards_answer_for_each_changed_key(session: Session) -> None:
    source = session.start_run().record.name
    finish(session, source)
    session.mallard.restart_check = "cylinder_check_restart.json"  # type: ignore[attr-defined]
    result = session.restart_check(source)
    assert session.mallard.extra[-2:] == ["--restart", str(session.verified_restart(session.project.run(source)))]  # type: ignore[attr-defined]
    assert result["known"] and not result["error"]
    effects = {c["key"]: c["effect"] for c in result["changes"]}
    assert effects["run.t_stop"] == "accepted" and "incompatible" in effects.values()


def test_a_build_that_classifies_no_key_says_so(session: Session) -> None:
    source = session.start_run().record.name
    finish(session, source)
    result = session.restart_check(source)
    assert not result["known"] and result["changes"] == []


def test_restart_check_refuses_a_changed_file(session: Session) -> None:
    source = session.start_run().record.name
    finish(session, source)
    (Path(session.active[source].job.run_dir) / "restart_10.restart").write_text("other\n")
    assert "SHA-256" in session.restart_check(source)["error"]


def test_a_restart_of_another_gas_is_previewed_then_converted_once_the_rule_is_accepted(session: Session) -> None:
    source = session.start_run().record.name
    finish(session, source)
    fake = session.mallard
    fake.restart_check = "mixture_check_restart_mechanism.json"  # type: ignore[attr-defined]
    fake.converted_check = "mixture_check_restart_converted.json"  # type: ignore[attr-defined]
    fake.convert_fixture = "restart_convert_dry.json"  # type: ignore[attr-defined]
    effects = {c["key"]: c["effect"] for c in session.restart_check(source)["changes"]}
    assert effects["physics.mechanism"] == "incompatible"
    preview = session.convert_preview(source)
    assert preview is not None and preview["rule"] == "element-projection" and not preview["exact"]
    restart = session.verified_restart(session.project.run(source))
    assert fake.converts[-1] == {"restart": restart, "out": "", "accept": ""}  # type: ignore[attr-defined]

    out = session.convert_restart(source, "", preview["rule"])
    assert out.parent == session.project.root / "converted" and out.read_bytes().endswith(b"converted\n")
    assert fake.converts[-1]["accept"] == "element-projection"  # type: ignore[attr-defined]
    after = session.restart_check(source, converted=str(out))
    assert fake.extra[-2:] == ["--restart", str(out)]  # type: ignore[attr-defined]
    assert not any(c["effect"] == "incompatible" for c in after["changes"]) and not after["error"]
    run = session.start_run(restart_from=source, converted=str(out))
    finish(session, run.record.name)
    assert (Path(run.job.run_dir) / "args-0.txt").read_text().strip() == str(out)


def test_a_setup_that_stops_on_the_file_with_no_key_to_blame_is_an_error(session: Session) -> None:
    source = session.start_run().record.name
    finish(session, source)
    session.mallard.restart_check = "mixture_check_restart_unclassified.json"  # type: ignore[attr-defined]
    result = session.restart_check(source)
    assert result["known"] and "RHOY_H2" in result["error"]


def test_a_build_that_cannot_convert_previews_nothing_and_refuses_to(session: Session) -> None:
    source = session.start_run().record.name
    finish(session, source)
    assert session.convert_preview(source) is None
    with pytest.raises(RestartError, match="cannot convert"):
        session.convert_restart(source, "", "element-projection")


@pytest.mark.mallard
def test_a_real_run_stopped_and_continued(real_mallard, cylinder_project: Path) -> None:  # type: ignore[no-untyped-def]
    path = cylinder_project / "input.toml"
    path.write_text(
        path.read_text()
        .replace("t_stop = 160.0", "t_stop = 100000.0")
        .replace("check_interval = 2000", "check_interval = 5")
    )
    s = Session(cylinder_project, real_mallard)
    s.check()

    def run_until_progress_then_stop(name: str, parts: int) -> None:
        end = time.monotonic() + 120
        while True:
            st = s.poll_run(name)
            if s.active[name].started and st.last and st.starts == parts and st.end is None:
                break
            assert time.monotonic() < end, "no progress"
            time.sleep(0.2)
        s.stop_run(name)
        assert isinstance(s.backend, LocalBackend)
        assert s.backend.wait(s.active[name].job, timeout=120) in (0, 3)
        s.poll_run(name)

    name = s.start_run().record.name
    try:
        run_until_progress_then_stop(name, 1)
        record = s.project.run(name)
        assert record.outcome == "stopped" and record.last_restart.get("sha256")
        first = record.last_restart["path"]
        s.continue_run(name)
        run_until_progress_then_stop(name, 2)
        st = s.active[name].follower.state
        assert st.starts == 2
        assert Path(st.start["restart_of"]["file"]).name == Path(first).name
        assert s.project.run(name).outcome == "stopped"
        forces = next(p for p in st.csv if "forces" in p)
        text = (Path(s.active[name].job.run_dir) / forces).read_text()
        assert text.count("# mallard-run ") == 2
    finally:
        s.backend.cancel(s.active[name].job)
