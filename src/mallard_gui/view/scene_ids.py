"""Names of the scene's VTK objects for the browser's copy of the scene (vtk.js), one per object for good.

trame-vtk names an object by its address, and vtk.js keeps what it built under a name until a later update
removes it. An address names another object as soon as the first is freed: previews redrawn after a release
free their actors, mappers and properties and build new ones in the same update, and a new actor at a freed
property's address reached vtk.js as an update of that property. That update threw halfway and so did every
later one: shapes it removed stayed drawn, new ones never were, and the page's handles left them. A counter
per object, forgotten when the object is deleted, never names two objects alike.

And a renderer whose actors have all gone is still told to the browser. trame-vtk tells a renderer's changes
as calls (add this prop, remove that one) worked out against what it told last, notes the new list as told, and
then says nothing at all of a renderer left with no prop to draw: the removals were never sent, the browser
kept drawing every actor of the scene before, and no later update removed them (a results view between two
runs, drawn in the browser, showed the last run under the new one)."""

from __future__ import annotations

import importlib
import itertools
import sys
from typing import Any

from trame_vtk.modules.vtk.serializers import helpers
from trame_vtk.modules.vtk.serializers import initialize as _initialize
from trame_vtk.modules.vtk.serializers import registry as _registry
from vtkmodules.vtkCommonCore import vtkCommand

_names: dict[str, str] = {}  # address -> the name of the object living there
_count = itertools.count(1)


def address(ref: Any) -> str:
    """Where a VTK object lives: trame-vtk's own name for it."""
    return str(ref.__this__)[1:17]


def reference_id(ref: Any) -> str:
    if not ref:
        return "0x0"
    try:
        at = address(ref)
    except AttributeError:
        return str(ref)[-12:-1]  # trame-vtk's own fallback
    name = _names.get(at)
    if name is None:
        name = _names[at] = f"{next(_count):016x}"
        ref.AddObserver(vtkCommand.DeleteEvent, lambda *_: _forget(at, name))
    return name


def _forget(at: str, name: str) -> None:
    """The object is deleted: its address is free for another, and trame-vtk's hash of it (kept by name, which
    an address made reusable) would otherwise stay for good."""
    _names.pop(at, None)
    helpers.data_array_sha_mapping.pop(name, None)


def _told_when_empty(serializer: Any) -> Any:
    """trame-vtk's renderer serializer, which still tells the browser of a renderer it would say nothing of:
    the props to remove, as calls on the renderer."""

    def renderer(parent: Any, instance: Any, obj_id: str, context: Any, depth: int) -> Any:
        key = f"{obj_id}-props"
        told = list(context.get_last_dependency_list(key))
        state = serializer(parent, instance, obj_id, context, depth)
        if state is not None:
            return state
        kept = [x for x in context.last_dependencies_mapping.get(key, []) if x in told]
        context.set_new_dependency_list(key, kept)  # a prop noted as told in the state dropped is told next time
        return {
            "parent": reference_id(parent),
            "id": obj_id,
            "type": _registry.class_name(instance),
            "properties": {},
            "dependencies": [],
            "calls": [["removeViewProp", [f"instance:${{{x}}}"]] for x in told if x not in kept],
            "mtime": instance.GetMTime(),
        }

    renderer.mallard_wraps = serializer  # type: ignore[attr-defined]
    return renderer


def install() -> None:
    """trame-vtk's serializers name objects through this module's `reference_id`, and its renderer serializer
    is the one above: where it is registered from (`initialize`) and where it already is."""
    for part in ("serializers", "protocols"):
        importlib.import_module(f"trame_vtk.modules.vtk.{part}")
    if not hasattr(_initialize.renderer_serializer, "mallard_wraps"):
        plain = _initialize.renderer_serializer
        _initialize.renderer_serializer = _told_when_empty(plain)
        for name, registered in list(_registry.SERIALIZERS.items()):
            if registered is plain:
                _registry.SERIALIZERS[name] = _initialize.renderer_serializer
    for name, module in list(sys.modules.items()):
        if name.startswith("trame_vtk.modules.vtk") and hasattr(module, "reference_id"):
            setattr(module, "reference_id", reference_id)  # noqa: B010  (mypy: no such attribute on ModuleType)


install()
