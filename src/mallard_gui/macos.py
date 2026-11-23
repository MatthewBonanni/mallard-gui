"""On macOS, VTK's Cocoa render window makes the process a regular Cocoa application, with a Dock icon,
even when it renders offscreen. A server and its tests have no window of their own: keep them out of the
Dock by making NSApplication's activation policy prohibited, whoever asks for another one."""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from typing import Any

PROHIBITED = 2  # NSApplicationActivationPolicyProhibited
_SetPolicy = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)
_keep: list[Any] = []  # the replacement method must outlive the call that installs it


def _objc() -> ctypes.CDLL | None:
    objc_path = ctypes.util.find_library("objc")
    appkit_path = ctypes.util.find_library("AppKit")
    if not objc_path or not appkit_path:
        return None
    ctypes.cdll.LoadLibrary(appkit_path)
    objc = ctypes.cdll.LoadLibrary(objc_path)
    for name, restype, argtypes in (
        ("objc_getClass", ctypes.c_void_p, [ctypes.c_char_p]),
        ("sel_registerName", ctypes.c_void_p, [ctypes.c_char_p]),
        ("class_getInstanceMethod", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
        ("method_getImplementation", ctypes.c_void_p, [ctypes.c_void_p]),
        ("method_setImplementation", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
    ):
        fn = getattr(objc, name)
        fn.restype, fn.argtypes = restype, argtypes
    return objc


def _send(objc: ctypes.CDLL, restype: Any, *argtypes: Any) -> Any:
    return ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(
        ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value or 0
    )


def hide_from_dock() -> bool:
    """Before VTK makes a window: every later `setActivationPolicy:` sets the prohibited policy, so the
    process never registers as a regular application, not even for the moment between VTK's call and ours.
    Returns whether the policy is now prohibited (False off macOS)."""
    if sys.platform != "darwin":
        return False
    objc = _objc()
    if objc is None:
        return False
    ns_app = objc.objc_getClass(b"NSApplication")
    sel = objc.sel_registerName(b"setActivationPolicy:")
    if not _keep:
        method = objc.class_getInstanceMethod(ns_app, sel)
        original = _SetPolicy(objc.method_getImplementation(method) or 0)

        def prohibited(this: int, cmd: int, policy: int) -> bool:
            return bool(original(this, cmd, PROHIBITED))

        replacement = _SetPolicy(prohibited)
        _keep.extend([original, replacement])
        objc.method_setImplementation(method, ctypes.cast(replacement, ctypes.c_void_p))
    app = _send(objc, ctypes.c_void_p)(ns_app, objc.sel_registerName(b"sharedApplication"))
    _send(objc, ctypes.c_bool, ctypes.c_long)(app, sel, PROHIBITED)
    return activation_policy() == PROHIBITED


def activation_policy() -> int | None:
    """The process's activation policy (0 regular, with a Dock icon; 2 prohibited); None off macOS."""
    objc = _objc() if sys.platform == "darwin" else None
    if objc is None:
        return None
    app = _send(objc, ctypes.c_void_p)(
        objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication")
    )
    return int(_send(objc, ctypes.c_long)(app, objc.sel_registerName(b"activationPolicy")))
