"""Case setup's editing beyond single values, without the UI: what the case view previews (regions, probes,
inflow vectors, a boundary's coordinate points), dragging handles, picking points into keys, and the actions of
the Regions, Probes and array sections. The trame app binds it to the view and the property panel; the tests
drive it directly."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..input_doc import format_path, parse_path
from ..mallard import coordinate_points
from . import regions as rg
from .geometry import Handle, Preview, Vec, apply_handle, preview
from .inflow import Faces, Inflow, Values, boundary_values, inflow

if TYPE_CHECKING:
    from ..session import Session

# The points of a boundary entry that say where its local coordinates start (Mallard's boundary guide).
COORDINATE_KEYS = ("origin", "toward")


@dataclass
class Shape:
    """A previewed region or probe: its tree node, kind, key path and value."""

    node: str
    kind: str  # region | probe
    path: str
    value: Any


EMPTY = Faces([], [], [])


def frame_markers(frame: Mapping[str, Any], dim: int) -> list[tuple[str, Vec]]:
    """Where a boundary's local coordinates start (`origin`) and a point along `u_direction` from it, a fifth of
    the way along; none for an entry whose faces have no u."""
    origin, direction = frame.get("origin"), frame.get("u_direction")
    if frame.get("shape") is None or not isinstance(origin, list) or len(origin) < 3:
        return []
    o: Vec = (float(origin[0]), float(origin[1]), float(origin[2]))
    out = [("u = 0" if dim == 2 else "(u, v) = (0, 0)", o)]
    length = float(frame.get("length") or 0.0)
    if isinstance(direction, list) and len(direction) >= 3 and length > 0:
        step = 0.2 * length
        out.append(("u grows", (o[0] + step * direction[0], o[1] + step * direction[1], o[2] + step * direction[2])))
    return out


class SetupEditor:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._working: dict[str, Any] = {}  # the table of a shape whose handle is being dragged
        self._values: tuple[Any, dict[str, Values]] = (None, {})  # boundary values read, for the result they are of

    @property
    def doc(self) -> Any:
        return self.session.doc

    @property
    def dim(self) -> int:
        s = self.session.structure
        return int(s.dim) if s and s.dim else 2

    # What is drawn

    def shapes(self) -> list[Shape]:
        out = [Shape(f"region:{e.path}", "region", e.path, e.value) for e in rg.regions(self.doc)]
        out += [Shape(f"el:{e.path}", "probe", e.path, e.value) for e in rg.probes(self.doc)]
        return out

    def shape_of(self, node: str) -> Shape | None:
        """The shape a tree node shows: itself, or a feature's region (`el:write_data[2]` -> its `region`)."""
        shapes = {s.node: s for s in self.shapes()}
        if node in shapes:
            return shapes[node]
        kind, _, ident = node.partition(":")
        if kind == "el":
            region = self.doc.get(f"{ident}.region")
            if isinstance(region, str):
                return shapes.get(f"region:regions.{region}")
            return shapes.get(f"region:{ident}.region")
        return None

    def previews(self, bounds: Sequence[float]) -> list[tuple[str, str, Preview]]:
        named = self.doc.data.get("regions") or {}
        root = self.session.input_path.parent
        out = []
        for s in self.shapes():
            value = self._working.get(s.node, s.value)
            out.append((s.node, s.kind, preview(value, self.dim, named, bounds, root)))
        return out

    # Handles

    def drag(self, node: str, handle: Handle, position: Vec, bounds: Sequence[float]) -> Preview | None:
        """The shape's preview with the handle at `position`, the input untouched until release."""
        shape = self.shape_of(node)
        if shape is None or not isinstance(shape.value, Mapping):
            return None
        table = copy.deepcopy(dict(self._working.get(shape.node, shape.value)))
        table.update(apply_handle(table, handle, position, self.dim))
        self._working[shape.node] = table
        named = self.doc.data.get("regions") or {}
        return preview(table, self.dim, named, bounds, self.session.input_path.parent)

    def block_of(self, node: str) -> str | None:
        """The case-view block that draws a shape's cells as Mallard found them: a named region's name, else
        the key path as Mallard names blocks (`write_data[prefix = "./solut/near_wake"].region`)."""
        shape = self.shape_of(node)
        if shape is None or shape.kind != "region":
            return None
        if shape.path.startswith("regions."):
            return shape.path.removeprefix("regions.")
        segments = [s for s in parse_path(shape.path) if isinstance(s, (str, int))]
        return rg.selector_path(self.doc.data, segments)

    def working(self, node: str) -> Mapping[str, Any]:
        """The table of a shape as its handle being dragged has it, or as the input has it."""
        shape = self.shape_of(node)
        if shape is None:
            return {}
        value = self._working.get(shape.node, shape.value)
        return value if isinstance(value, Mapping) else {}

    def release(self, node: str, handle: Handle, position: Vec) -> list[str]:
        """Set the keys the handle sets in the input (in place, comments kept); returns their paths."""
        shape = self.shape_of(node)
        working = self._working.pop(shape.node if shape else node, None)
        if shape is None or not isinstance(shape.value, Mapping):
            return []
        # From the table as the drag left it: a handle's anchor is where the previous move put it.
        changed = apply_handle(working if isinstance(working, Mapping) else shape.value, handle, position, self.dim)
        for key, value in changed.items():
            self.doc.set(f"{shape.path}.{key}", value)
        self.doc.commit()
        return [f"{shape.path}.{k}" for k in changed]

    # Boundary coordinates

    def coordinate_rows(self, zone: str) -> list[tuple[str, Any]]:
        """The keys a click on the view can set for a boundary entry, with their values."""
        el = self.doc.get(f'boundaries[name = "{zone}"]')
        if not isinstance(el, Mapping):
            return []
        return [(k, el.get(k)) for k in COORDINATE_KEYS]

    def pick(self, path: str, point: Sequence[float]) -> None:
        """Set a point key (a boundary's `origin` or `toward`, a probe's point) to a point picked in the view."""
        self.doc.set(path, [float(f"{c:.6g}") + 0.0 for c in point[: self.dim]])
        self.doc.commit()

    def markers(self, zone: str) -> list[tuple[str, Vec]]:
        """A boundary's `origin` and `toward`, and where its coordinates start and which way u grows: the case
        view's frames, or for builds without them the check's text."""
        out: list[tuple[str, Vec]] = []
        for key, value in self.coordinate_rows(zone):
            if isinstance(value, list) and all(isinstance(v, (int, float)) for v in value):
                xs = [float(v) for v in value] + [0.0] * (3 - len(value))
                out.append((key, (xs[0], xs[1], xs[2])))
        structure = self.session.structure
        frames = structure.frames if structure else None
        if frames is not None:
            for f in frames:
                if f.get("zone") == zone:
                    out += frame_markers(f, self.dim)
            return out
        for b in structure.boundaries if structure else []:
            if b.get("zone") != zone:
                continue
            out += coordinate_points(b)
        return out

    # Inflow vectors

    def values(self) -> dict[str, Values]:
        """Mallard's values at the faces of each inflow zone, from the last check's `case.boundary_values`."""
        result = self.session.boundary_values
        if not result:
            return {}
        if self._values[0] is not result:
            by_index = boundary_values(result, self.session.input_path.parent)
            self._values = (result, {v.zone: v for v in by_index.values()})
        return self._values[1]

    def inflows(self, zones: Sequence[str], faces: Mapping[str, Faces], density: int | None) -> list[Inflow]:
        out = []
        values = self.values()
        for zone in zones:
            el = self.doc.get(f'boundaries[name = "{zone}"]')
            if not isinstance(el, Mapping) or (zone not in faces and zone not in values):
                continue
            f = inflow(zone, el, faces.get(zone, EMPTY), self.doc.data, self.dim, density, values.get(zone))
            if f is not None:
                out.append(f)
        return out

    def inflow_zones(self) -> list[str]:
        from .inflow import INFLOW_TYPES

        return [e.label for e in self.doc.elements("boundaries") if e.kind in INFLOW_TYPES]

    # Actions of the property panel

    def actions(self, node: str) -> list[dict[str, Any]]:
        """Buttons and pickers the property panel shows for a node: each an id, a label and its options."""
        kind, _, ident = node.partition(":")
        out: list[dict[str, Any]] = []
        if kind == "region" and ident.startswith("regions."):
            out.append({"id": "use_in", "label": "Use in", "options": rg.features(self.doc, self.session.catalog)})
            out.append({"id": "remove_region", "label": "Remove", "options": []})
        elif kind == "region" and self.doc.has(ident) and isinstance(self.doc.get(ident), Mapping):
            out.append({"id": "promote", "label": "Promote to [regions]", "options": []})
        return out

    def act(self, node: str, action: str, value: str, bounds: Sequence[float]) -> str | None:
        """Carry out an action; returns the node to select after it, if another."""
        ident = node.partition(":")[2]
        new: str | None = None
        if action == "add_region":
            new = "region:" + rg.add_region(self.doc, value, self.dim, bounds)
        elif action == "add_probe":
            new = f"el:probes[{rg.add_probe(self.doc, value, self.dim, bounds)}]"
        elif action == "use_in" and value:
            rg.use_in(self.doc, ident.removeprefix("regions."), value)
        elif action == "remove_region":
            rg.remove_region(self.doc, ident)
            new = "section:regions"
        elif action == "promote":
            new = "region:" + rg.promote(self.doc, ident, value)
        elif action.startswith("add_element:"):
            array = action.split(":", 1)[1]
            index = self.doc.append_element(array, {})
            new = f"el:{array}[{index}]"
        else:
            raise ValueError(f"unknown action {action!r}")
        self.doc.commit()
        return new


def canonical(doc: Any, path: str | None) -> str | None:
    """A key path with selectors resolved to indices (`write_data[prefix = "x"].region` -> `write_data[2].region`),
    so a case-view block and an input entry compare equal."""
    if not path:
        return None
    try:
        return format_path(doc.resolve(parse_path(path)))
    except (KeyError, ValueError):
        return None
