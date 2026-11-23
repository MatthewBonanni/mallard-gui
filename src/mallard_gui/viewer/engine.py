"""The viewer engine: Mallard's snapshots rendered with VTK where they are, as images.

It reuses the case view's scene (navigation presets, the section tool's clip plane, x-ray, isolate,
picking), and adds fields with a colormap and range, time across a series' snapshots, cuts through the
volume on the plane, zone surfaces, and probes and lines over time.

Budgets keep it bounded whatever the mesh (the design's Scale rules): a snapshot's header gives its
cell count before any data is read, and a volume above `Budget.volume_cells` is never loaded (zone
surfaces still are); surfaces above `Budget.triangles` are decimated, and while the camera moves a
coarser level of at most `Budget.interactive_triangles` is drawn at half resolution, full quality
returning on release. Snapshots read are kept in memory up to `Budget.cache_bytes`.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from vtkmodules.util.numpy_support import vtk_to_numpy
from vtkmodules.vtkCommonCore import vtkLookupTable, vtkPoints
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkDataSet, vtkPolyData, vtkStaticCellLocator
from vtkmodules.vtkFiltersCore import (
    vtkAppendPolyData,
    vtkCellCenters,
    vtkCellDataToPointData,
    vtkCutter,
    vtkPointDataToCellData,
    vtkProbeFilter,
    vtkTriangleFilter,
)
from vtkmodules.vtkFiltersGeometry import vtkDataSetSurfaceFilter
from vtkmodules.vtkFiltersSources import vtkLineSource, vtkOutlineSource
from vtkmodules.vtkFiltersVerdict import vtkCellSizeFilter
from vtkmodules.vtkIOHDF import vtkHDFReader
from vtkmodules.vtkIOXML import vtkXMLGenericDataObjectReader
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

from ..grouping import grouped
from ..view.scene import OKABE_ITO, THEMES, Block, CaseScene
from . import geometry as geom
from . import lod as lod_mod
from .catalog import Series, Snapshot, discover, particle_columns, snapshot_header
from .colormaps import COLORMAPS, DEFAULT, DIVERGING, LEGACY, canonical, color_at, symmetric
from .lod import LodSurface
from .stats import field_stats

COMPONENTS = ("magnitude", "x", "y", "z")
# Each cell flat in its value, or the values averaged to the points and interpolated linearly between them
INTERPOLATIONS = ("cells", "linear")
VIEWABLE_EXT = {".vtu": "vtu", ".vtp": "vtp", ".vtkhdf": "vtkhdf", ".hdf": "vtkhdf"}


class ViewerError(RuntimeError):
    pass


class BudgetError(ViewerError):
    def __init__(self, message: str, cells: int = 0) -> None:
        super().__init__(message)
        self.cells = cells  # of the snapshot over the volume budget, when that is why


@dataclass
class Budget:
    volume_cells: int = 5_000_000  # O(cells) data one viewer process may hold
    triangles: int = 2_000_000  # a surface drawn at full quality; decimated above
    interactive_triangles: int = 200_000  # the level drawn while the camera moves
    frame_pixels: int = 2880 * 1800
    cache_bytes: int = 512 << 20  # snapshots kept in memory
    probe_snapshots: int = 400  # snapshots read by one probe request
    geometry_triangles: int = 2_000_000  # the most geometry mode sends the browser


@dataclass
class Layer:
    """A block's data: the dataset it came from and its levels of detail, finest first."""

    series: str
    snapshot: Snapshot
    data: vtkDataSet
    levels: list[vtkPolyData]
    level: int = 0


