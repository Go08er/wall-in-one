"""Live palette synchronization at the GTK application boundary."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from wall_in_one import paths  # noqa: E402
from wall_in_one.control import server  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.theme import noctalia, source  # noqa: E402
from wall_in_one.theme.palette import Palette  # noqa: E402
from wall_in_one.ui.app import (  # noqa: E402
    APPLICATION_STYLE_PRIORITY,
    PALETTE_RELOAD_DEBOUNCE_MS,
    Application,
    _Commands,
    _PaletteResult,
)


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def application() -> Iterator[Application]:
    instance = Application()
    yield instance
    instance._stop_palette_monitor()
    instance._shutdown_theme_jobs(wait=True)
    instance._stills.shutdown()
    instance._session.shutdown()


def _replace(path: Path, contents: str) -> None:
    temporary = path.with_name(f".{path.name}.test-tmp")
    temporary.write_text(contents, encoding="utf-8")
    os.replace(temporary, path)


def _spin_until(predicate: Callable[[], bool], seconds: float = 2.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return predicate()


def _spin_for(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.005)


def _resolved(detail: str) -> source.ResolvedPalette:
    return source.ResolvedPalette(
        palette=source.fallback_palette(),
        origin=source.Origin.GENERATED,
        detail=detail,
    )


def test_application_palette_wins_over_the_startup_user_stylesheet() -> None:
    assert APPLICATION_STYLE_PRIORITY == Gtk.STYLE_PROVIDER_PRIORITY_USER + 1


def test_disabling_noctalia_palette_following_uses_a_fixed_app_palette(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    application._settings = replace(application.settings, follow_noctalia_palette=False)
    monkeypatch.setattr(
        source,
        "resolve",
        lambda **_arguments: pytest.fail("disabled following must not inspect Noctalia"),
    )

    resolved = application.reload_palette()

    assert resolved.origin is source.Origin.FALLBACK
    assert "following is off" in resolved.detail


def test_atomic_palette_replacement_triggers_reload_without_the_socket(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[None] = []
    monkeypatch.setattr(application, "reload_palette", lambda: calls.append(None))
    application._start_palette_monitor()

    _replace(paths.palette_path(), "first")

    assert _spin_until(lambda: len(calls) == 1)


def test_rapid_atomic_palette_replacements_are_debounced(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[None] = []
    monkeypatch.setattr(application, "reload_palette", lambda: calls.append(None))
    application._start_palette_monitor()

    for index in range(4):
        _replace(paths.palette_path(), str(index))

    assert _spin_until(lambda: len(calls) == 1)
    _spin_for(PALETTE_RELOAD_DEBOUNCE_MS / 1000 * 3)
    assert len(calls) == 1


def test_noctalia_settings_changes_reload_when_no_palette_output_exists(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = paths.noctalia_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    calls: list[None] = []
    monkeypatch.setattr(application, "reload_palette", lambda: calls.append(None))
    application._start_palette_monitor()
    _replace(settings, '[theme]\nmode = "light"\n')
    assert _spin_until(lambda: len(calls) == 1)


def test_noctalia_settings_directory_created_after_start_is_observed(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = paths.noctalia_settings_path()
    assert not settings.parent.exists()
    calls: list[None] = []
    monkeypatch.setattr(application, "reload_palette", lambda: calls.append(None))
    application._start_palette_monitor()

    settings.parent.mkdir(parents=True)
    _replace(settings, '[theme]\nmode = "light"\n')
    assert _spin_until(lambda: len(calls) == 1)

    _replace(settings, '[theme]\nmode = "dark"\n')
    assert _spin_until(lambda: len(calls) == 2)


def test_real_palette_replacement_changes_css_and_adwaita_mode(
    application: Application,
) -> None:
    from tests.test_theme_source import _registration

    _registration()
    target = paths.palette_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    def rendered(mode: str, surface: str) -> str:
        palette = source.fallback_palette("light" if mode == "light" else "dark")
        colors = {key: value.hex for key, value in palette.colours.items()}
        colors["surface"] = surface
        return json.dumps({"mode": mode, "colors": colors})

    display = Gdk.Display.get_default()
    assert display is not None
    Gtk.StyleContext.add_provider_for_display(
        display, application._provider, APPLICATION_STYLE_PRIORITY
    )
    existing = Gtk.Window()
    try:
        application._start_palette_monitor()
        for mode, surface_hex, adw_mode in (
            ("dark", "#131318", Adw.ColorScheme.FORCE_DARK),
            ("dark", "#182927", Adw.ColorScheme.FORCE_DARK),
            ("light", "#f9f9ff", Adw.ColorScheme.FORCE_LIGHT),
            ("dark", "#131318", Adw.ColorScheme.FORCE_DARK),
        ):
            _replace(target, rendered(mode, surface_hex))

            def applied(wanted: str = mode, wanted_surface: str = surface_hex) -> bool:
                return (
                    application.resolved_palette is not None
                    and application.resolved_palette.origin is source.Origin.TEMPLATE
                    and application.resolved_palette.palette.mode == wanted
                    and application.resolved_palette.palette["surface"].hex == wanted_surface
                )

            assert _spin_until(applied)
            assert Adw.StyleManager.get_default().get_color_scheme() == adw_mode
            palette = application.resolved_palette
            assert palette is not None
            surface = palette.palette["surface"]
            reopened = Gtk.Window()
            try:
                for window in (existing, reopened):
                    found, color = window.get_style_context().lookup_color("window_bg_color")
                    assert found
                    assert color.red == pytest.approx(surface.red / 255, abs=0.001)
                    assert color.green == pytest.approx(surface.green / 255, abs=0.001)
                    assert color.blue == pytest.approx(surface.blue / 255, abs=0.001)
            finally:
                reopened.destroy()
    finally:
        existing.destroy()
        Gtk.StyleContext.remove_provider_for_display(display, application._provider)


def test_unrenderable_palette_preserves_last_good_state_and_answers_callbacks(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = source.fixed()
    application._resolved = previous
    application._apply_stylesheet(previous)
    previous_css = application._provider.to_string()
    reports: list[str] = []
    replies: list[tuple[object, str]] = []
    monkeypatch.setattr(application, "window_report", reports.append)
    application._theme_reload_callbacks.append(
        lambda palette, error: replies.append((palette, error))
    )
    partial = source.ResolvedPalette(
        Palette.from_template_document('{"mode":"light","colors":{"primary":"#fff"}}'),
        source.Origin.TEMPLATE,
        "incomplete",
    )
    application._finish_palette_resolution(_PaletteResult(application._theme_generation, partial))
    assert application.resolved_palette is previous
    assert application._provider.to_string() == previous_css
    assert len(replies) == 1 and replies[0][0] is None
    assert "token" in replies[0][1]
    assert reports and "keeping current colours" in reports[0]


def test_a_blocked_noctalia_resolution_does_not_stop_the_gtk_heartbeat(
    application: Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    pulses = 0

    def blocked(**_arguments: object) -> source.ResolvedPalette:
        started.set()
        assert release.wait(2.0)
        return _resolved("worker result")

    def heartbeat() -> bool:
        nonlocal pulses
        pulses += 1
        return GLib.SOURCE_CONTINUE

    monkeypatch.setattr(source, "resolve", blocked)
    heartbeat_source = GLib.timeout_add(5, heartbeat)
    try:
        before = time.monotonic()
        application.reload_palette()
        assert time.monotonic() - before < 0.05
        assert _spin_until(started.is_set)
        _spin_for(0.08)
        assert pulses >= 5
    finally:
        release.set()
        GLib.source_remove(heartbeat_source)
    assert _spin_until(
        lambda: (
            application.resolved_palette is not None
            and application.resolved_palette.detail == "worker result"
        )
    )


def test_a_newer_reload_suppresses_a_stale_worker_result(
    application: Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    applied: list[str] = []

    def resolve(*, scheme: str, **_arguments: object) -> source.ResolvedPalette:
        if scheme == "vibrant":
            started.set()
            assert release.wait(2.0)
        return _resolved(scheme)

    monkeypatch.setattr(source, "resolve", resolve)
    monkeypatch.setattr(
        application,
        "_apply_stylesheet",
        lambda resolved: applied.append(resolved.detail),
    )
    application._resolved = _resolved("previous")
    application._settings = replace(application.settings, preview_scheme="vibrant")
    application.reload_palette()
    assert _spin_until(started.is_set)

    application._settings = replace(application.settings, preview_scheme="soft")
    application.reload_palette()
    release.set()

    assert _spin_until(
        lambda: (
            application.resolved_palette is not None
            and application.resolved_palette.detail == "soft"
        )
    )
    assert applied == ["soft"]


def test_an_explicit_choice_runs_before_a_pending_reload(
    application: Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def resolve(*, scheme: str, **_arguments: object) -> source.ResolvedPalette:
        order.append(f"resolve {scheme}")
        if scheme == "vibrant":
            started.set()
            assert release.wait(2.0)
        return _resolved(scheme)

    def message(
        command: str,
        palette_source: str,
        name: str,
        **_keywords: object,
    ) -> str:
        order.append(f"{command} {palette_source} {name}")
        return ""

    monkeypatch.setattr(source, "resolve", resolve)
    monkeypatch.setattr(noctalia, "message", message)
    application._settings = replace(application.settings, preview_scheme="vibrant")
    application.reload_palette()
    assert _spin_until(started.is_set)

    application._settings = replace(application.settings, preview_scheme="soft")
    application.reload_palette()
    application.apply_noctalia_palette_async("custom", "Mine")
    release.set()

    assert _spin_until(lambda: order[-1:] == ["resolve soft"])
    assert order == [
        "resolve vibrant",
        "color-scheme-set custom Mine",
        "resolve soft",
    ]


def test_queued_palette_choices_coalesce_and_stale_callbacks_do_not_land(
    application: Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    selected: list[str] = []
    callbacks: list[str] = []

    def message(
        _command: str,
        _palette_source: str,
        name: str,
        **_keywords: object,
    ) -> str:
        selected.append(name)
        if name == "First":
            started.set()
            assert release.wait(2.0)
        return ""

    monkeypatch.setattr(noctalia, "message", message)
    application._settings = replace(application.settings, follow_noctalia_palette=False)
    application.apply_noctalia_palette_async(
        "custom", "First", on_complete=lambda _error: callbacks.append("First")
    )
    assert _spin_until(started.is_set)
    application.apply_noctalia_palette_async(
        "custom", "Second", on_complete=lambda _error: callbacks.append("Second")
    )
    application.apply_noctalia_palette_async(
        "custom", "Newest", on_complete=lambda _error: callbacks.append("Newest")
    )
    release.set()

    assert _spin_until(lambda: callbacks == ["Newest"])
    assert selected == ["First", "Newest"]


def test_noctalia_action_failure_is_delivered_on_gtk(
    application: Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gtk_thread = threading.get_ident()
    landed: list[tuple[int, str]] = []

    def fail(*_arguments: str, **_keywords: object) -> str:
        raise noctalia.NoctaliaError("shell rejected the palette")

    monkeypatch.setattr(noctalia, "message", fail)
    application._settings = replace(application.settings, follow_noctalia_palette=False)
    application.apply_noctalia_palette_async(
        "custom",
        "Broken",
        on_complete=lambda error: landed.append((threading.get_ident(), error)),
    )

    assert _spin_until(lambda: bool(landed))
    assert landed == [(gtk_thread, "shell rejected the palette")]


def test_palette_worker_failures_are_reported_and_answer_deferred_control(
    application: Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports: list[str] = []
    replies: list[Response] = []
    application._resolved = _resolved("last known good")
    monkeypatch.setattr(application, "window_report", reports.append)
    monkeypatch.setattr(
        source,
        "resolve",
        lambda **_arguments: (_ for _ in ()).throw(RuntimeError("generator exploded")),
    )

    outcome = _Commands(application).reload_palette()
    assert isinstance(outcome, server.Deferred)
    outcome.start(replies.append)

    assert _spin_until(lambda: bool(replies))
    assert not replies[0].ok
    assert "generator exploded" in replies[0].message
    assert reports == ["Palette reload failed; keeping current colours: generator exploded"]
    assert application.resolved_palette is not None
    assert application.resolved_palette.detail == "last known good"


def test_shutdown_invalidates_a_late_palette_delivery(
    application: Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    applied: list[str] = []
    callbacks: list[str] = []
    application._resolved = _resolved("last known good")

    def blocked(**_arguments: object) -> source.ResolvedPalette:
        started.set()
        assert release.wait(2.0)
        return _resolved("too late")

    monkeypatch.setattr(source, "resolve", blocked)
    monkeypatch.setattr(
        application,
        "_apply_stylesheet",
        lambda resolved: applied.append(resolved.detail),
    )
    application.reload_palette(lambda _resolved, _error: callbacks.append("landed"))
    assert _spin_until(started.is_set)

    application._shutdown_theme_jobs()
    release.set()
    assert _spin_until(lambda: not application._theme_draining)
    _spin_for(0.03)

    assert applied == []
    assert callbacks == []
    assert application.resolved_palette is not None
    assert application.resolved_palette.detail == "last known good"
