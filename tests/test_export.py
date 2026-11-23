from __future__ import annotations

import csv
import io
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from mallard_gui.csvseries import CsvSeries
from mallard_gui.export import (
    ParaView,
    PlotSpec,
    ViewState,
    caption_frame,
    encode_animation,
    find_paraview,
    frame_indices,
    open_in_paraview,
    plot_csv,
    series_csv,
    series_files,
    state_script,
    svg_pair,
)
from mallard_gui.figstyle import DARK, LIGHT
from mallard_gui.plots import Curve

from .runs import make_run


def test_plot_csv_shares_x_or_lists_points() -> None:
    shared = PlotSpec([Curve("Fx", [0, 1], [1.0, 2.0]), Curve("Fy", [0, 1], [3.0, float("nan")])], "t (s)", "F (N)")
    assert plot_csv(shared).splitlines() == ["t (s),Fx,Fy", "0.0,1.0,3.0", "1.0,2.0,nan"]
    apart = PlotSpec([Curve("a", [0], [1.0]), Curve("b", [5, 6], [2.0, 3.0])], "t (s)", "F (N)")
    assert next(csv.reader(io.StringIO(plot_csv(apart)))) == ["curve", "t (s)", "F (N)"]
    assert len(plot_csv(apart).splitlines()) == 4


def test_series_csv_keeps_every_point_and_the_units() -> None:
    c = CsvSeries("./solut/probe_wake.csv")
    for line in ("step,t,point,x,y,P", "# units: 1,s,1,m,m,Pa", "1,0.1,0,0,0,10", "1,0.1,1,1,0,11", "2,0.2,0,0,0,12"):
        c.add_line(line)
    lines = series_csv(c).splitlines()
    assert lines[:2] == ["t,point,P", "# units: s,1,Pa"]
    assert lines[2:] == ["0.1,0,10.0", "0.2,0,12.0", "0.1,1,11.0"]


def test_svg_pair_in_each_themes_colors(tmp_path: Path) -> None:
    spec = PlotSpec([Curve("Cd", [0, 1, 2], [1.3, 1.4, 1.35])], "t (s)", "Cd", styles=[""])
    light, dark = svg_pair(spec, tmp_path / "drag")
    assert (light.name, dark.name) == ("drag-light.svg", "drag-dark.svg")
    lt, dt = light.read_text(), dark.read_text()
    assert LIGHT["teal"] in lt and DARK["teal"] in dt
    assert "<text" in lt or "Cd" in lt  # real text (svg.fonttype none), not paths
    assert 'fill="#ffffff"' not in lt.split('<g id="axes_1">')[0]  # transparent figure background


def test_gif_has_one_frame_per_snapshot_with_its_caption(tmp_path: Path) -> None:
    frames = []
    for shade in (0, 128, 255):
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (shade, shade, shade)).save(buf, format="JPEG")
        frames.append(caption_frame(buf.getvalue(), "P t 0.1", "cividis", 0.0, 1.0, "P (Pa)"))
    path = encode_animation(frames, tmp_path / "a.gif")
    with Image.open(path) as im:
        assert getattr(im, "n_frames", 1) == 3
    if shutil.which("ffmpeg") is None:
        with pytest.raises(FileNotFoundError):
            encode_animation(frames, tmp_path / "a.mp4")


def test_frame_indices_spread_and_keep_the_last() -> None:
    assert frame_indices(5) == [0, 1, 2, 3, 4]
    picked = frame_indices(1000, 10)
    assert len(picked) == 10 and picked[0] == 0 and picked[-1] == 999


def test_find_paraview_prefers_the_newest_app(tmp_path: Path) -> None:
    for version in ("5.13.2", "6.0.1"):
        app = tmp_path / f"ParaView-{version}.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "MacOS" / "paraview").touch()
        (app / "Contents" / "bin").mkdir()
        (app / "Contents" / "bin" / "pvpython").touch()
    pv = find_paraview(env={}, which=lambda _: None, apps=tmp_path)
    assert pv is not None and pv.gui.name == "ParaView-6.0.1.app" and pv.pvpython is not None
    assert find_paraview(env={}, which=lambda _: None, apps=tmp_path / "none") is None


def test_without_paraview_a_python_state_is_written(tmp_path: Path) -> None:
    path, what = open_in_paraview(None, [tmp_path / "s.pvd"], "P", "Fast", tmp_path, "box")
    assert path.suffix == ".py" and "not found" in what
    text = path.read_text()
    assert "OpenDataFile" in text and "'P'" in text and "lut.ColorSpace = 'Lab'" in text