class ResultScene(CaseScene):
    XRAY_KINDS = ("zone", "region", "field")
    UNCLIPPED_KINDS = ("slice", "section")

    def __init__(self, root: Path, budget: Budget | None = None, theme: str = "dark", offscreen: bool = True) -> None:
        self.layers: dict[str, Layer] = {}  # before the scene: its theme is set as it is made, and marks the plane
        self.particle_ids: list[str] = []
        self.particle_field = ""  # "": one color
        self._particles: dict[str, tuple[Snapshot, vtkPolyData]] = {}
        self.slice_enabled = False
        super().__init__(offscreen=offscreen, theme=theme)
        self.root = root.resolve()
        self.budget = budget or Budget()
        self.run_dir: Path | None = None
        self.series: dict[str, Series] = {}
        self.primary = ""
        self.index = 0
        self.zone_ids: list[str] = []
        self.field = ""
        self.component = "magnitude"
        self.colormap = DEFAULT
        self.interpolation = "cells"  # how a cell field is drawn: INTERPOLATIONS
        self.range: tuple[float, float] | None = None  # None: the current snapshot's range
        self.notice = ""
        self.over_budget = 0  # cells of the volume shown as zone surfaces only, as it is over the budget
        self.interactive = False
        self._cache: OrderedDict[tuple[str, int], tuple[vtkDataSet, int]] = OrderedDict()
        self._surfaces: dict[tuple[str, int], list[vtkPolyData]] = {}  # a cached snapshot's levels, with it
        self._geometries: OrderedDict[int, tuple[vtkDataSet, tuple[Any, Any]]] = OrderedDict()
        self._cached_bytes = 0
        self._camera_set = False
        self._lut: vtkLookupTable | None = None
        self.lods: dict[str, LodSurface] = {}
        self._text_markers: list[Any] = []

    # Runs and series

    def open(self, run: str) -> dict[str, Any]:
        path = Path(run)
        path = (path if path.is_absolute() else self.root / path).resolve()
        if path != self.root and self.root not in path.parents:
            raise ViewerError(f"{run}: outside the viewer's root {self.root}")
        if not path.is_dir():
            raise ViewerError(f"{run}: no such run directory")
        self.run_dir = path
        self.series = {s.id: s for s in discover(path)}
        self._expand_derived()
        self.dim = _run_dim(path) or 3
        self.lock_2d = self.dim == 2
        # A run opens whole: no section left from the last one, and a plane that cuts it (z cuts no 2D run).
        self.clip_enabled = self.slice_enabled = self.keep_flipped = False
        self.clip_normal = (1.0, 0.0, 0.0) if self.dim == 2 else (0.0, 0.0, 1.0)
        self.xray = False
        self.clear()
        self.layers.clear()
        self._cache.clear()
        self._surfaces.clear()
        self._cached_bytes = 0
        self.primary, self.zone_ids, self.field, self.range = "", [], "", None
        self.particle_ids, self.particle_field = [], ""
        self._particles.clear()
        self._camera_set = False
        return {"dim": self.dim, "series": [s.to_dict() for s in self.series.values()]}

    def rescan(self) -> dict[str, Any]:
        """The run's series again, keeping what is shown: after a post-processing job wrote new ones."""
        if self.run_dir is None:
            raise ViewerError("no run open")
        self.series = {s.id: s for s in discover(self.run_dir)}
        self._expand_derived()
        return {"dim": self.dim, "series": [s.to_dict() for s in self.series.values()]}

    def _expand_derived(self) -> None:
        """A derived VTKHDF file's steps, with their times, from its own metadata."""
        assert self.run_dir is not None
        for s in self.series.values():
            if s.kind != "derived" or s.format != "vtkhdf" or len(s.snapshots) != 1:
                continue
            path = self.run_dir / s.snapshots[0].path
            reader = vtkHDFReader()
            reader.SetFileName(str(path))
            try:
                reader.UpdateInformation()
            except RuntimeError:
                continue
            n = int(reader.GetNumberOfSteps()) if reader.GetNumberOfSteps() else 1
            times = list(reader.GetOutputInformation(0).Get(_TIME_STEPS()) or []) if n > 1 else []
            if n > 1:
                s.snapshots = [
                    Snapshot(s.snapshots[0].path, k, times[k] if k < len(times) else None, k) for k in range(n)
                ]

    def _series(self, sid: str) -> Series:
        if sid not in self.series:
            raise ViewerError(f"no series {sid!r} in this run")
        return self.series[sid]

    def header(self, sid: str, index: int) -> dict[str, Any]:
        s = self._series(sid)
        snap = s.snapshots[index]
        return snapshot_header(self._file(snap))

    def _file(self, snap: Snapshot) -> Path:
        assert self.run_dir is not None
        return self.run_dir / snap.path

    def read(self, sid: str, index: int) -> vtkDataSet:
        """A snapshot's dataset, from the cache or its file, once its header fits the budget."""
        s = self._series(sid)
        if not 0 <= index < len(s.snapshots):
            raise ViewerError(f"{sid}: no snapshot {index} (it has {len(s.snapshots)})")
        key = (sid, index)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key][0]
        snap = s.snapshots[index]
        path = self._file(snap)
        if not path.is_file():
            raise ViewerError(f"{snap.path}: missing")
        cells = snapshot_header(path)["cells"] if s.format != "vtkhdf" else None
        if cells is not None and cells > self.budget.volume_cells:
            raise BudgetError(
                f"{s.label}: {grouped(cells)} cells is over this viewer's budget of "
                f"{grouped(self.budget.volume_cells)}",
                cells,
            )
        data = _read_dataset(path, s.format, snap.index)
        if data.GetNumberOfCells() > self.budget.volume_cells:
            raise BudgetError(
                f"{s.label}: {data.GetNumberOfCells()} cells is over this viewer's budget", data.GetNumberOfCells()
            )
        size = int(data.GetActualMemorySize()) * 1024
        self._cache[key] = (data, size)
        self._cached_bytes += size
        self._evict()
        return data

    def _evict(self) -> None:
        while self._cached_bytes > self.budget.cache_bytes and len(self._cache) > 1:
            key, (_, dropped) = self._cache.popitem(last=False)
            self._surfaces.pop(key, None)
            self._cached_bytes -= dropped

    def _surface_levels(self, sid: str, index: int, data: vtkDataSet) -> list[vtkPolyData]:
        """A snapshot's levels, made once while it is cached: going back to a snapshot redraws it at once."""
        key = (sid, index)
        if key in self._surfaces:
            return self._surfaces[key]
        levels = self._levels(data)
        if key in self._cache and self._cache[key][0] is data:
            size = sum(int(lv.GetActualMemorySize()) * 1024 for lv in levels)
            self._cache[key] = (data, self._cache[key][1] + size)
            self._cached_bytes += size
            self._surfaces[key] = levels
            self._evict()
        return levels

    def index_at(self, sid: str, step: int | None, t: float | None) -> int:
        """The snapshot of a series nearest a step (else a time): zone surfaces follow the volume's time."""
        snaps = self._series(sid).snapshots
        if not snaps:
            raise ViewerError(f"{sid}: no snapshots")
        if step is not None and any(s.step is not None for s in snaps):
            return min(range(len(snaps)), key=lambda i: abs((snaps[i].step or 0) - step))
        return min(range(len(snaps)), key=lambda i: abs((snaps[i].t or 0.0) - (t or 0.0)))

    # What is shown

    def show(
        self,
        series: str,
        index: int,
        zones: Sequence[str] | None = None,
        particles: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """The primary series at a snapshot, with the zone surfaces and the particles of the same time."""
        s = self._series(series)
        index = max(0, min(int(index), len(s.snapshots) - 1))
        self.primary, self.index = series, index
        if zones is not None:
            self.zone_ids = [z for z in zones if z in self.series and z != series]
        if particles is not None:
            self.particle_ids = [p for p in particles if p in self.series and self.series[p].kind == "particles"]
        snap = s.snapshots[index]
        self.notice = ""
        self.over_budget = 0
        wanted: dict[str, tuple[str, int, str]] = {}
        try:
            if self._lod(series, index) is None:
                self.read(series, index)  # its levels of detail stand in for a volume of any size
            wanted["field"] = (series, index, "field")
        except BudgetError as e:
            self.notice = f"{e}: zone surfaces only"
            self.over_budget = e.cells
            if zones is None and not self.zone_ids:
                self.zone_ids = [z.id for z in self.series.values() if z.kind == "zone"]
        for zid in self.zone_ids:
            wanted[f"zone:{zid}"] = (zid, self.index_at(zid, snap.step, snap.t), "zone")
        self._sync_blocks(wanted)
        self._sync_particles(snap)
        if not self.field or self.field not in self.field_names():
            self.field = _default_field(self.field_names(), s.fields)
        self._update_slice()
        self._apply_field()
        self._apply_particles()
        if not self._camera_set and self.blocks:
            self.bounds = self.renderer.ComputeVisiblePropBounds()
            self.clip_origin = tuple((self.bounds[2 * i] + self.bounds[2 * i + 1]) / 2 for i in range(3))  # type: ignore[assignment]
            self.reset_camera()
            self._camera_set = True
        self._apply_clip()
        self._apply_styles()
        return self.info()

    def _sync_blocks(self, wanted: dict[str, tuple[str, int, str]]) -> None:
        kept = ("slice", "domain", "section")
        for name in [n for n in self.blocks if n not in wanted and n not in kept and not n.startswith("particles:")]:
            for a in self.blocks[name].actors:
                self.renderer.RemoveActor(a)
            del self.blocks[name]
            self.layers.pop(name, None)
            self.lods.pop(name, None)
        colors = 0
        for name, (sid, index, kind) in wanted.items():
            layer = self.layers.get(name)
            snap = self.series[sid].snapshots[index]
            if layer is not None and layer.snapshot is snap:
                continue
            lod = self._lod(sid, index)
            data: vtkDataSet
            if lod is not None:
                self.lods[name] = lod
                coarse = lod.coarsest()
                data, levels = coarse, [coarse]
            else:
                self.lods.pop(name, None)
                data = self.read(sid, index)
                levels = self._surface_levels(sid, index, data)
            if name in self.blocks:
                block = self.blocks[name]
                _mapper(block.actor).SetInputData(levels[0])
                block.cells = data.GetNumberOfCells()
            else:
                color = OKABE_ITO[colors % len(OKABE_ITO)] if kind == "zone" else THEMES[self.theme]["mesh"]
                actor = _actor(levels[0])
                if kind == "zone" and _dimension(data) == 1:
                    actor.GetProperty().SetLineWidth(3.0)
                block = Block(
                    name, self.series[sid].label, kind, color, None, _dimension(data), data.GetNumberOfCells(), actor
                )
                self.blocks[name] = block
                self.renderer.AddActor(actor)
            if kind == "zone":
                colors += 1
            self.layers[name] = Layer(sid, snap, data, levels)
        self._outline()

    def _sync_particles(self, snap: Snapshot) -> None:
        """The particles of each shown particle series at the snapshot nearest the primary's time."""
        wanted = {f"particles:{sid}": (sid, self.index_at(sid, snap.step, snap.t)) for sid in self.particle_ids}
        for name in [n for n in self.blocks if n.startswith("particles:") and n not in wanted]:
            for a in self.blocks[name].actors:
                self.renderer.RemoveActor(a)
            del self.blocks[name]
            self._particles.pop(name, None)
        for name, (sid, index) in wanted.items():
            psnap = self.series[sid].snapshots[index]
            if name in self._particles and self._particles[name][0] is psnap:
                continue
            poly = _read_particles(self._file(psnap))
            self._particles[name] = (psnap, poly)
            if name in self.blocks:
                _mapper(self.blocks[name].actor).SetInputData(poly)
                self.blocks[name].cells = poly.GetNumberOfPoints()
                continue
            actor, halo = _actor(poly), _actor(poly)
            for a, size in ((actor, PARTICLE_PX), (halo, PARTICLE_PX + 2 * HALO_PX)):
                p = a.GetProperty()
                p.SetRepresentationToPoints()
                p.SetPointSize(size)
                p.RenderPointsAsSpheresOn()
                self.renderer.AddActor(a)
            halo.GetMapper().ScalarVisibilityOff()
            halo.GetProperty().SetColor(0.05, 0.05, 0.05)
            halo.GetProperty().LightingOff()
            halo.PickableOff()
            label = self.series[sid].label
            block = Block(name, label, "particles", OKABE_ITO[-1], None, 0, poly.GetNumberOfPoints(), actor, [halo])
            self.blocks[name] = block
        names = self.particle_field_names()
        if self.particle_field not in names:
            self.particle_field = next((n for n in names if n not in (*_PARTICLE_STATE, "id")), "")
            if not self.particle_field and "speed" in names:
                self.particle_field = "speed"

    def particle_field_names(self) -> list[str]:
        names: list[str] = []
        for _, poly in self._particles.values():
            pd = poly.GetPointData()
            for i in range(pd.GetNumberOfArrays()):
                name = pd.GetArrayName(i)
                if name and name != "id" and name not in names:
                    names.append(name)
        return names + (["id"] if self._particles else [])

    def particle_range(self) -> tuple[float, float] | None:
        lo, hi = math.inf, -math.inf
        for _, poly in self._particles.values():
            arr = poly.GetPointData().GetArray(self.particle_field) if self.particle_field else None
            if arr is not None and arr.GetNumberOfTuples():
                a, b = arr.GetRange(0)
                lo, hi = min(lo, a), max(hi, b)
        if lo > hi:
            return None
        if hi <= lo:
            pad = abs(lo) * 1e-6 or 1e-12
            lo, hi = lo - pad, hi + pad
        return lo, hi

    def set_particles(self, field: str | None = None) -> dict[str, Any]:
        """Color the particles by one of their quantities, or one color ("")."""
        if field is not None:
            if field and field not in self.particle_field_names():
                raise ViewerError(f"particles carry no {field!r}: {', '.join(self.particle_field_names())}")
            self.particle_field = field
        self._apply_particles()
        return self.info()

    def _apply_particles(self) -> None:
        rng = self.particle_range()
        lut = vtkLookupTable()
        lut.SetNumberOfTableValues(256)
        for i in range(256):
            lut.SetTableValue(i, *color_at(self.colormap, i / 255), 1.0)
        if rng:
            lut.SetTableRange(*rng)
        lut.Build()
        for name, (_, poly) in self._particles.items():
            mapper = _mapper(self.blocks[name].actor)
            arr = poly.GetPointData().GetArray(self.particle_field) if self.particle_field else None
            if arr is None or rng is None:
                mapper.ScalarVisibilityOff()
                continue
            mapper.ScalarVisibilityOn()
            mapper.SetLookupTable(lut)
            mapper.UseLookupTableScalarRangeOn()
            mapper.SetScalarModeToUsePointFieldData()
            mapper.SelectColorArray(self.particle_field)

    def render_image(self, fmt: str = "png", quality: int = 90) -> bytes:
        self._lift_particles()
        return super().render_image(fmt, quality)

    def _lift_particles(self) -> None:
        """Each particle drawn in front of its dark halo: toward the camera by more than the halo is wide."""
        if not self._particles:
            return
        cam = self.renderer.GetActiveCamera()
        height = max(self.window.GetSize()[1], 1)
        if cam.GetParallelProjection():
            per_px = 2 * cam.GetParallelScale() / height
        else:
            per_px = 2 * cam.GetDistance() * math.tan(math.radians(cam.GetViewAngle()) / 2) / height
        lift = -2 * HALO_PX * per_px
        offset = [lift * c for c in cam.GetDirectionOfProjection()]
        for name in self._particles:
            self.blocks[name].actor.SetPosition(*offset)

    def _particles_info(self) -> dict[str, Any]:
        snaps = [snap for snap, _ in self._particles.values()]
        rng = self.particle_range()
        return {
            "shown": list(self.particle_ids),
            "field": self.particle_field,
            "fields": self.particle_field_names(),
            "range": list(rng) if rng else None,
            "count": sum(poly.GetNumberOfPoints() for _, poly in self._particles.values()),
            "step": snaps[0].step if snaps else None,
            "t": snaps[0].t if snaps else None,
        }

    def _outline(self) -> None:
        if self.dim != 3 or "domain" in self.blocks or "field" not in self.layers:
            return
        outline = vtkOutlineSource()
        outline.SetBounds(*self.layers["field"].data.GetBounds())
        outline.Update()
        actor = _actor(outline.GetOutput())
        actor.GetMapper().ScalarVisibilityOff()
        actor.GetProperty().SetColor(*THEMES[self.theme]["outline"])
        actor.PickableOff()
        self.blocks["domain"] = Block("domain", "domain", "mesh", THEMES[self.theme]["outline"], None, 3, 0, actor)
        self.renderer.AddActor(actor)

    def _levels(self, data: vtkDataSet) -> list[vtkPolyData]:
        """The surface a dataset shows (its boundary in 3D, itself in 2D and below), and coarser levels of it
        while it is over the budgets. Decimated levels carry the fields as point data."""
        surface = vtkDataSetSurfaceFilter()
        surface.SetInputData(data)
        full = _with_point_data(surface)
        levels = [full]
        for target in (self.budget.triangles, self.budget.interactive_triangles):
            src = levels[-1]
            if src.GetNumberOfCells() <= target or src.GetNumberOfPolys() == 0:
                continue
            levels.append(_decimate(src, target))
        if levels[0].GetNumberOfCells() > self.budget.triangles:
            levels.pop(0)  # never drawn at more than the full-quality budget
        return levels

    def _update_slice(self) -> None:
        """The cut through the volume on the section plane: the slice, and the face a clipped volume shows where
        it is cut (its surface alone is a hollow shell). In 2D the cut is a line, drawn in the accent color: in
        the field's colors it is the field it lies on."""
        if "slice" in self.blocks:
            for a in self.blocks["slice"].actors:
                self.renderer.RemoveActor(a)
            del self.blocks["slice"]
            self.layers.pop("slice", None)
        self._update_mark()
        wanted = self.slice_enabled or (self.clip_enabled and self.dim == 3)
        if not wanted or "field" not in self.layers or "field" in self.lods:
            return  # a cut through a tiled surface's volume is the query process's (mallard session)
        field = self.layers["field"]
        cutter = vtkCutter()
        cutter.SetCutFunction(self.plane)
        cutter.SetInputData(field.data)
        cut = _with_point_data(cutter)
        actor = _actor(cut)
        actor.GetProperty().SetLineWidth(3.0)
        self.blocks["slice"] = Block(
            "slice", "slice", "slice", THEMES[self.theme]["accent"], None, self.dim - 1, cut.GetNumberOfCells(), actor
        )
        if self.dim == 3:
            self.layers["slice"] = Layer(field.series, field.snapshot, cut, [cut])
        else:
            actor.GetMapper().ScalarVisibilityOff()
            actor.GetProperty().LightingOff()
        self.renderer.AddActor(actor)

    def _update_mark(self) -> None:
        """Where the section plane is, drawn in the scene while it clips or slices (and the mark is wanted): a
        block of its own, on the data's box, so it is sent as geometry with the rest."""
        if "section" in self.blocks:
            for a in self.blocks["section"].actors:
                self.renderer.RemoveActor(a)
            del self.blocks["section"]
        if not (self.clip_enabled or self.slice_enabled) or not self.section_mark or "field" not in self.layers:
            return
        actor = _actor(self.mark_poly(self.layers["field"].data.GetBounds()))
        actor.GetMapper().ScalarVisibilityOff()
        actor.GetProperty().SetLineWidth(3.0)
        actor.GetProperty().LightingOff()
        actor.PickableOff()
        self.blocks["section"] = Block("section", "section", "section", THEMES[self.theme]["accent"], None, 1, 0, actor)
        self.renderer.AddActor(actor)

    def _apply_styles(self) -> None:
        super()._apply_styles()
        accent = THEMES[self.theme]["accent"]
        for name in ("section", "slice"):
            if name in self.blocks and name not in self.layers:
                self.blocks[name].actor.GetProperty().SetColor(*accent)
                self.blocks[name].actor.PickableOff()
        for name in self._particles:
            self.blocks[name].actor.PickableOff()  # probes and lines go on the flow, not on a particle
        # A slice lies inside the volume: its surface is drawn faintly around it, as x-ray draws it.
        field = self.blocks.get("field")
        if field and self.dim == 3 and self.slice_enabled and not self.clip_enabled and "slice" in self.blocks:
            field.actor.GetProperty().SetOpacity(min(self.xray_opacity, field.actor.GetProperty().GetOpacity()))
            field.actor.PickableOff()

    # Fields

    def field_names(self) -> list[str]:
        names: list[str] = []
        for layer in self.layers.values():
            for name, _ in _arrays(layer.data):
                if name not in names:
                    names.append(name)
        return names

    def set_field(
        self,
        field: str = "",
        component: str = "",
        colormap: str = "",
        range: Sequence[float] | None = None,
        auto_range: bool = False,
        interpolation: str = "",
    ) -> dict[str, Any]:
        if interpolation:
            if interpolation not in INTERPOLATIONS:
                raise ViewerError(f"interpolation {interpolation!r}: one of {', '.join(INTERPOLATIONS)}")
            self.interpolation = interpolation
        if field:
            self.field = field
        if component:
            if component not in COMPONENTS:
                raise ViewerError(f"component {component!r}: one of {', '.join(COMPONENTS)}")
            self.component = component
        if colormap:
            if colormap not in COLORMAPS and colormap not in LEGACY:
                raise ViewerError(f"colormap {colormap!r}: one of {', '.join(COLORMAPS)}")
            self.colormap = canonical(colormap)
        if auto_range:
            self.range = None
        elif range is not None:
            lo, hi = float(range[0]), float(range[1])
            if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
                raise ViewerError(f"range [{lo}, {hi}]: needs min < max")
            self.range = (lo, hi)
        self._apply_field()
        self._apply_particles()  # the same colormap
        return self.info()

    def data_range(self) -> tuple[float, float] | None:
        """The shown field's range over the blocks drawing it, this snapshot."""
        lo, hi = math.inf, -math.inf
        for name, layer in self.layers.items():
            if name == "slice" and "field" in self.layers:
                continue
            arr = layer.data.GetCellData().GetArray(self.field) or layer.data.GetPointData().GetArray(self.field)
            if arr is None:
                continue
            comp = _component_index(self.component, arr.GetNumberOfComponents())
            a, b = arr.GetRange(comp)
            lo, hi = min(lo, a), max(hi, b)
        return (lo, hi) if lo <= hi else None

    def shown_range(self) -> tuple[float, float] | None:
        if self.range is not None:
            return self.range
        r = self.data_range()
        if r is None:
            return None
        lo, hi = r
        if self.colormap in DIVERGING and lo < 0 < hi:
            lo, hi = symmetric(lo, hi)
        if hi <= lo:
            pad = abs(lo) * 1e-6 or 1e-12
            lo, hi = lo - pad, hi + pad
        return lo, hi

    def _apply_field(self) -> None:
        rng = self.shown_range()
        lut = vtkLookupTable()
        lut.SetNumberOfTableValues(256)
        for i in range(256):
            lut.SetTableValue(i, *color_at(self.colormap, i / 255), 1.0)
        if rng:
            lut.SetTableRange(*rng)
        lut.Build()
        self._lut = lut
        for name, layer in self.layers.items():
            block = self.blocks.get(name)
            if block is None:
                continue
            mapper = _mapper(block.actor)
            level = layer.levels[min(layer.level, len(layer.levels) - 1)]
            mapper.SetInputData(level)
            on_cells = level.GetCellData().GetArray(self.field) is not None and (
                self.interpolation == "cells" or level.GetPointData().GetArray(self.field) is None
            )
            arr = level.GetCellData().GetArray(self.field) if on_cells else level.GetPointData().GetArray(self.field)
            if arr is None or rng is None:
                mapper.ScalarVisibilityOff()
                continue
            n = arr.GetNumberOfComponents()
            if n > 1 and self.component == "magnitude":
                lut.SetVectorModeToMagnitude()
            else:
                lut.SetVectorModeToComponent()
                lut.SetVectorComponent(max(_component_index(self.component, n), 0))
            mapper.ScalarVisibilityOn()
            mapper.SetLookupTable(lut)
            mapper.UseLookupTableScalarRangeOn()
            if on_cells:
                mapper.SetScalarModeToUseCellFieldData()
            else:
                mapper.SetScalarModeToUsePointFieldData()
            mapper.SelectColorArray(self.field)
            mapper.InterpolateScalarsBeforeMappingOn()  # point colors through the map, not between its ends

    def fields(self) -> list[dict[str, Any]]:
        """The fields shown blocks carry, with their units from the snapshots' headers."""
        units: dict[str, dict[str, str]] = {}
        for layer in self.layers.values():
            if layer.series in self.series:
                path = self._file(layer.snapshot)
                if path.is_file():
                    for f in snapshot_header(path)["fields"]:
                        units.setdefault(f["name"], f)
        out = []
        for name in self.field_names():
            comps = 1
            for layer in self.layers.values():
                arr = layer.data.GetCellData().GetArray(name) or layer.data.GetPointData().GetArray(name)
                if arr is not None:
                    comps = arr.GetNumberOfComponents()
                    break
            meta = units.get(name, {})
            out.append(
                {
                    "name": name,
                    "components": comps,
                    "units": meta.get("units", ""),
                    "long_name": meta.get("long_name", ""),
                }
            )
        return out

    # The section tool, with a cut through the volume

    def set_section(
        self,
        clip: bool | None = None,
        slice: bool | None = None,
        origin: Sequence[float] | None = None,
        normal: Sequence[float] | None = None,
        flipped: bool | None = None,
        snap: bool = False,
        mark: bool | None = None,
    ) -> dict[str, Any]:
        if slice is not None:
            self.slice_enabled = bool(slice)
        if mark is not None:
            self.section_mark = bool(mark)
        if self.dim == 2 and normal is not None and not (normal[0] or normal[1]):
            normal = None  # a plane of the page cuts no 2D run: the plane stays as it was
        self.set_clip(self.clip_enabled if clip is None else bool(clip), origin, normal, flipped, snap)
        self._update_slice()
        self._apply_field()
        self._apply_clip()
        self._apply_styles()
        return self.section()

    def section(self) -> dict[str, Any]:
        return {
            "clip": self.clip_enabled,
            "slice": self.slice_enabled,
            "origin": list(self.clip_origin),
            "normal": list(self.clip_normal),
            "flipped": self.keep_flipped,
            "mark": self.section_mark,
            "extent": list(self.section_extent()),
            "offset": self.section_offset(),
            "slice_cells": self.blocks["slice"].cells if self.slice_enabled and "slice" in self.blocks else 0,
        }

    # The camera and frames

    def camera(
        self, op: str, dx: float = 0.0, dy: float = 0.0, factor: float = 1.0, preset: str = "", aspect: float = 0.0
    ) -> None:
        """Camera moves in screen fractions: orbit and pan by (dx, dy) of the view's size, zoom by a factor. A move
        is the user's camera: a frame of another size (the coarse frames while moving are half size) keeps it
        rather than framing the view again. `aspect` is the width / height of the view a preset, Fit or focus
        frames for, where the page draws it (geometry mode) at another shape than the last image."""
        cam = self.renderer.GetActiveCamera()
        if aspect > 0:
            self.aspect = aspect
        if op in ("orbit", "pan", "zoom"):
            self.framed = False
        if op == "orbit":
            cam.Azimuth(-dx * 180.0)
            cam.Elevation(dy * 180.0)
            cam.OrthogonalizeViewUp()
        elif op == "pan":
            fx, fy, fz = cam.GetFocalPoint()
            px, py, pz = cam.GetPosition()
            height = (
                2 * cam.GetParallelScale()
                if cam.GetParallelProjection()
                else 2 * cam.GetDistance() * math.tan(math.radians(cam.GetViewAngle() / 2))
            )
            w, h = self.window.GetSize()
            up = cam.GetViewUp()
            normal = cam.GetDirectionOfProjection()
            right = (
                normal[1] * up[2] - normal[2] * up[1],
                normal[2] * up[0] - normal[0] * up[2],
                normal[0] * up[1] - normal[1] * up[0],
            )
            sx, sy = -dx * height * (w / max(h, 1)), dy * height
            move = [sx * r + sy * u for r, u in zip(right, up, strict=True)]
            cam.SetFocalPoint(fx + move[0], fy + move[1], fz + move[2])
            cam.SetPosition(px + move[0], py + move[1], pz + move[2])
        elif op == "zoom":
            if cam.GetParallelProjection():
                cam.SetParallelScale(cam.GetParallelScale() / factor)
            else:
                cam.Dolly(factor)
        elif op == "preset":
            self.view_preset(preset)
        elif op == "fit":
            self.fit()
        elif op == "lock2d":
            self.set_lock_2d(bool(factor))
        elif op == "focus":
            shown = [b.actor.GetBounds() for n, b in self.blocks.items() if n.startswith("zone:") and b.visible]
            if not shown:
                raise ViewerError("no zone surface shown to focus on")
            lo = [min(b[2 * i] for b in shown) for i in range(3)]
            hi = [max(b[2 * i + 1] for b in shown) for i in range(3)]
            self.frame_bounds([v for pair in zip(lo, hi, strict=True) for v in pair])
        elif op == "parallel":
            self.set_parallel(bool(factor))
        else:
            raise ViewerError(f"camera op {op!r}")
        self.renderer.ResetCameraClippingRange()

    def frame(self, width: int, height: int, interactive: bool = False) -> dict[str, Any]:
        """An image of the view: while the camera moves, the coarse level at half resolution as JPEG;
        otherwise full quality."""
        width, height = max(int(width), 16), max(int(height), 16)
        scale = math.sqrt(self.budget.frame_pixels / (width * height))
        if scale < 1:
            width, height = int(width * scale), int(height * scale)
        if interactive:
            width, height = max(width // 2, 16), max(height // 2, 16)
        self._set_level(interactive)
        self.aspect = 0.0  # this image's own
        self.window.SetSize(width, height)
        if not interactive:
            self._refine(height, width / max(height, 1))
        start = time.perf_counter()
        image = self.render_image("jpeg", 55 if interactive else 92)
        return {
            "image": image,
            "mime": "image/jpeg",
            "width": width,
            "height": height,
            "ms": round(1000 * (time.perf_counter() - start), 1),
            "camera": self.get_camera(),
            "triangles": sum(
                lay.levels[min(lay.level, len(lay.levels) - 1)].GetNumberOfCells() for lay in self.layers.values()
            ),
        }

    def frame_field(
        self, field: str, component: str = "magnitude", width: int = 800, height: int = 600, interactive: bool = False
    ) -> dict[str, Any]:
        """The view as it is, colored by another field over its own range in the same colormap: the split view's
        second picture. The field shown stays the one set."""
        if field not in self.field_names():
            raise ViewerError(f"field {field!r}: one of {', '.join(self.field_names())}")
        if component not in COMPONENTS:
            raise ViewerError(f"component {component!r}: one of {', '.join(COMPONENTS)}")
        keep = (self.field, self.component, self.range)
        self.field, self.component, self.range = field, component, None
        try:
            self._apply_field()
            shown = self.shown_range()
            shot = self.frame(width, height, interactive)
        finally:
            self.field, self.component, self.range = keep
            self._apply_field()
        return {**shot, "field": field, "component": component, "range": list(shown) if shown else None}

    def screenshot(self, width: int, height: int) -> dict[str, Any]:
        """The view as a PNG of exactly `width` x `height` pixels, at full quality, for a file: the camera stays,
        and so does the size the next frame is drawn at."""
        width, height = max(int(width), 16), max(int(height), 16)
        size, framed = self.window.GetSize(), self.framed
        self.framed = False  # another size here is not the view resized
        try:
            self._set_level(False)
            self.window.SetSize(width, height)
            self._refine(height, width / max(height, 1))
            image = self.render_image("png")
        finally:
            self.window.SetSize(*size)
            self.framed = framed
        return {"image": image, "mime": "image/png", "width": width, "height": height}

    # Surface levels of detail

    def lod_dir(self, sid: str, index: int) -> Path:
        """Where a snapshot's levels of detail live: `lod/<series>/<step>/` in the run directory."""
        assert self.run_dir is not None
        snap = self._series(sid).snapshots[index]
        return self.run_dir / "lod" / sid / f"{snap.step if snap.step is not None else index:09d}"

    def _lod(self, sid: str, index: int) -> LodSurface | None:
        root = self.lod_dir(sid, index)
        if not (root / "index.json").is_file():
            return None
        try:
            return LodSurface(root)
        except (ValueError, OSError, KeyError):
            return None

    def _refine(self, height: int, aspect: float) -> None:
        if not self.lods:
            return
        share = self.budget.triangles // len(self.lods)
        changed = False
        for name, lod in self.lods.items():
            if lod.refine(self.renderer.GetActiveCamera(), height, share, aspect):
                poly = lod.polydata()
                layer = self.layers[name]
                layer.data, layer.levels, layer.level = poly, [poly], 0
                changed = True
        if changed:
            self._apply_field()

    def build_lod(self, series: str, index: int, targets: Sequence[int] = lod_mod.DEFAULT_TARGETS) -> dict[str, Any]:
        """Make a snapshot's surface levels of detail next to the data, reading its pieces one at a time. The
        snapshot's surface is never held whole: each piece is clustered and merged into every level."""
        s = self._series(series)
        snap = s.snapshots[int(index)]
        path = self._file(snap)
        pieces = _piece_files(path) if s.format == "pvtu" else [path]
        bounds = [math.inf, -math.inf] * 3
        area = 0.0
        fields: list[tuple[str, int]] = []
        for p in pieces:  # first pass: bounds, area and fields, piece by piece
            poly = lod_mod.triangulated(_read_dataset(p, s.format if p == path else "vtu", snap.index))
            if poly.GetNumberOfPolys() == 0:
                continue
            b = poly.GetBounds()
            bounds = [min(bounds[i], b[i]) if i % 2 == 0 else max(bounds[i], b[i]) for i in range(6)]
            area += lod_mod.surface_area(poly)
            if not fields:
                pd = poly.GetPointData()
                fields = [
                    (pd.GetArrayName(i), pd.GetArray(i).GetNumberOfComponents())
                    for i in range(pd.GetNumberOfArrays())
                    if pd.GetArrayName(i) and not pd.GetArrayName(i).startswith("vtk")
                ]
        if not math.isfinite(bounds[0]):
            raise ViewerError(f"{snap.path}: no surface to make levels of")

        def stream() -> Any:
            for p in pieces:
                yield lod_mod.triangulated(_read_dataset(p, s.format if p == path else "vtu", snap.index))

        units = {f["name"]: f["units"] for f in snapshot_header(path)["fields"]} if s.format != "vtkhdf" else {}
        out = self.lod_dir(series, int(index))
        index_doc = lod_mod.build(
            stream(),
            out,
            fields,
            bounds,
            area,
            targets,
            source={"series": series, "path": snap.path, "step": snap.step, "t": snap.t},
            units=units,
        )
        return {
            "path": str(out.relative_to(self.run_dir or out)),
            "levels": [lv["triangles"] for lv in index_doc["levels"]],
        }

    def _set_level(self, interactive: bool) -> None:
        changed = False
        for layer in self.layers.values():
            want = 0
            if interactive:
                want = next(
                    (
                        i
                        for i, lv in enumerate(layer.levels)
                        if lv.GetNumberOfCells() <= self.budget.interactive_triangles
                    ),
                    len(layer.levels) - 1,
                )
            if layer.level != want:
                layer.level, changed = want, True
        if changed:
            self._apply_field()

    def pick_point(self, x: float, y: float, css_width: float, css_height: float) -> dict[str, Any] | None:
        """The surface point under a position in the shown image's CSS pixels (y down)."""
        w, h = self.window.GetSize()
        px, py = x * w / max(css_width, 1), (css_height - y) * h / max(css_height, 1)
        hit = self.pick(px, py)
        if hit is None or hit.get("point") is None:
            return None
        point = list(hit["point"])
        if self.dim == 2:
            point[2] = 0.0
        return {"point": point, "block": hit["name"]}

    # Geometry mode, saved views and extracts

    def geometry(self, max_triangles: int = 0) -> dict[str, Any]:
        """The visible surfaces as triangles with their colors mapped, clipped by the section plane, within
        the geometry budget (decimated further to fit), for the browser to render itself."""
        budget = min(max_triangles or self.budget.geometry_triangles, self.budget.geometry_triangles)
        shown = []
        for name, block in self.blocks.items():
            if not block.visible or not block.actor.GetVisibility():
                continue  # as the image: the domain's box is not drawn around zone surfaces
            layer = self.layers.get(name)
            poly = (
                layer.levels[-1 if self._coarse_only(layer, budget) else 0]
                if layer
                else _mapper(block.actor).GetInput()
            )
            if not isinstance(poly, vtkPolyData):
                continue
            if self.clip_enabled and block.kind not in self.UNCLIPPED_KINDS and block.kind != "mesh":
                poly = geom.clipped(poly, self.plane)
            shown.append((name, block, geom.triangles(poly)))
        total = sum(p.GetNumberOfPolys() for _, _, p in shown)
        blocks = []
        for name, block, poly in shown:
            if total > budget and poly.GetNumberOfPolys() > 0:
                poly = _decimate(poly, max(int(budget * poly.GetNumberOfPolys() / total), 4))
            colored = _mapper(block.actor).GetScalarVisibility() and self._lut is not None
            if colored and self.interpolation == "cells" and poly.GetCellData().GetArray(self.field) is not None:
                poly = _exploded(poly, self.field)  # the browser colors points: a cell flat needs its own
            rgb = self._colors(poly) if colored else None
            opacity = block.actor.GetProperty().GetOpacity()
            blocks.append(geom.encode_block(name, poly, rgb, block.actor.GetProperty().GetColor(), opacity))
        sent = sum(b["n_triangles"] for b in blocks)
        if sent > budget * 1.05:
            raise BudgetError(f"{sent} triangles is over the geometry budget of {budget}: image mode")
        return {
            "schema": geom.SCHEMA,
            "version": geom.VERSION,
            "blocks": blocks,
            "triangles": sent,
            "camera": self.get_camera(),
            "background": list(THEMES[self.theme]["background"]),
        }

    def _coarse_only(self, layer: Layer, budget: int) -> bool:
        return layer.levels[0].GetNumberOfCells() > budget and len(layer.levels) > 1

    def _colors(self, poly: vtkPolyData) -> Any:
        """Per-point RGB of the shown field on `poly` through the current lookup table."""
        import numpy as np
        from vtkmodules.util.numpy_support import vtk_to_numpy

        assert self._lut is not None
        arr = poly.GetPointData().GetArray(self.field)
        if arr is None and poly.GetCellData().GetArray(self.field) is not None:
            to_points = vtkCellDataToPointData()
            to_points.SetInputData(poly)
            to_points.Update()
            arr = to_points.GetOutput().GetPointData().GetArray(self.field)
        if arr is None:
            return None
        n = arr.GetNumberOfComponents()
        comp = _component_index(self.component, n)
        mapped = self._lut.MapScalars(arr, 0, comp, 3)  # VTK_COLOR_MODE_DEFAULT, VTK_RGB
        out = vtk_to_numpy(mapped).reshape(-1, 3)
        return np.ascontiguousarray(out, dtype=np.uint8)

    def get_camera(self) -> dict[str, Any]:
        cam = self.renderer.GetActiveCamera()
        return {
            "position": list(cam.GetPosition()),
            "focal_point": list(cam.GetFocalPoint()),
            "view_up": list(cam.GetViewUp()),
            "view_angle": cam.GetViewAngle(),
            "parallel": bool(cam.GetParallelProjection()),
            "parallel_scale": cam.GetParallelScale(),
        }

    def set_camera(
        self,
        position: Sequence[float],
        focal_point: Sequence[float],
        view_up: Sequence[float],
        view_angle: float = 30.0,
        parallel: bool = False,
        parallel_scale: float = 1.0,
    ) -> dict[str, Any]:
        cam = self.renderer.GetActiveCamera()
        cam.SetPosition(*[float(c) for c in position][:3])
        cam.SetFocalPoint(*[float(c) for c in focal_point][:3])
        cam.SetViewUp(*[float(c) for c in view_up][:3])
        cam.SetViewAngle(float(view_angle))
        cam.SetParallelProjection(1 if parallel else 0)
        cam.SetParallelScale(float(parallel_scale))
        cam.OrthogonalizeViewUp()
        self.renderer.ResetCameraClippingRange()
        self._camera_set = True
        return self.get_camera()

    def export_surface(self, name: str, triangles: int = 200_000) -> dict[str, Any]:
        """The visible surfaces with the fields as point data, decimated to about `triangles`, written next to
        the data as `extract/NAME.vtp`: a small object to view or copy instead of the snapshot."""
        from vtkmodules.vtkIOXML import vtkXMLPolyDataWriter

        if self.run_dir is None:
            raise ViewerError("no run open")
        if not name or "/" in name or name.startswith("."):
            raise ViewerError(f"extract name {name!r}: a plain file name")
        append = vtkAppendPolyData()
        for n, layer in self.layers.items():
            if self.blocks.get(n) is not None and self.blocks[n].visible:
                poly = layer.levels[0]
                to_points = vtkCellDataToPointData()
                to_points.SetInputData(poly)
                to_points.Update()
                append.AddInputData(geom.triangles(to_points.GetOutput()))
        if append.GetNumberOfInputConnections(0) == 0:
            raise ViewerError("nothing shown to extract")
        append.Update()
        poly = append.GetOutput()
        if poly.GetNumberOfPolys() > triangles:
            poly = _decimate(poly, triangles)
        out = self.run_dir / "extract" / f"{name}.vtp"
        out.parent.mkdir(exist_ok=True)
        partial = out.with_name(out.name + ".partial")
        writer = vtkXMLPolyDataWriter()
        writer.SetFileName(str(partial))
        writer.SetInputData(poly)
        writer.SetDataModeToBinary()
        writer.SetCompressorTypeToZLib()
        if not writer.Write():
            raise ViewerError(f"{out}: not written")
        partial.replace(out)
        import hashlib

        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        return {
            "path": out.relative_to(self.run_dir).as_posix(),
            "bytes": out.stat().st_size,
            "sha256": digest,
            "triangles": poly.GetNumberOfPolys(),
        }

    def overlay(self, path: str, name: str = "cut") -> dict[str, Any]:
        """Draw a surface file of the run (a cut the session wrote next to the data) as the slice, colored by
        the shown field, in place of the viewer's own cut."""
        if self.run_dir is None:
            raise ViewerError("no run open")
        file = (self.run_dir / path).resolve()
        if self.run_dir not in file.parents:
            raise ViewerError(f"{path}: outside the run directory")
        data = _read_dataset(file, VIEWABLE_EXT.get(file.suffix, "vtu"), 0)
        poly = geom.triangles(lod_mod.triangulated(data)) if not isinstance(data, vtkPolyData) else data
        if "slice" in self.blocks:
            for a in self.blocks["slice"].actors:
                self.renderer.RemoveActor(a)
        actor = _actor(poly)
        self.blocks["slice"] = Block(
            "slice", name, "slice", THEMES[self.theme]["accent"], None, 2, poly.GetNumberOfCells(), actor
        )
        primary = self.layers.get("field")
        self.layers["slice"] = Layer(
            primary.series if primary else "", primary.snapshot if primary else Snapshot(path), poly, [poly]
        )
        self.renderer.AddActor(actor)
        self._apply_field()
        self._apply_clip()
        self._apply_styles()
        return {"cells": poly.GetNumberOfCells()}

    def render_info(self) -> dict[str, Any]:
        """How this viewer renders: the window class (EGL on a GPU, OSMesa on CPUs) and OpenGL's strings."""
        self.window.Render()
        report = self.window.ReportCapabilities() or ""
        fields = {}
        for line in report.splitlines():
            key, _, value = line.partition(":")
            if key.strip() in ("OpenGL vendor string", "OpenGL renderer string", "OpenGL version string"):
                fields[key.strip().split()[1]] = value.strip()
        return {
            "window": self.window.GetClassName(),
            "vendor": fields.get("vendor", ""),
            "renderer": fields.get("renderer", ""),
            "version": fields.get("version", ""),
            "gpu": _is_gpu(self.window.GetClassName(), fields.get("renderer", "")),
        }

    # Probes and lines over time

    def set_markers(self, points: Sequence[Any] = (), lines: Sequence[Any] = ()) -> dict[str, Any]:
        """Where the probes and lines being plotted are: labeled points `[label, [x, y, z]]` and segments
        `[[x, y, z], [x, y, z]]`, drawn over the data and clipped with it."""
        from ..view.overlay import marker_actors

        def vec(p: Sequence[Any]) -> tuple[float, float, float]:
            xs = [float(c) for c in p][:3] + [0.0] * (3 - len(p))
            return (xs[0], xs[1], xs[2])

        for t in self._text_markers:
            self.renderer.RemoveActor(t)
        b = self.bounds
        size = math.dist(b[0::2], b[1::2]) or 1.0
        actors = marker_actors(
            [(str(label), vec(p)) for label, p in points],
            THEMES[self.theme]["accent"],
            size,
            [(vec(a), vec(c)) for a, c in lines],
        )
        self.set_overlay("markers", [a for a in actors if isinstance(a, vtkActor)])
        self._text_markers = [a for a in actors if not isinstance(a, vtkActor)]
        for t in self._text_markers:
            self.renderer.AddActor(t)
        return {"markers": len(points), "lines": len(lines)}

    def probe(
        self, series: str, point: Sequence[float], indices: Sequence[int], fields: Sequence[str] = ()
    ) -> dict[str, Any]:
        """Values at a point at each of the snapshots `indices`: a time series, read next to the data."""
        if len(indices) > self.budget.probe_snapshots:
            raise BudgetError(f"{len(indices)} snapshots in one request; at most {self.budget.probe_snapshots}")
        s = self._series(series)
        pts = vtkPoints()
        pts.InsertNextPoint(*[float(c) for c in point][:3], *([0.0] * (3 - len(point))))
        out: dict[str, Any] = {"t": [], "step": [], "values": {}}
        for i in indices:
            data = self.read(series, int(i))
            probed = _probe(data, pts)
            snap = s.snapshots[int(i)]
            out["t"].append(snap.t)
            out["step"].append(snap.step)
            valid = probed.GetPointData().GetArray("vtkValidPointMask")
            ok = valid is None or valid.GetTuple1(0) > 0
            for name, comps in _arrays(data):
                if fields and name not in fields:
                    continue
                arr = probed.GetPointData().GetArray(name)
                value = _value(arr, 0, self.component if name == self.field else "magnitude", comps) if ok else None
                out["values"].setdefault(name, []).append(value)
        return out

    def line(
        self,
        series: str,
        p0: Sequence[float],
        p1: Sequence[float],
        indices: Sequence[int],
        field: str,
        resolution: int = 200,
    ) -> dict[str, Any]:
        """A field along the segment p0-p1 at each of the snapshots `indices`: profiles over time."""
        if len(indices) > self.budget.probe_snapshots:
            raise BudgetError(f"{len(indices)} snapshots in one request; at most {self.budget.probe_snapshots}")
        s = self._series(series)
        line = vtkLineSource()
        a = [float(c) for c in p0][:3] + [0.0] * (3 - len(p0))
        b = [float(c) for c in p1][:3] + [0.0] * (3 - len(p1))
        line.SetPoint1(*a)
        line.SetPoint2(*b)
        line.SetResolution(max(2, min(int(resolution), 4000)))
        line.Update()
        pts = line.GetOutput().GetPoints()
        length = math.dist(a, b)
        n = pts.GetNumberOfPoints()
        out: dict[str, Any] = {"s": [length * k / (n - 1) for k in range(n)], "curves": []}
        for i in indices:
            data = self.read(series, int(i))
            probed = _probe(data, pts)
            arr = probed.GetPointData().GetArray(field)
            valid = probed.GetPointData().GetArray("vtkValidPointMask")
            comps = arr.GetNumberOfComponents() if arr is not None else 1
            values = [
                _value(arr, k, self.component if field == self.field else "magnitude", comps)
                if arr is not None and (valid is None or valid.GetTuple1(k) > 0)
                else None
                for k in range(n)
            ]
            snap = s.snapshots[int(i)]
            out["curves"].append({"t": snap.t, "step": snap.step, "values": values})
        return out

    def stats(self, bins: int = 32) -> dict[str, Any]:
        """The shown field's statistics this snapshot (stats.py): over the domain and, while a slice is on in 3D,
        over the slice; the histogram over the shown range."""
        out: dict[str, Any] = {"field": self.field, "component": self.component}
        shown = self.shown_range()
        for where, key in (("field", "domain"), ("slice", "slice")):
            layer = self.layers.get(where)
            got = (
                _cell_stats(layer.data, self.field, self.component, shown, bins, self._geometry(layer.data))
                if layer
                else None
            )
            if got is not None:
                out[key] = got
        return out

    def _geometry(self, data: vtkDataSet) -> tuple[Any, Any]:
        """A dataset's cell measures and centers, kept for the last few: another field's statistics are at once."""
        key = id(data)
        if key in self._geometries and self._geometries[key][0] is data:
            self._geometries.move_to_end(key)
            return self._geometries[key][1]
        self._geometries[key] = (data, _cell_geometry(data))
        while len(self._geometries) > 4:
            self._geometries.popitem(last=False)
        return self._geometries[key][1]

    # State for the UI

    def info(self) -> dict[str, Any]:
        s = self.series.get(self.primary)
        snap = s.snapshots[self.index] if s and s.snapshots else None
        rng = self.shown_range()
        data_rng = self.data_range()
        return {
            "series": self.primary,
            "index": self.index,
            "count": len(s.snapshots) if s else 0,
            "step": snap.step if snap else None,
            "t": snap.t if snap else None,
            "zones": list(self.zone_ids),
            "field": self.field,
            "component": self.component,
            "colormap": self.colormap,
            "interpolation": self.interpolation,
            "range": list(rng) if rng else None,
            "data_range": list(data_rng) if data_rng else None,
            "auto_range": self.range is None,
            "fields": self.fields(),
            "blocks": [b.info() for b in self.blocks.values() if b.kind != "mesh"],
            "cells": sum(lay.data.GetNumberOfCells() for n, lay in self.layers.items() if n != "slice"),
            "triangles": sum(lay.levels[0].GetNumberOfCells() for lay in self.layers.values()),
            "levels": {n: [lv.GetNumberOfCells() for lv in lay.levels] for n, lay in self.layers.items()},
            "cached_bytes": self._cached_bytes,
            "budget": asdict(self.budget),
            "notice": self.notice,
            "over_budget": self.over_budget,
            "lod": {
                n: {"levels": lod.levels_shown(), "triangles": lod.triangles, "loads": lod.loads}
                for n, lod in self.lods.items()
            },
            "section": self.section(),
            "dim": self.dim,
            "particles": self._particles_info(),
        }


# Helpers


def _piece_files(pvtu: Path) -> list[Path]:
    """The piece files a `.pvtu` names, in order: read one at a time."""
    import xml.etree.ElementTree as ET

    root = ET.parse(pvtu).getroot()
    return [pvtu.parent / str(p.get("Source")) for p in root.iter("Piece") if p.get("Source")]


def _is_gpu(window: str, renderer: str) -> bool:
    software = ("llvmpipe", "softpipe", "swrast", "mesa offscreen", "apple software")
    return not any(w in renderer.lower() for w in software) and window != "vtkOSOpenGLRenderWindow"


def _TIME_STEPS() -> Any:
    from vtkmodules.vtkCommonExecutionModel import vtkStreamingDemandDrivenPipeline

    return vtkStreamingDemandDrivenPipeline.TIME_STEPS()


def _run_dim(run_dir: Path) -> int | None:
    import json

    log = run_dir / "mallard.jsonl"
    try:
        with log.open("rb") as f:
            for line in f:
                rec = json.loads(line)
                if rec.get("type") == "start":
                    dim = ((rec.get("provenance") or {}).get("build") or {}).get("dim")
                    return int(dim) if dim else None
    except (OSError, ValueError):
        return None
    return None


def _read_dataset(path: Path, fmt: str, index: int) -> vtkDataSet:
    if fmt == "vtkhdf":
        reader = vtkHDFReader()
        reader.SetFileName(str(path))
        reader.UpdateInformation()
        reader.SetStep(index)
        reader.Update()
        out = reader.GetOutputDataObject(0)
    else:
        xml = vtkXMLGenericDataObjectReader()
        xml.SetFileName(str(path))
        xml.Update()
        out = xml.GetOutput()
    if not isinstance(out, vtkDataSet):
        raise ViewerError(f"{path.name}: not a dataset VTK reads ({type(out).__name__})")
    return out


PARTICLE_PX = 8.0
HALO_PX = 1.5
_PARTICLE_STATE = ("V_X", "V_Y", "V_Z", "speed", "T_P")  # what a solid particle is, before its quantities


def _read_particles(path: Path) -> vtkPolyData:
    """The particles' CSV file as vertices, each column after the position a point array, and the speed
    of solid particles."""
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray

    with path.open(encoding="utf-8") as f:
        rows = [line for line in f if line.strip() and not line.startswith("#")]
    if not rows:
        raise ViewerError(f"{path.name}: no particle columns")
    header = particle_columns(rows[0])
    values = np.loadtxt(rows[1:], delimiter=",", ndmin=2) if len(rows) > 1 else np.zeros((0, len(header)))
    if values.shape[1] != len(header) or not {"x", "y"} <= set(header):
        raise ViewerError(f"{path.name}: not a particle file (id, x, y[, z], ...)")
    n = values.shape[0]
    xyz = np.zeros((n, 3))
    for d, c in enumerate("xyz"):
        if c in header:
            xyz[:, d] = values[:, header.index(c)]
    poly = vtkPolyData()
    points = vtkPoints()
    points.SetData(numpy_to_vtk(xyz, deep=True))
    poly.SetPoints(points)
    verts = vtkCellArray()
    ids = numpy_to_vtkIdTypeArray(np.arange(n + 1), deep=True)
    verts.SetData(ids, numpy_to_vtkIdTypeArray(np.arange(n), deep=True))
    poly.SetVerts(verts)
    columns = {c: values[:, i] for i, c in enumerate(header) if c not in ("x", "y", "z")}
    if "V_X" in columns:
        columns["speed"] = np.sqrt(sum(columns[k] ** 2 for k in ("V_X", "V_Y", "V_Z") if k in columns))
    for name, col in columns.items():
        arr = numpy_to_vtk(np.ascontiguousarray(col), deep=True)
        arr.SetName(name)
        poly.GetPointData().AddArray(arr)
    return poly


def _actor(poly: vtkPolyData) -> vtkActor:
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(poly)
    actor = vtkActor()
    actor.SetMapper(mapper)
    return actor


def _mapper(actor: vtkActor) -> vtkPolyDataMapper:
    mapper = actor.GetMapper()
    assert isinstance(mapper, vtkPolyDataMapper)
    return mapper


def _dimension(data: vtkDataSet) -> int:
    n = data.GetNumberOfCells()
    if n == 0:
        return 0
    return max(data.GetCell(i).GetCellDimension() for i in range(0, n, max(1, n // 64)))


def _exploded(poly: vtkPolyData, field: str) -> vtkPolyData:
    """Triangles with points of their own, a cell field as their points' values: flat cells, drawn per point."""
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray

    if poly.GetNumberOfLines() or not poly.GetNumberOfPolys():
        return poly
    pts = vtk_to_numpy(poly.GetPoints().GetData())
    conn = vtk_to_numpy(poly.GetPolys().GetConnectivityArray()).reshape(-1, 3)
    values = vtk_to_numpy(poly.GetCellData().GetArray(field))
    out = vtkPolyData()
    points = vtkPoints()
    points.SetData(numpy_to_vtk(np.ascontiguousarray(pts[conn.ravel()]), deep=True))
    out.SetPoints(points)
    cells = vtkCellArray()
    n = conn.size
    cells.SetData(
        numpy_to_vtkIdTypeArray(np.arange(0, n + 1, 3), deep=True), numpy_to_vtkIdTypeArray(np.arange(n), deep=True)
    )
    out.SetPolys(cells)
    arr = numpy_to_vtk(np.ascontiguousarray(np.repeat(values, 3, axis=0)), deep=True)
    arr.SetName(field)
    out.GetPointData().AddArray(arr)
    return out


def _with_point_data(source: Any) -> vtkPolyData:
    """A filter's output with its cell fields also as point data: the interpolated mode's colors."""
    to_points = vtkCellDataToPointData()
    to_points.SetInputConnection(source.GetOutputPort())
    to_points.PassCellDataOn()
    to_points.Update()
    out = vtkPolyData()
    out.ShallowCopy(to_points.GetOutput())
    return out


def _decimate(poly: vtkPolyData, target: int) -> vtkPolyData:
    """A coarser surface of about `target` triangles, its fields as cell data (merged triangles' means) and as
    point data (interpolated)."""
    to_points = vtkCellDataToPointData()
    to_points.SetInputData(poly)
    to_points.PassCellDataOn()
    tri = vtkTriangleFilter()
    tri.SetInputConnection(to_points.GetOutputPort())
    tri.PassLinesOff()
    tri.PassVertsOff()
    tri.Update()
    return lod_mod.clustered(tri.GetOutput(), target)


def _arrays(data: vtkDataSet) -> list[tuple[str, int]]:
    out = []
    for attrs in (data.GetCellData(), data.GetPointData()):
        for i in range(attrs.GetNumberOfArrays()):
            arr = attrs.GetArray(i)
            if arr is None or not arr.GetName() or arr.GetName().startswith("vtk"):
                continue
            out.append((arr.GetName(), arr.GetNumberOfComponents()))
    return out


def _default_field(names: Sequence[str], preferred: Sequence[str]) -> str:
    for want in (*preferred, "P", "RHO"):
        if want in names:
            return want
    return names[0] if names else ""


def _component_index(component: str, n: int) -> int:
    if n == 1:
        return 0
    if component == "magnitude":
        return -1
    i = COMPONENTS.index(component) - 1
    return i if i < n else -1


def _cell_geometry(data: vtkDataSet) -> tuple[Any, Any]:
    """Each cell's measure (volume, area or length) and center."""
    sizes = vtkCellSizeFilter()
    sizes.SetInputData(data)
    sizes.Update()
    kind = {3: "Volume", 2: "Area", 1: "Length"}.get(_dimension(data), "Vertex")
    measure = vtk_to_numpy(sizes.GetOutput().GetCellData().GetArray(kind)).astype(float)
    centers = vtkCellCenters()
    centers.SetInputData(data)
    centers.Update()
    return measure, vtk_to_numpy(centers.GetOutput().GetPoints().GetData()).astype(float)


def _cell_stats(
    data: vtkDataSet,
    field: str,
    component: str,
    shown: Sequence[float] | None,
    bins: int,
    geometry: tuple[Any, Any] | None = None,
) -> dict[str, Any] | None:
    """A field's statistics over a dataset's cells (point data averaged onto them), each weighted by its measure."""
    measure, centers = geometry or _cell_geometry(data)
    if data.GetCellData().GetArray(field) is None:
        if data.GetPointData().GetArray(field) is None:
            return None
        p2c = vtkPointDataToCellData()
        p2c.SetInputData(data)
        p2c.Update()
        data = p2c.GetOutput()
    values = vtk_to_numpy(data.GetCellData().GetArray(field)).astype(float)
    if values.ndim > 1:
        c = _component_index(component, values.shape[1])
        values = np.linalg.norm(values, axis=1) if c < 0 else values[:, c]
    return field_stats(values, measure, centers, shown, bins)


def _value(arr: Any, k: int, component: str, comps: int) -> float | None:
    if arr is None:
        return None
    t = arr.GetTuple(k)
    c = _component_index(component, comps)
    v = math.sqrt(sum(x * x for x in t)) if c < 0 else t[c]
    return float(v) if math.isfinite(v) else None


def _probe(data: vtkDataSet, points: vtkPoints) -> vtkPolyData:
    probe_input = vtkPolyData()
    probe_input.SetPoints(points)
    verts = vtkCellArray()
    for i in range(points.GetNumberOfPoints()):
        verts.InsertNextCell(1)
        verts.InsertCellPoint(i)
    probe_input.SetVerts(verts)
    probe = vtkProbeFilter()
    probe.SetInputData(probe_input)
    probe.SetSourceData(data)
    probe.SetCellLocator(vtkStaticCellLocator())
    probe.Update()
    out: vtkPolyData = probe.GetOutput()
    return out
