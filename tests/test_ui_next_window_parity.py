"""Window-level behaviour the new interface keeps from the classic window.

Each test that describes shared behaviour runs against both windows, built by
`Application(ui=...)` named explicitly, and asserts the same outcome.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from tests.gtk_helpers import destroy_window, spin_until  # noqa: E402
from tests.started import started_application  # noqa: E402
from tests.test_next_status_line import two_display_status  # noqa: E402
from wall_in_one import config  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.log_section import LogSection  # noqa: E402
from wall_in_one.ui.next.settings import SettingsPage  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.window_services import UiKind  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture(params=["classic", "next"])
def shown(request: pytest.FixtureRequest) -> Iterator[tuple[Application, Gtk.Window]]:
    """An application with the window ``--ui`` names, shown, and closed afterwards."""
    ui = cast(UiKind, request.param)
    application = started_application(ui, first_activation_done=True)
    window = application._build_window()
    application._window = window
    window.present()
    try:
        yield application, window
    finally:
        application._window = None
        window.destroy()
        application._stills.shutdown()
        application.session.shutdown()


def test_each_runtime_error_is_said_once_in_either_window(
    shown: tuple[Application, Gtk.Window], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classic window toasts a stopped renderer, degraded display discovery
    and an invalid status reply, each once until it changes; so does the new one."""
    application, window = shown
    said: list[str] = []
    monkeypatch.setattr(window, "report", said.append)
    model = application.status_model

    crashed = two_display_status(extra={"last_error": "linux-wallpaperengine exited (1)"})
    model.adopt(crashed)
    model.adopt(crashed)  # the next poll: the same error, not said again
    assert said == ["linux-wallpaperengine exited (1)"]

    degraded = two_display_status(
        extra={"output_discovery_error": "the Wayland compositor did not answer within 3000 ms"}
    )
    model.adopt(degraded)
    model.adopt(degraded)
    assert said[1:] == [
        "Display discovery is degraded: the Wayland compositor did not answer within 3000 ms"
    ]

    model.mark_invalid("status reply is not JSON")
    model.mark_invalid("status reply is not JSON")
    assert said[2:] == ["status reply is not JSON"]

    model.adopt(two_display_status())  # all clear
    model.mark_unavailable(forget=True)
    model.adopt(crashed)  # the service is back with the old error: said again
    assert said[3:] == ["linux-wallpaperengine exited (1)"]


def _actions(menu: Gio.MenuModel) -> list[str]:
    found: list[str] = []
    for index in range(menu.get_n_items()):
        action = menu.get_item_attribute_value(index, "action", GLib.VariantType.new("s"))
        if action is not None:
            found.append(action.get_string())
        section = menu.get_item_link(index, "section")
        if section is not None:
            found.extend(_actions(section))
    return found


def test_rescan_is_in_the_new_windows_main_menu_as_in_the_classic_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = started_application(ui="next", first_activation_done=True)
    rescans: list[bool] = []
    monkeypatch.setattr(application, "refresh_library", lambda **_: rescans.append(True))
    window = NextWindow(application, application.settings)
    try:
        sections = window.extra_menu_sections()
        assert [_actions(menu) for _label, menu in sections] == [["win.refresh"]]
        label = sections[0][1].get_item_attribute_value(0, "label", GLib.VariantType.new("s"))
        assert label is not None and label.get_string() == "Rescan the Library"
        window.activate_action("win.refresh", None)
        assert rescans == [True]
    finally:
        destroy_window(window)
        application._stills.shutdown()
        application.session.shutdown()


def test_settings_shows_this_sessions_log_with_file_paths_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the user was told and what the runtime said, as it changed; Copy copies it,
    with the home folder, the library folders and the login name hidden."""
    monkeypatch.setenv("LOGNAME", "wio-tester")
    monkeypatch.setenv("USER", "wio-tester")
    library = Path.home() / 'Walls "for" Bob\u2019s desk'
    library.mkdir(parents=True)
    config.update({"roots": (library,)})
    application = started_application(ui="next")
    window = NextWindow(application, application.settings)
    application._window = window
    toasts: list[str] = []
    window.state.connect("toast", lambda _state, text, _button: toasts.append(text))
    window.present()
    home = Path.home()
    try:
        page = window.pages["settings"]
        assert isinstance(page, SettingsPage)
        section = page.log
        assert isinstance(section, LogSection)
        application.window_report(f"Using {home}/walls as the wallpaper library")
        application.status_model.adopt(
            two_display_status(extra={"last_error": f"mpv could not open {library}/a b.mp4"})
        )
        application.window_report("Steam says /home/wio-tester/.steam is missing")
        window.navigate("settings:logs")
        spin_until(section.get_mapped)
        assert section.shown_lines == [
            "Using ~/walls as the wallpaper library",
            "The wallpaper service is running",
            "Playing Evening (schedule)",
            "mpv could not open <library 1>/a b.mp4",
            "Steam says /home/<user>/.steam is missing",
        ]
        assert section.hiding.get_active(), "on whenever Wall-in-One starts"
        assert "File names stay readable" in (section.hiding.get_subtitle() or ""), "not anonymity"
        assert "5 entries, 1 problem." in (section.get_description() or "")

        section.hiding.set_active(False)
        assert section.shown_lines[3] == f"mpv could not open {library}/a b.mp4"
        section.hiding.set_active(True)

        section.copy.emit("clicked")
        assert toasts == ["Copied 5 lines with file paths hidden"]
        copied: list[str | None] = []

        def read(clipboard: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
            copied.append(clipboard.read_text_finish(result))

        window.get_clipboard().read_text_async(None, read)
        spin_until(lambda: bool(copied))
        assert copied[0] == section.text()
        assert copied[0] is not None
        for secret in (str(home), "wio-tester", "Bob\u2019s"):
            assert secret not in copied[0]

        application.window_report("one more")  # followed while shown
        assert section.shown_lines[-1] == "one more"
    finally:
        application._window = None
        window.destroy()
        application._stills.shutdown()
        application.session.shutdown()
