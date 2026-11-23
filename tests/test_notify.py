from __future__ import annotations

from mallard_gui.notify import NONE, SHELL, TAURI, WEB, can_notify, parse


def test_the_page_reports_its_channel_and_the_web_permission() -> None:
    assert parse("shell") == (SHELL, "")
    assert parse("tauri") == (TAURI, "")
    assert parse("web:default") == (WEB, "default")
    assert parse("web:granted") == (WEB, "granted")
    assert parse("osascript") == (NONE, "")  # nothing the page does not know


def test_a_browser_notifies_only_once_granted() -> None:
    assert can_notify(SHELL, "") and can_notify(TAURI, "")
    assert can_notify(WEB, "granted")
    assert not can_notify(WEB, "default") and not can_notify(WEB, "denied") and not can_notify(NONE, "")
