"""A ``settings.toml`` that can't be saved: all or nothing, and a start that still opens.

An ordinary setup: the configuration folder is kept read-only (its permission
bits, a read-only mount, a declarative setup that owns it). Two things then
went wrong.

A change that spans files and saves the settings last was half done. Deleting
the playlist the saved default names took it out of ``playlists.json``, with
its rules and its displays, and then could not clear ``active_playlist``:
``settings.toml`` named a playlist that was gone, and every publication was
refused. Such a change now looks first whether that save can be made
(`config.why_not_writable`) and is refused whole when it can't.

And a start that found a default naming nothing could not clear it either,
rested its gate at "repair", and never loaded the library. It now goes on
without a default, says so, and names the file to change.

Real permission bits throughout; each test restores them. Those that say so
fail on d38cb66.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(os.geteuid() == 0, reason="root writes to a read-only folder"),
]

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_astra030_ac_saved import _reply  # noqa: E402
from tests.test_ui_codex030_round1b import (  # noqa: E402, F401
    Profile,
    _lane_idle,
    profiles,
)
from tests.test_ui_codex030_round1b import _playlist as _saved  # noqa: E402
from tests.test_ui_codex030_round1c import (  # noqa: E402, F401
    Shown,
    _playlist,
    shown,
    toolkit,
)
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.library import displays, playlists, schedules  # noqa: E402
from wall_in_one.ui.next.real_schedule import RealScheduling  # noqa: E402


@contextlib.contextmanager
def _settings_can_not_be_saved() -> Iterator[Path]:
    """The folder ``settings.toml`` is in, read-only for as long as this lasts."""
    folder = paths.settings_path().parent
    folder.chmod(0o500)
    try:
        yield folder
    finally:
        folder.chmod(0o700)


def _everything_saved() -> dict[Path, bytes | None]:
    files = (
        paths.settings_path(),
        playlists.state_path(),
        schedules.state_path(),
        displays.state_path(),
    )
    return {path: path.read_bytes() if path.exists() else None for path in files}


def _builds(application: Any) -> bool:
    try:
        runtime_config.render(config.load_strict(), application.session)
    except runtime_config.RuntimeConfigError:
        return False
    return True


# -- all or nothing -------------------------------------------------------------------------


@pytest.mark.parametrize("via", ["ctl", "service", "a window"])
def test_deleting_the_default_changes_nothing_when_the_settings_can_not_be_saved(
    profiles: Any,  # noqa: F811
    via: str,
) -> None:
    """Both windows delete through `delete_playlist_async`; ``ctl`` on the lane
    and in the headless service through the same task."""
    profile: Profile = profiles(service=via == "service")
    application = profile.application
    lone = _saved(profile, "Lone", [profile.dawn], used=True)
    application.session.displays.assign("DP-1", lone.id)
    application.update_settings(active_playlist=lone.id)
    held = _everything_saved()

    with _settings_can_not_be_saved() as folder:
        if via == "a window":
            said: list[str] = []
            application.delete_playlist_async(
                lone.id, lambda _result, _failures: said.append("deleted"), on_error=said.append
            )
            spin_until(lambda: bool(said) and _lane_idle(application), 30, what="the delete")
            message = said[-1]
        else:
            reply = _reply(profile, "playlist-delete", lone.id)
            assert not reply.ok and reply.kind == "local-io", reply
            message = reply.message

    # Failed on d38cb66: the playlist, its rule and its display's playlist were
    # gone, and settings.toml still named it as the default.
    assert _everything_saved() == held
    assert message == (
        "local-io: this playlist is the saved default, which deleting it would clear, and "
        f"settings.toml can't be changed (a file can't be made in {folder}: Permission denied)"
    )
    assert _builds(application)


def test_a_playlist_the_default_does_not_name_is_deleted_all_the_same(
    profiles: Any,  # noqa: F811
) -> None:
    """Nothing in the settings would change, however read-only they are."""
    profile: Profile = profiles()
    application = profile.application
    lone = _saved(profile, "Lone", [profile.dawn])
    other = _saved(profile, "Other", [profile.dusk])
    application.update_settings(active_playlist=lone.id)

    with _settings_can_not_be_saved():
        reply = _reply(profile, "playlist-delete", other.id)

    assert reply.ok, reply
    assert playlists.Store.open().get(other.id) is None
    assert config.load_strict().active_playlist == lone.id
    assert _builds(application)


def test_forget_display_forgets_nothing_when_its_colors_step_could_not_be_saved(
    shown: Shown,  # noqa: F811
) -> None:
    """Forget Display takes the display's rules and playlist, then stops the
    desktop colors following it: a settings save, last."""
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.dawn])
    application.session.displays.assign("DP-7", evening.id)
    application.update_settings(theme_source_connector="DP-7")
    assert application.settings.display_mode == config.DISPLAY_MODE_MIRRORED
    scheduling = opened.window.state.scheduling
    assert isinstance(scheduling, RealScheduling)
    held = _everything_saved()
    settled: list[bool] = []

    with _settings_can_not_be_saved():
        scheduling.forget_display("DP-7", settled.append)
        opened.wait("Forget to settle", lambda: bool(settled))

    # Failed on d38cb66: the display's playlist was forgotten, and the colors
    # were left following a display nothing knew.
    assert settled == [False]
    assert _everything_saved() == held
    assert opened.reports[-1].startswith(
        "Forgetting DP-7 was not saved; nothing changed: local-io: "
    )
    assert "settings.toml can't be changed" in opened.reports[-1]


# -- a start that still opens ---------------------------------------------------------------


@pytest.mark.parametrize("default", ["names nothing", "by name"])
def test_a_start_goes_on_past_a_default_it_can_not_save(
    profiles: Any,  # noqa: F811
    default: str,
) -> None:
    """``settings.toml`` names a default the start would repair: one that names
    nothing (cleared), or one by name (given the id). It can't be saved. Neither
    is a reason to hold the library back: the first is left out for this
    process, and said; the second names its playlist as it is."""
    profile: Profile = profiles()
    application = profile.application
    _saved(profile, "Lone", [profile.dawn])
    reference = "ghost" if default == "names nothing" else "lone"
    config.update({"active_playlist": reference})
    application._settings = config.load_strict()
    application.session.update_settings(application._settings, rescan_library=False)
    saved = paths.settings_path().read_bytes()

    with _settings_can_not_be_saved():
        partial = application._repair_dangling_playlist_references()
        response = application._finish_playlist_reference_repair(partial)

        # Failed on d38cb66: "authoring-repair-required", and writes stayed paused.
        assert response.ok, response
        assert bool(application.authoring_ready)
        assert paths.settings_path().read_bytes() == saved
        # What is published is built from the settings as they are saved.
        built = runtime_config.render(config.load_strict(), application.session)
        if default == "names nothing":
            assert f'default_playlist = "{runtime_config.FALLBACK_PLAYLIST_ID}"' in built

    about_it = [text for text in profile.window.reports if "default playlist" in text]
    if default == "by name":
        assert about_it == []
    else:
        (told,) = about_it
        assert told.startswith("The saved default playlist (ghost) doesn\u2019t exist")
        assert f"edit active_playlist in {paths.settings_path()}" in told
        # And once a playlist answers to it, it is the default again.
        made = _saved(profile, "ghost", [profile.dusk])
        again = runtime_config.render(config.load_strict(), application.session)
        assert f'default_playlist = "{made.id}"' in again
