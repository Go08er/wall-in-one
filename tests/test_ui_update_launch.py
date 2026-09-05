"""A desktop launch must not silently activate a different installed package."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from wall_in_one import config, paths  # noqa: E402
from wall_in_one.theme import source  # noqa: E402
from wall_in_one.theme.palette import Colour, Palette  # noqa: E402
from wall_in_one.ui import app, readonly_palette, update_prompt  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    Gtk.init()
    Adw.init()


def test_application_exports_read_only_package_identity() -> None:
    application = app.Application()
    try:
        identity = application.lookup_action(app.PACKAGE_SOURCE_ACTION)
        assert identity is not None and not identity.get_enabled()
        state = identity.get_state()
        assert state is not None and state.unpack() == app.PACKAGE_SOURCE
        identity.change_state(GLib.Variant("s", "pretend-newer-package"))
        state = identity.get_state()
        assert state is not None and state.unpack() == app.PACKAGE_SOURCE
        assert Path(app.PACKAGE_SOURCE).name == "wall_in_one"
        assert application.lookup_action(app.PRESENT_PACKAGE_ACTION) is not None
    finally:
        application._stills.shutdown()
        application._session.shutdown()


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("source", [None, "older-package", app.PACKAGE_SOURCE])
@pytest.mark.parametrize("service", [False, True])
def test_launch_registration_precedes_generation_checked_activation(
    monkeypatch: pytest.MonkeyPatch, remote: bool, source: str | None, service: bool
) -> None:
    events: list[object] = []

    class StubApplication:
        _stills = SimpleNamespace(shutdown=lambda: events.append("local stills closed"))
        _session = SimpleNamespace(shutdown=lambda: events.append("local session closed"))

        def __init__(self, **_kwargs: object) -> None:
            pass

        def register(self, _cancel: object) -> None:
            events.append("registered")

        def get_is_remote(self) -> bool:
            assert events[0] == "registered"
            return remote

        def has_action(self, _name: str) -> bool:
            return source is not None

        def get_action_state(self, _name: str) -> GLib.Variant:
            return GLib.Variant("s", source)

        def activate(self) -> None:
            pytest.fail("silently activated an unverified package")

        def activate_action(self, name: str, parameters: GLib.Variant) -> None:
            events.append((name, parameters.unpack()))

        def get_dbus_connection(self) -> Any:
            return SimpleNamespace(flush_sync=lambda _cancel: events.append("flushed"))

        def run(self, _argv: object) -> int:
            assert not remote, "run would forward an unqualified remote activation"
            events.append("primary run")
            return 0

    monkeypatch.setattr(app, "Application", StubApplication)

    def show_notice(_show: object) -> int:
        events.append("update notice")
        return 0

    monkeypatch.setattr(update_prompt, "run", show_notice)
    assert app.run(initial_page="settings", service=service) == (
        75 if remote and service and source != app.PACKAGE_SOURCE else 0
    )
    if not remote:
        assert events == ["registered", "primary run"]
    elif source == app.PACKAGE_SOURCE:
        assert events == [
            "registered",
            "local stills closed",
            "local session closed",
        ] + (
            []
            if service
            else [(app.PRESENT_PACKAGE_ACTION, (app.PACKAGE_SOURCE, "settings")), "flushed"]
        )
    else:
        assert events == [
            "registered",
            "local stills closed",
            "local session closed",
        ] + ([] if service else ["update notice"])


def test_registration_failure_closes_local_resources_without_opening_a_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []

    class BrokenRegistration:
        _stills = SimpleNamespace(shutdown=lambda: events.append("stills closed"))
        _session = SimpleNamespace(shutdown=lambda: events.append("session closed"))

        def __init__(self, **_kwargs: object) -> None:
            pass

        def register(self, _cancel: object) -> None:
            raise GLib.Error("session disconnected")

    monkeypatch.setattr(app, "Application", BrokenRegistration)
    assert app.run() == 1
    assert events == ["stills closed", "session closed"]
    assert "Cannot register Wall-in-One" in capsys.readouterr().err


def test_owner_change_cannot_redirect_a_qualified_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = app.Application()
    shown: list[str] = []
    monkeypatch.setattr(application, "present_page", shown.append)
    try:
        action = application.lookup_action(app.PRESENT_PACKAGE_ACTION)
        assert action is not None
        action.activate(GLib.Variant("(ss)", ("different-package", "settings")))
        assert not shown
        action.activate(GLib.Variant("(ss)", (app.PACKAGE_SOURCE, "settings")))
        assert shown == ["settings"]
    finally:
        application._stills.shutdown()
        application._session.shutdown()


def test_update_notice_is_read_only_and_showing_old_app_requires_a_click() -> None:
    shown: list[bool] = []
    failures: list[BaseException] = []
    notice = update_prompt.UpdateNotice(lambda: shown.append(True))

    # A display-backed widget check; actual cross-process registration and
    # second-launch ownership are exercised by the old/new desktop VM.
    def activated(_notice: update_prompt.UpdateNotice) -> None:
        try:
            assert not shown
            assert notice._window is not None
            assert notice._window.get_title() == update_prompt.TITLE
            assert "Finish downloads and pending saves" in update_prompt.DESCRIPTION
            notice._on_show_running(Gtk.Button())
            assert shown == [True]
        except BaseException as error:
            failures.append(error)
        finally:
            if notice._window is not None:
                notice._window.destroy()
            notice.quit()

    notice.connect_after("activate", activated)
    assert notice.run([]) == 0
    assert not failures, failures
    assert shown == [True]


def spin_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "GTK condition did not arrive"
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)


def test_notice_follows_atomic_palette_changes_and_dark_light_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.save(config.Settings())
    palette_path = paths.palette_path()
    palette_path.parent.mkdir(parents=True, exist_ok=True)
    main_thread = threading.get_ident()

    def resolve(**_kwargs: object) -> source.ResolvedPalette:
        assert threading.get_ident() != main_thread
        value = source.from_template()
        assert value is not None
        return value

    monkeypatch.setattr(source, "resolve", resolve)
    notice = update_prompt.UpdateNotice(lambda: None)
    display = Gdk.Display.get_default()
    assert display is not None
    Gtk.StyleContext.add_provider_for_display(
        display, notice._provider, app.APPLICATION_STYLE_PRIORITY
    )
    window = Gtk.Window()
    try:
        notice._colours.start()
        spin_until(lambda: notice._colours._watch_ready)
        for mode, expected in (
            ("light", Adw.ColorScheme.FORCE_LIGHT),
            ("dark", Adw.ColorScheme.FORCE_DARK),
        ):
            palette = source.fallback_palette("light" if mode == "light" else "dark")
            temporary = palette_path.with_name(".test-render")
            temporary.write_text(
                json.dumps(
                    {
                        "mode": mode,
                        "colors": {key: value.hex for key, value in palette.colours.items()},
                    }
                )
            )
            os.replace(temporary, palette_path)

            def applied(
                expected: Adw.ColorScheme = expected, surface: Colour = palette["surface"]
            ) -> bool:
                found, colour = window.get_style_context().lookup_color("window_bg_color")
                return bool(
                    found
                    and notice.get_style_manager().get_color_scheme() == expected
                    and abs(colour.red - surface.red / 255) < 0.001
                    and abs(colour.green - surface.green / 255) < 0.001
                    and abs(colour.blue - surface.blue / 255) < 0.001
                )

            spin_until(applied)
            found, colour = window.get_style_context().lookup_color("window_bg_color")
            assert found
            assert colour.red == pytest.approx(palette["surface"].red / 255, abs=0.001)
        assert not paths.socket_path().exists()
    finally:
        notice._colours.close()
        Gtk.StyleContext.remove_provider_for_display(display, notice._provider)
        window.destroy()


def test_notice_closes_without_waiting_for_a_blocked_colour_read_or_creating_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, released = threading.Event(), threading.Event()

    def slow(**_kwargs: object) -> source.ResolvedPalette:
        entered.set()
        assert released.wait(3)
        return source.fixed()

    monkeypatch.setattr(source, "resolve", slow)
    notice = update_prompt.UpdateNotice(lambda: None)
    try:
        notice._colours.start()
        spin_until(entered.is_set)
        before = notice._provider.to_string()
        began = time.monotonic()
        notice._colours.close()
        assert time.monotonic() - began < 0.25
        assert not notice._colours._monitors
        assert not paths.settings_path().parent.exists()
        assert not paths.palette_path().parent.exists()
        released.set()
        notice._colours.reload()
        assert notice._provider.to_string() == before
    finally:
        released.set()
        notice._colours.close()


def test_notice_keeps_good_colours_when_generated_palette_cannot_render(
    capsys: pytest.CaptureFixture[str],
) -> None:
    notice = update_prompt.UpdateNotice(lambda: None)
    notice._apply_colours(source.fixed(), 1.0)
    before = notice._provider.to_string()
    follower = notice._colours
    follower._watch_ready = True
    malformed = source.ResolvedPalette(Palette("dark", {}), source.Origin.GENERATED, "test")
    try:
        follower._finish(readonly_palette._Result(0, malformed, 1.0, ()))
        assert notice._provider.to_string() == before
        assert "keeps its current colours" in capsys.readouterr().err
        # A bad generated response cannot disable following later good colours.
        light = source.ResolvedPalette(
            source.fallback_palette("light"), source.Origin.GENERATED, "test recovery"
        )
        follower._finish(readonly_palette._Result(0, light, 1.0, ()))
        assert notice._provider.to_string() != before
        assert notice.get_style_manager().get_color_scheme() == Adw.ColorScheme.FORCE_LIGHT
    finally:
        follower.close()
