"""The results viewer: series from the run log, snapshot headers, the engine (fields, ranges, time, cuts, zone
surfaces, probes and lines over time, budgets and levels of detail, frames), the protocol, and the remote
viewer job through the fake ssh and a Slurm stand-in."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from mallard_gui.backends.ssh import SshConnection
from mallard_gui.config import Install, Remote, RemoteStore
from mallard_gui.data.manager import DataManager
from mallard_gui.project import Project, RunRecord
from mallard_gui.ui.results_page import ResultsPage, camera_calls, zoom_op
from mallard_gui.viewer.catalog import discover, snapshot_header
from mallard_gui.viewer.client import LocalViewer, RemoteViewer, viewer_command, viewer_settings
from mallard_gui.viewer.colormaps import COLORMAPS, color_at, css_gradient, svg_swatch
from mallard_gui.viewer.engine import Budget, ResultScene, ViewerError
from mallard_gui.viewer.protocol import dispatch, serve

from .runs import make_run
from .test_remotes import FAKE_SSH, fake_cluster

JPEG = b"\xff\xd8"


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_run(tmp_path_factory.mktemp("viewer") / "run")


def scene(run: Path, **budget: int) -> ResultScene:
    s = ResultScene(run.parent, Budget(**budget) if budget else None)
    s.open(run.name)
    return s


def test_series_come_from_the_run_log_classified_by_the_input(run: Path) -> None:
    series = {s.id: s for s in discover(run)}
    assert set(series) == {"solut/box", "solut/inlet"}
    assert (series["solut/box"].kind, series["solut/inlet"].kind) == ("volume", "zone")
    assert series["solut/inlet"].region == "inlet"
    assert series["solut/box"].fields == ["P", "U"]
    assert [s.step for s in series["solut/box"].snapshots] == [0, 10, 20, 30]
    assert [s.t for s in series["solut/box"].snapshots] == [0.0, 1.0, 2.0, 3.0]


def test_a_step_written_again_keeps_the_newest_record(tmp_path: Path) -> None:
    run = make_run(tmp_path / "run", steps=2)
    log = run / "mallard.jsonl"
    again = {"type": "output", "step": 10, "t": 1.5, "kind": "vtu", "path": "./solut/box_000000.vtu"}
    log.write_text(log.read_text() + json.dumps({**again, "writer": "./solut/box.pvd"}) + "\n")
    box = next(s for s in discover(run) if s.id == "solut/box")
    assert [(s.step, s.t) for s in box.snapshots] == [(0, 0.0), (10, 1.5)]


def test_without_a_run_log_the_series_indexes_are_read(tmp_path: Path) -> None:
    run = make_run(tmp_path / "run", steps=2)
    (run / "mallard.jsonl").unlink()
    (run / "solut" / "box.pvd").write_text(
        '<VTKFile type="Collection"><Collection>'
        '<DataSet timestep="0" file="box_000000.vtu"/><DataSet timestep="1" file="box_000010.vtu"/>'
        "</Collection></VTKFile>"
    )
    (box,) = discover(run)
    assert box.id == "solut/box" and box.kind == "volume"
    assert [(s.path, s.step, s.t) for s in box.snapshots] == [
        ("solut/box_000000.vtu", 0, 0.0),
        ("solut/box_000010.vtu", 10, 1.0),
    ]


def test_header_gives_fields_units_and_cells_without_the_data(run: Path) -> None:
    h = snapshot_header(run / "solut" / "box_000010.vtu")
    assert (h["step"], h["time"], h["cells"]) == (10, 1.0, 216)
    assert [(f["name"], f["units"]) for f in h["fields"]] == [("P", "Pa"), ("U", "m s-1")]
    assert snapshot_header(run / "solut" / "inlet_000010.vtp")["cells"] == 36


def test_show_draws_the_volume_and_zone_surfaces_of_the_same_time(run: Path) -> None:
    s = scene(run)
    info = s.show("solut/box", 2, zones=["solut/inlet"])
    assert (info["step"], info["t"], info["count"]) == (20, 2.0, 4)
    assert info["field"] == "P"
    assert info["range"] == pytest.approx([2 + 1 / 12, 2 + 11 / 12])
    assert {b["name"] for b in info["blocks"]} == {"field", "zone:solut/inlet"}
    assert "domain" in s.blocks  # the outline
    assert s.layers["zone:solut/inlet"].snapshot.step == 20
    assert [f["units"] for f in info["fields"]] == ["Pa", "m s-1"]


def test_field_component_colormap_and_fixed_range(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 1)
    info = s.set_field(field="U", component="z")
    assert info["range"][0] < 1.0 < info["range"][1]  # U_z = t everywhere: a range padded around it
    info = s.set_field(component="x", colormap="Viridis")
    assert info["range"] == pytest.approx([1 / 12, 11 / 12])
    assert s.set_field(range=[0, 5])["range"] == [0, 5]
    assert s.show("solut/box", 3)["range"] == [0, 5]  # fixed across time
    assert s.set_field(auto_range=True)["auto_range"] is True
    with pytest.raises(ViewerError, match="min < max"):
        s.set_field(range=[1, 1])
    with pytest.raises(ViewerError, match="colormap"):
        s.set_field(colormap="jet")


def test_a_diverging_map_centers_zero_only_for_signed_data(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 0)
    lo, _ = s.set_field(colormap="Cool to Warm")["range"]
    assert lo > 0  # P = x here: unsigned, its own range
    p = s.layers["field"].data.GetCellData().GetArray("P")
    p.SetValue(0, -2.0)
    p.Modified()
    assert s.shown_range() == (-2.0, 2.0)


def test_slice_cuts_the_volume_on_the_section_plane(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 0)
    sec = s.set_section(slice=True, clip=True, origin=[0.5, 0.5, 0.5], normal=[0, 0, 1])
    assert sec["clip"] and sec["slice"] and sec["slice_cells"] > 0
    assert s.blocks["slice"].actor.GetMapper().GetNumberOfClippingPlanes() == 0
    assert s.blocks["field"].actor.GetMapper().GetNumberOfClippingPlanes() == 1
    assert s.set_section(slice=False)["slice_cells"] == 0


def test_probe_and_line_over_time(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 0)
    p = s.probe("solut/box", [0.25, 0.4, 0.5], [0, 1, 2, 3], ["P"])
    assert p["t"] == [0.0, 1.0, 2.0, 3.0]
    assert p["values"]["P"] == pytest.approx([t + 0.25 for t in (0, 1, 2, 3)])  # the cell at x 1/6..2/6
    line = s.line("solut/box", [0.01, 0.5, 0.5], [0.99, 0.5, 0.5], [0, 3], "P", resolution=10)
    assert len(line["curves"]) == 2 and len(line["s"]) == 11
    assert line["curves"][1]["values"][0] == pytest.approx(3 + 1 / 12)
    outside = s.probe("solut/box", [5, 5, 5], [0], ["P"])
    assert outside["values"]["P"] == [None]
    with pytest.raises(ViewerError, match="at most"):
        scene(run, probe_snapshots=2).probe("solut/box", [0, 0, 0], [0, 1, 2], ["P"])


def test_a_volume_over_budget_is_never_read_but_its_zone_surfaces_are(run: Path) -> None:
    s = scene(run, volume_cells=100)
    info = s.show("solut/box", 1)
    assert "over this viewer's budget of 100" in info["notice"]
    assert "field" not in s.layers and "zone:solut/inlet" in s.layers
    assert info["zones"] == ["solut/inlet"]
    assert info["over_budget"] > 100  # its cells: what the parallel viewer job is planned from
    assert scene(run).show("solut/box", 1)["over_budget"] == 0


def test_levels_of_detail_within_the_triangle_budgets(run: Path) -> None:
    s = scene(run, triangles=150, interactive_triangles=60)
    s.show("solut/box", 0)
    levels = s.info()["levels"]["field"]
    assert len(levels) == 2 and levels[0] <= 150 and levels[1] <= 60
    moving = s.frame(400, 300, interactive=True)
    assert (moving["width"], moving["height"]) == (200, 150)
    assert s.layers["field"].level == 1
    assert moving["triangles"] < s.info()["triangles"]
    still = s.frame(400, 300)
    assert still["image"][:2] == JPEG and (still["width"], still["height"]) == (400, 300)
    assert s.layers["field"].level == 0
    capped = scene(run, frame_pixels=100 * 100).frame(400, 400)
    assert capped["width"] * capped["height"] <= 100 * 100


def test_camera_and_picking_a_point(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 0)
    s.camera("preset", preset="+Z")
    s.frame(300, 300)
    hit = s.pick_point(150, 150, 300, 300)
    assert hit is not None and hit["block"] == "field"
    assert hit["point"][0] == pytest.approx(0.5, abs=0.05) and hit["point"][1] == pytest.approx(0.5, abs=0.05)
    before = s.renderer.GetActiveCamera().GetPosition()
    s.camera("orbit", dx=0.1)
    s.camera("pan", dx=0.1, dy=0.1)
    s.camera("zoom", factor=2)
    assert s.renderer.GetActiveCamera().GetPosition() != before
    with pytest.raises(ViewerError, match="camera op"):
        s.camera("spin")


def test_pan_follows_the_pointer(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 0)
    s.camera("preset", preset="-Z")
    s.window.SetSize(300, 300)

    def screen_x() -> float:
        s.renderer.SetWorldPoint(0.5, 0.5, 0.5, 1.0)
        s.renderer.WorldToDisplay()
        return float(s.renderer.GetDisplayPoint()[0])

    before = screen_x()
    s.camera("pan", dx=0.2)  # the pointer moves right by a fifth of the view
    assert screen_x() - before == pytest.approx(60, abs=6)


def test_focus_frames_the_zone_surfaces_shown(run: Path) -> None:
    s = scene(run)
    s.window.SetSize(300, 300)
    s.show("solut/box", 0, zones=[])
    with pytest.raises(ViewerError, match="no zone surface"):
        s.camera("focus")
    s.show("solut/box", 0, zones=["solut/inlet"])
    s.camera("preset", preset="+Z")
    s.camera("focus")
    s.renderer.SetWorldPoint(0.0, 0.5, 0.5, 1.0)  # the inlet face's center
    s.renderer.WorldToDisplay()
    assert s.renderer.GetDisplayPoint()[:2] == pytest.approx((150, 150), abs=1)


def test_open_stays_under_the_root(run: Path) -> None:
    s = ResultScene(run)
    with pytest.raises(ViewerError, match="outside"):
        s.open("/etc")
    assert s.open(".")["dim"] == 3


def test_probe_and_line_markers_are_drawn_and_replaced(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 0)
    r = dispatch(s, {"id": 1, "method": "set_markers", "params": {"points": [["P", [0.25, 0.4, 0.5]]]}})
    assert r["result"] == {"markers": 1, "lines": 0}
    assert len(s.overlay_layers["markers"]) == 1 and len(s._text_markers) == 1
    s.set_markers([["a", [0, 0, 0]], ["b", [1, 0, 0]]], [[[0, 0, 0], [1, 0, 0]]])
    assert len(s.overlay_layers["markers"]) == 3 and len(s._text_markers) == 2
    s.set_markers()
    assert "markers" not in s.overlay_layers and not s._text_markers


def test_protocol_dispatch_and_frames(run: Path) -> None:
    s = ResultScene(run.parent)
    assert dispatch(s, {"id": 1, "method": "open", "params": {"run": run.name}})["result"]["dim"] == 3
    assert "unknown method" in dispatch(s, {"id": 2, "method": "read", "params": {}})["error"]
    assert "unknown method" in dispatch(s, {"id": 2, "method": "_file", "params": {}})["error"]
    assert "ViewerError" in dispatch(s, {"id": 3, "method": "show", "params": {"series": "nope", "index": 0}})["error"]
    v = LocalViewer(run.parent)
    try:
        assert v.call("open", run=run.name)["dim"] == 3
        v.call("show", series="solut/box", index=0)
        frame = v.call("frame", width=64, height=48)
        assert frame["image"][:2] == JPEG
        assert v.info["pid"] != os.getpid()  # its own process: VTK's window on that process's main thread
    finally:
        v.close()
    assert v.proc.returncode == 0


def test_serve_ends_when_the_client_closes_or_after_idle(run: Path, tmp_path: Path) -> None:
    s = ResultScene(run.parent)
    r, w = os.pipe()
    os.write(w, json.dumps({"id": 7, "method": "open", "params": {"run": run.name}}).encode() + b"\nnot json\n")
    os.close(w)  # the client has gone
    replies = tmp_path / "replies"
    with os.fdopen(r, "rb") as inp, replies.open("wb") as out:
        assert serve(s, inp, out, idle=30) == "closed"
    hello, opened, bad = (json.loads(line) for line in replies.read_bytes().splitlines())
    assert hello["hello"] == "mallard-viewer" and opened["id"] == 7 and "bad request" in bad["error"]
    r, w = os.pipe()
    with os.fdopen(r, "rb") as inp, open(os.devnull, "wb") as out:
        assert serve(s, inp, out, idle=0.3) == "idle"
    os.close(w)


def test_viewer_command_runs_one_task_under_srun(tmp_path: Path) -> None:
    r = Remote("hpc", "hpc", installs=[Install("/opt/mallard/0.10.0-cpu/bin/mallard")], defaults={"account": "cfd"})
    assert viewer_settings(r)["python"] == "/opt/mallard/0.10.0-cpu/viewer/bin/python"
    r.viewer = {"partition": "viz", "time": "00:30:00", "idle": "60"}
    argv = viewer_command(r, "/scratch")
    assert argv[0] == "srun" and "--partition=viz" in argv and "--account=cfd" in argv and "--time=00:30:00" in argv
    assert argv[-8:] == [
        "/opt/mallard/0.10.0-cpu/viewer/bin/python",
        "-m",
        "mallard_gui.viewer",
        "--stdio",
        "--root",
        "/scratch",
        "--idle",
        "60",
    ]
    r.viewer["launch"] = "login"
    assert viewer_command(r, "/")[0].endswith("python")
    r.viewer["launch"] = "ssh"
    with pytest.raises(ViewerError, match="viewer launch"):
        viewer_command(r, "/")


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_cluster(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_HOMES", str(tmp_path / "homes"))
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    return tmp_path


def test_remote_viewer_job_renders_next_to_the_data(cluster: Path) -> None:
    run = make_run(cluster / "homes" / "hpc" / "scratch" / "run")
    remote = Remote("hpc", "hpc", installs=[Install("/opt/m/bin/mallard")], viewer={"python": sys.executable})
    conn = SshConnection("hpc", control_dir=cluster / "ctl", ssh=FAKE_SSH)
    v = RemoteViewer(conn, remote, str(run.parent))
    try:
        assert v.info["hello"] == "mallard-viewer"
        assert v.call("open", run=str(run))["series"][0]["id"] == "solut/box"
        assert v.call("show", series="solut/box", index=3, zones=[])["t"] == 3.0
        assert v.call("frame", width=80, height=60)["image"][:2] == JPEG
        assert v.call("probe", series="solut/box", point=[0.9, 0.5, 0.5], indices=[0, 1], fields=["P"])["values"][
            "P"
        ] == pytest.approx([11 / 12, 1 + 11 / 12])
        with pytest.raises(ViewerError, match="outside"):
            v.call("open", run="/etc")
    finally:
        v.close()
    assert v.proc.returncode == 0
    log = (cluster / "ssh.log").read_text()
    assert "srun --job-name=mallard-viewer" in log and "mallard_gui.viewer --stdio" in log


def test_remote_viewer_reports_a_job_that_fails_to_start(cluster: Path) -> None:
    remote = Remote("hpc", "hpc", installs=[Install("/opt/m/bin/mallard")], viewer={"python": "/nonexistent/python"})
    conn = SshConnection("hpc", control_dir=cluster / "ctl", ssh=FAKE_SSH)
    with pytest.raises(ViewerError, match="exited"):
        RemoteViewer(conn, remote, "/")


def test_session_opens_a_remote_run_with_one_viewer_per_remote(
    cluster: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mallard_gui.session import Session

    from .conftest import FakeMallard

    monkeypatch.setenv("MALLARD_GUI_SSH", FAKE_SSH)
    run = make_run(cluster / "homes" / "hpc" / "scratch" / "run")
    store = RemoteStore()
    store.add(Remote("hpc", "hpc", installs=[Install("/opt/m/bin/mallard")], viewer={"python": sys.executable}))
    store.save()
    project = tmp_path / "case"
    project.mkdir()
    (project / "input.toml").write_text("[run]\nt_stop = 1.0\n")
    Project.open(project).save_run(RunRecord("r", home={"remote": "hpc", "directory": str(run)}))
    Project.open(project).save_run(RunRecord("here", home={"remote": "local", "directory": str(run)}))
    session = Session(project, FakeMallard())  # type: ignore[arg-type]
    session._data = DataManager(session.project)
    try:
        viewer, root = session.viewer(session.project.run("r"))
        assert isinstance(viewer, RemoteViewer) and root == str(run)
        assert viewer.call("open", run=root)["dim"] == 3
        again, _ = session.viewer(session.project.run("r"))
        assert again is viewer
        local, here = session.viewer(session.project.run("here"))
        assert isinstance(local, LocalViewer) and Path(here) == run.resolve()
    finally:
        session.close_viewers()
    assert viewer.proc.returncode == 0 and local.proc.returncode == 0 and not session.viewers


def test_colormaps_are_paraviews_interpolated_in_their_own_color_space() -> None:
    """ParaView 6.2.1's colors (pvpython, the preset applied over [0, 1]) between control points, where Lab
    (Fast) and Diverging (Cool to Warm) differ from interpolating in RGB."""
    assert COLORMAPS[0] == "Fast"
    assert color_at("Fast", 0.365) == pytest.approx((0.5412, 0.8353, 0.9373), abs=0.004)
    assert color_at("Cool to Warm", 0.25) == pytest.approx((0.5529, 0.6902, 0.9961), abs=0.004)
    assert color_at("vik", 0.25) == color_at("Cool to Warm", 0.25)  # a saved view's former name
    assert len({svg_swatch(n).split('id="')[1].split('"')[0] for n in COLORMAPS}) == len(COLORMAPS)
    assert css_gradient("Jet").startswith("linear-gradient(to right, rgb(0,0,143) 0%")


def test_merged_image_zooms_keep_each_cursor_point_in_place(run: Path) -> None:
    wheel = [(1.3, 0.75, 0.3), (1.2, 0.7, 0.35), (0.9, 0.2, 0.8)]
    by_event, merged = scene(run), scene(run)
    for s in (by_event, merged):
        s.show("solut/box", 0)
        s.camera("preset", preset="+Z")
        s.frame(400, 300)
    for f, nx, ny in wheel:
        by_event.zoom_at(f, nx, ny)
    ops = ResultsPage._merge([zoom_op(f, nx, ny) for f, nx, ny in wheel])
    assert len(ops) == 1
    for call in camera_calls(ops):
        merged.camera(**call)
    a, b = by_event.renderer.GetActiveCamera(), merged.renderer.GetActiveCamera()
    assert a.GetFocalPoint() == pytest.approx(b.GetFocalPoint(), abs=1e-9)
    assert a.GetPosition() == pytest.approx(b.GetPosition(), abs=1e-9)


def test_a_camera_move_survives_frames_of_another_size(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 0)
    s.frame(400, 300)  # the first frame frames the view
    s.camera("zoom", factor=2.0)
    moved = s.renderer.GetActiveCamera().GetPosition()
    s.frame(200, 150, interactive=True)  # the coarse frames while moving are half size
    s.frame(400, 300)
    assert s.renderer.GetActiveCamera().GetPosition() == pytest.approx(moved)


def test_statistics_of_the_shown_field_over_the_domain_and_the_slice(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 1)
    s.set_field(field="P")  # P = t + x at the cell centers of the unit box, t = 1
    got = s.stats()["domain"]
    assert got["mean"] == pytest.approx(1.5) and got["integral"] == pytest.approx(1.5)
    assert got["measure"] == pytest.approx(1.0) and got["argmin"][0] == pytest.approx(1 / 12)
    s.set_section(slice=True, origin=[0.5, 0.5, 0.5], normal=[0, 0, 1])
    cut = s.stats()["slice"]  # the plane z = 0.5 through the box: its area, P averaged onto it
    assert cut["measure"] == pytest.approx(1.0) and cut["mean"] == pytest.approx(1.5, abs=0.02)


def test_a_frame_of_another_field_leaves_the_shown_one(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 1)
    s.set_field(field="P")
    before = s.frame(64, 48)["image"]
    other = s.frame_field("U", "y", 64, 48)  # P = t + x; U = (x, y, t)
    u = s.layers["field"].data.GetCellData().GetArray("U") or s.layers["field"].data.GetPointData().GetArray("U")
    assert other["range"] == pytest.approx(list(u.GetRange(1))) and other["image"] != before
    assert s.info()["field"] == "P" and s.frame(64, 48)["image"] == before
    with pytest.raises(ViewerError):
        s.frame_field("nope")


def test_cell_values_or_interpolated_for_the_view_slice_and_geometry_but_not_statistics(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 2)
    s.set_section(slice=True)
    stats = s.stats()
    mapper = s.blocks["field"].actor.GetMapper()
    cut = s.blocks["slice"].actor.GetMapper()
    assert s.info()["interpolation"] == "cells"
    assert mapper.GetScalarModeAsString() == cut.GetScalarModeAsString() == "UseCellFieldData"
    flat = s.frame(400, 300)["image"]
    blocks = s.geometry()["blocks"]
    assert all(b["n_points"] == 3 * b["n_triangles"] for b in blocks if b["name"] == "field")  # flat cells
    assert s.set_field(interpolation="linear")["interpolation"] == "linear"
    assert mapper.GetScalarModeAsString() == cut.GetScalarModeAsString() == "UsePointFieldData"
    assert s.frame(400, 300)["image"] != flat
    assert all(b["n_points"] < 3 * b["n_triangles"] for b in s.geometry()["blocks"] if b["name"] == "field")
    assert s.stats() == stats  # always the cells Mallard wrote
    with pytest.raises(ViewerError, match="interpolation"):
        s.set_field(interpolation="cubic")


PARTICLES = """\
# mallard v0.11.0 (main abc1234) 3D double
# step 30, t = 3
id,x,y,z,v_x,v_y,v_z,turns
0,0.1,0.2,0.3,3,4,0,1.5
1,0.5,0.5,0.5,0,0,0,-0.5
2,0.9,0.8,0.7,1,2,2,0.25
"""


@pytest.mark.parametrize("header", ["V_X,V_Y,V_Z", "v_x,v_y,v_z"])  # Mallard's names, and before #1466
def test_the_particle_file_is_drawn_colored_by_its_first_quantity(tmp_path: Path, header: str) -> None:
    run = make_run(tmp_path / "run")
    (run / "input.toml").write_text((run / "input.toml").read_text() + '[particles]\nfile = "out/cloud.csv"\n')
    (run / "out").mkdir()
    (run / "out" / "cloud.csv").write_text(PARTICLES.replace("v_x,v_y,v_z", header))
    cloud = next(x for x in discover(run) if x.kind == "particles")
    assert (cloud.id, cloud.fields) == ("out/cloud", ["V_X", "V_Y", "V_Z", "turns"])
    assert (cloud.snapshots[0].step, cloud.snapshots[0].t) == (30, 3.0)
    s = scene(run)
    p = s.show("solut/box", 1, particles=["out/cloud"])["particles"]
    assert (p["count"], p["step"], p["field"], p["range"]) == (3, 30, "turns", [-0.5, 1.5])
    assert p["fields"] == ["V_X", "V_Y", "V_Z", "turns", "speed", "id"]
    poly = s._particles["particles:out/cloud"][1]
    assert poly.GetPoint(2) == pytest.approx((0.9, 0.8, 0.7))
    assert s.set_particles(field="speed")["particles"]["range"] == [0.0, 5.0]
    mapper = s.blocks["particles:out/cloud"].actor.GetMapper()
    assert mapper.GetScalarVisibility() and mapper.GetArrayName() == "speed"
    assert s.set_particles(field="")["particles"]["range"] is None
    assert not mapper.GetScalarVisibility()
    assert s.show("solut/box", 1, particles=[])["particles"]["shown"] == []
    assert "particles:out/cloud" not in s.blocks
