"""Desktop notifications, always shown by the page, never by this process.

The server only queues a notification in the state (`h_native`); the page shows it through the first channel
it has:

- `shell`: the desktop app's bridge, `window.mallardShell.notify({title, body})`, which the Tauri shell
  implements with its notification plugin, so a notification carries the app's name and icon;
- `tauri`: Tauri's notification plugin directly, when the shell exposes its globals but no bridge;
- `web`: the Web Notifications API in a browser, once the user has granted the permission (asked when they
  turn Notify on);
- `none`: in-app toasts only.
"""

from __future__ import annotations

SHELL, TAURI, WEB, NONE = "shell", "tauri", "web", "none"
CHANNELS = (SHELL, TAURI, WEB, NONE)
ICON = "mallard_gui/favicon.png"

_BRIDGE = "window.mallardShell && window.mallardShell.notify"
_PLUGIN = "window.__TAURI__ && window.__TAURI__.notification"

# The channel and, for `web`, the permission, as "web:granted" and so on.
DETECT = (
    f"({_BRIDGE}) ? '{SHELL}' : ({_PLUGIN}) ? '{TAURI}' : "
    f"window.Notification ? '{WEB}:' + window.Notification.permission : '{NONE}'"
)

# Shows the queued notification `h_native` through the first channel the page has.
DELIVER = (
    "h_native.id && ("
    f"({_BRIDGE}) ? window.mallardShell.notify({{title: h_native.title, body: h_native.body}}) : "
    f"({_PLUGIN}) ? window.__TAURI__.notification.sendNotification({{title: h_native.title, body: h_native.body}}) : "
    "(window.Notification && window.Notification.permission === 'granted') ? "
    "new window.Notification(h_native.title, {body: h_native.body, icon: h_native.icon, tag: 'mallard-' + h_native.id})"
    " : null)"
)

# On turning Notify on in a browser that has not been asked yet: the permission prompt (needs the click).
ASK = (
    f"h_route === '{WEB}' && $event.target.checked && window.Notification && "
    "window.Notification.permission === 'default' && "
    "window.Notification.requestPermission().then(p => { h_perm = p })"
)


def parse(detected: str) -> tuple[str, str]:
    """The channel and the web permission from `DETECT`'s result."""
    channel, _, permission = str(detected).partition(":")
    if channel not in CHANNELS:
        return NONE, ""
    return channel, permission if channel == WEB else ""


def can_notify(channel: str, permission: str) -> bool:
    return channel in (SHELL, TAURI) or (channel == WEB and permission == "granted")