def test_with_paraview_a_pvsm_is_written_and_opened(tmp_path: Path) -> None:
    launched: list[list[str]] = []

    def fake_pvpython(cmd: list[str], **_: object) -> None:
        script = Path(cmd[-1]).read_text()
        assert "SaveState(" in script
        (tmp_path / "box.pvsm").write_text("<ParaView/>")

    pv = ParaView(Path("/opt/pv/bin/paraview"), Path("/opt/pv/bin/pvpython"))
    path, _ = open_in_paraview(
        pv, [tmp_path / "s.pvd"], "P", "", tmp_path, "box", run=fake_pvpython, spawn=launched.append
    )
    assert path == tmp_path / "box.pvsm"
    assert launched == [["/opt/pv/bin/paraview", f"--state={path}"]]


def away_from_home(tmp: Path) -> Any:
    """Run a command with its home in `tmp`: ParaView writes its own settings (~/.config/ParaView) as it runs,
    and a test leaves the user's alone."""
    import os
    import subprocess

    def run(cmd: list[str], **kwargs: Any) -> Any:
        home = tmp / "home"
        home.mkdir(exist_ok=True)
        env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
        return subprocess.run(cmd, env=env, **kwargs)

    return run


def test_real_pvpython_writes_a_state_of_the_series(tmp_path: Path) -> None:
    pv = find_paraview()
    if pv is None or pv.pvpython is None:
        pytest.skip("no ParaView with pvpython")
    run = make_run(tmp_path / "run", steps=2)
    files = series_files(
        run, "solut/box", [{"path": p.relative_to(run).as_posix()} for p in sorted(run.glob("solut/box_*.vtu"))]
    )
    assert files
    pv_no_gui = ParaView(Path("/usr/bin/true"), pv.pvpython)
    path, _ = open_in_paraview(
        pv_no_gui, files, "P", "cividis", tmp_path / "out", "box", run=away_from_home(tmp_path), spawn=lambda _: None
    )
    text = path.read_text()
    assert path.suffix == ".pvsm" and "<ServerManagerState" in text and "box" in text


VIEW = ViewState(
    t=1.0,
    component="y",
    range=[0.5, 2.5],
    camera={"position": [3.0, 2.0, 4.0], "focal_point": [0.5, 0.5, 0.5], "view_up": [0, 0, 1], "view_angle": 30.0},
    section={"slice": True, "origin": [0.5, 0.5, 0.5], "normal": [1, 0, 0]},
)


def test_the_state_carries_the_view() -> None:
    text = state_script([Path("s.pvd")], "U", "vik", None, VIEW)
    assert "source = Slice(Input=reader)" in text and "source.SliceType.Normal = [1, 0, 0]" in text
    assert "scene.AnimationTime = 1.0" in text and "lut.VectorComponent = 1" in text
    assert "lut.RescaleTransferFunction(0.5, 2.5)" in text
    assert "# Cool to Warm" in text and "lut.ColorSpace = 'Diverging'" in text  # the view's map, a saved name's too
    assert "view.CameraPosition = [3.0, 2.0, 4.0]" in text and "ResetCamera" not in text


def test_real_pvpython_takes_the_view_state(tmp_path: Path) -> None:
    pv = find_paraview()
    if pv is None or pv.pvpython is None:
        pytest.skip("no ParaView with pvpython")
    run = make_run(tmp_path / "run", steps=2)
    files = series_files(
        run, "solut/box", [{"path": p.relative_to(run).as_posix()} for p in sorted(run.glob("solut/box_*.vtu"))]
    )
    pv_no_gui = ParaView(Path("/usr/bin/true"), pv.pvpython)
    path, _ = open_in_paraview(
        pv_no_gui, files, "U", "Fast", tmp_path / "out", "box", run=away_from_home(tmp_path), spawn=lambda _: None,
        view=VIEW,
    )  # fmt: skip
    text = path.read_text()
    assert "<ServerManagerState" in text and "Slice" in text
    lut = re.search(r'<Proxy group="lookup_tables".*?</Proxy>', text, re.S)
    assert lut is not None
    space = re.search(r'name="ColorSpace".*?value="(\d)"', lut.group(0), re.S)
    first = re.search(r'name="RGBPoints".*?index="1" value="([^"]+)"', lut.group(0), re.S)
    assert space and space.group(1) == "2"  # Lab
    assert first and float(first.group(1)) == pytest.approx(0.0564)  # Fast's first color, as the GUI draws it
