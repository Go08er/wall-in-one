"""The GUI keeps settings.toml read-only while it has keys this build lacks."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from wall_in_one import config, paths  # noqa: E402
from wall_in_one.library import playlists  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.preferences import PreferencesPage  # noqa: E402

UNKNOWN = b"future_setting = true\n"


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


def _settings_with_unknown_key(tmp_path: Path, **changes: Any) -> bytes:
    root = tmp_path / "Library"
    root.mkdir(exist_ok=True)
    base = config.Settings(roots=(root,), scan_workshop=False, cycle_interval=42)
    target = config.save(replace(base, **changes))
    document = target.read_bytes() + UNKNOWN
    target.write_bytes(document)
    return document


@pytest.fixture
def make_application() -> Iterator[Callable[[], Application]]:
    created: list[Application] = []

    def build() -> Application:
        app = Application()
        app._authoring_migration_ready = True
        created.append(app)
        return app

    yield build
    for app in created:
        app._runtime_shutdown = True
        authoring_jobs = app._authoring_jobs
        app._shutdown_authoring_jobs()
        if authoring_jobs is not None:
            authoring_jobs.shutdown(wait=True, cancel_futures=True)
        app._stills.shutdown()
        app.session.shutdown()


def _buttons(widget: Gtk.Widget) -> Iterator[Gtk.Button]:
    if isinstance(widget, Gtk.Button):
        yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _buttons(child)
        child = child.get_next_sibling()


def test_application_refuses_settings_writes_with_the_read_only_message(
    tmp_path: Path, make_application: Callable[[], Application]
) -> None:
    before = _settings_with_unknown_key(tmp_path)
    app = make_application()

    assert app.settings_unknown_keys == ("future_setting",)
    assert app.settings.cycle_interval == 42
    errors: list[str] = []
    assert app.update_settings_async(cycle_interval=60, on_error=errors.append) is False
    assert errors == [config.read_only_message(("future_setting",))]
    assert app.requested_settings.cycle_interval == 42
    with pytest.raises(config.SettingsReadOnlyError):
        app.update_settings(cycle_interval=60)
    assert app.settings.cycle_interval == 42
    assert paths.settings_path().read_bytes() == before


def test_preferences_shows_the_banner_and_offers_no_settings_edits(
    tmp_path: Path, make_application: Callable[[], Application]
) -> None:
    second = tmp_path / "Second"
    second.mkdir()
    before = _settings_with_unknown_key(
        tmp_path, roots=(tmp_path / "Library", second), dynamics_enabled=True
    )
    app = make_application()
    page = PreferencesPage(app)

    banner = page._read_only_banner
    assert banner.get_revealed()
    assert not banner.get_use_markup()
    title = banner.get_title()
    assert title.startswith("Settings are read-only. settings.toml has settings")
    assert "(future_setting)" in title
    assert "changes are disabled so they aren't lost" in title
    for control in page._settings_controls:
        assert not control.get_sensitive(), control
    root_buttons = [button for row in page._root_rows for button in _buttons(row)]
    assert len(root_buttons) == 3
    assert not any(button.get_sensitive() for button in root_buttons)
    # The Wallhaven key lives in its own file and stays editable.
    assert page._api_key_entry.get_sensitive()
    # So do the interface to start and GPU acceleration, in ui.toml.
    assert page._interface.get_sensitive() and page._gpu.get_sensitive()

    # Even a programmatic toggle cannot get a change saved: it is refused,
    # and the switch returns to the durable value.
    page._dynamics.set_active(False)
    assert page._dynamics.get_active() is True
    assert app.settings.dynamics_enabled is True
    assert paths.settings_path().read_bytes() == before


def test_preferences_for_known_settings_has_no_banner(
    tmp_path: Path, make_application: Callable[[], Application]
) -> None:
    root = tmp_path / "Library"
    root.mkdir()
    config.save(config.Settings(roots=(root,), scan_workshop=False))
    app = make_application()
    page = PreferencesPage(app)

    assert app.settings_unknown_keys == ()
    assert not page._read_only_banner.get_revealed()
    assert all(control.get_sensitive() for control in page._settings_controls)


def test_deleting_the_saved_default_playlist_is_refused_whole(
    tmp_path: Path, make_application: Callable[[], Application]
) -> None:
    store = playlists.Store.open()
    default = store.create("Evening")
    other = store.create("Morning")
    before = _settings_with_unknown_key(tmp_path, active_playlist=default.id)
    app = make_application()

    with pytest.raises(config.SettingsReadOnlyError):
        app._prepare_playlist_delete(default.id)()
    assert playlists.Store.open().get(default.id) is not None

    result = app._prepare_playlist_delete(other.id)()
    assert result.failures == ()
    assert result.settings is None
    assert playlists.Store.open().get(other.id) is None
    assert paths.settings_path().read_bytes() == before


def test_startup_repair_of_a_dangling_default_fails_closed_with_the_message(
    tmp_path: Path,
) -> None:
    """Fail-closed by choice: the runtime cannot compile a default naming nothing."""
    before = _settings_with_unknown_key(tmp_path, active_playlist="deleted")

    result = Application._repair_dangling_playlist_references()

    assert result.settings is None
    assert len(result.failures) == 1
    assert result.failures[0].startswith("saved default: settings.toml has settings")
    assert paths.settings_path().read_bytes() == before

    # With no dangling default there is nothing to write, so nothing to refuse.
    paths.settings_path().write_bytes(before.replace(b'"deleted"', b'""'))
    assert Application._repair_dangling_playlist_references().failures == ()
