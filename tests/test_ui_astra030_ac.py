"""AC-1 and AC-2: what another program saved between a window's snapshot and its write.

Nothing watches ``settings.toml`` or the stores. Another program (``ctl`` in
another session, the service, a hand edit) can finish saving long before a
gesture is made in a window that still shows what it read when it opened.
A gesture that is relative to what is saved has to be applied to, and judged
against, what is read at the write, never a whole value built from the window's
snapshot:

- a folder gesture (add, remove, make the download folder) is an operation on
  the folders ``settings.toml`` holds then (AC-1);
- "does anything name this playlist?" is asked of each reference where it is
  read, by the compiler's own lookup: its id, or its name in any letter case
  (AC-2).

Each test here runs the real application, has a second process save something,
and then makes the gesture. Each fails on 7bdc1a6.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gio, Gtk  # noqa: E402

from tests import released  # noqa: E402
from tests.gtk_helpers import spin_until  # noqa: E402
from tests.released import released_source  # noqa: E402, F401
from tests.test_ui_codex030_round1c import (  # noqa: E402, F401
    Shown,
    _playlist,
    shown,
    toolkit,
)
from tests.test_ui_playlists_polish import QuietLoader  # noqa: E402
from tests.whole_document import builds_whole  # noqa: E402
from wall_in_one import config, paths  # noqa: E402
from wall_in_one.control import server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import displays, playlists, schedules  # noqa: E402
from wall_in_one.ui import playlists_page, preferences  # noqa: E402
from wall_in_one.ui.app import _Commands  # noqa: E402
from wall_in_one.ui.next.real_playlists import RealPlaylists  # noqa: E402

#: Another program: its own process, saving through the same files.
_ANOTHER_PROGRAM = r"""
import json
import sys
from pathlib import Path

from wall_in_one import config
from wall_in_one.library import displays, schedules

wanted = json.loads(sys.argv[1])
settings = dict(wanted.get("settings", {}))
if "roots" in settings:
    settings["roots"] = tuple(Path(root) for root in settings["roots"])
if settings:
    config.update(settings)
for playlist in wanted.get("rules", []):
    schedules.Store.open().add(playlist)
for connector, playlist in wanted.get("assignments", {}).items():
    displays.Store.open().assign(connector, playlist)
"""


def _another_program_saves(
    *,
    rules: tuple[str, ...] = (),
    assignments: dict[str, str] | None = None,
    **settings: object,
) -> None:
    """A second process saves, and has finished before anything else happens here."""
    project = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(project / "src") + (os.pathsep + existing if existing else "")
    wanted = {
        "settings": {
            key: [str(root) for root in value] if key == "roots" else value  # type: ignore[attr-defined]
            for key, value in settings.items()
        },
        "rules": list(rules),
        "assignments": assignments or {},
    }
    subprocess.run(
        [sys.executable, "-c", _ANOTHER_PROGRAM, json.dumps(wanted)],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _builds_from_what_is_saved(shown: Shown) -> bool:  # noqa: F811
    """Whether the runtime document is built whole from ``settings.toml`` as it is.

    Nothing left out and no playlist dropped: what "it can be built" meant
    while one playlist that can't play refused the whole compile
    (`tests.whole_document`).
    """
    return builds_whole(config.load_strict(), shown.opened.application.session)


# -- AC-1: folder gestures ------------------------------------------------------------------


class _Chose:
    """A folder chooser that has just been answered with ``folder``."""

    def __init__(self, folder: Path) -> None:
        self._folder = folder

    def select_folder_finish(self, _result: object) -> Gio.File:
        return Gio.File.new_for_path(str(self._folder))


@pytest.mark.parametrize("window", ["new", "classic"])
@pytest.mark.parametrize("gesture", ["add", "remove", "make-download-folder"])
def test_a_folder_gesture_keeps_a_folder_another_program_saved(
    shown: Shown,  # noqa: F811
    tmp_path: Path,
    gesture: str,
    window: str,
) -> None:
    """The window opened on two folders. Another program then saves a third and
    is done. The window, which has no way to know, makes one folder gesture. The
    gesture is applied to the three folders that are saved, not written as a
    whole list built from the two the window remembers: the third stays, and the
    window then shows what was actually saved."""
    opened = shown.opened
    application = opened.application
    walls, walls2 = shown.walls, shown.walls2
    outside, chosen = tmp_path / "outside", tmp_path / "chosen"
    for folder in (outside, chosen):
        folder.mkdir()
        (folder / "sea.png").write_bytes(b"placeholder image bytes")
    _another_program_saves(roots=(walls, walls2, outside))
    assert application.settings.roots == (walls, walls2), "the window knows nothing of it"

    access = opened.page.access
    assert access is not None
    classic = preferences.PreferencesPage(application)
    classic.apply_settings(application.settings)
    expected: tuple[Path, ...]
    if gesture == "add":
        expected = (walls, walls2, outside, chosen)
        if window == "new":
            access.add_folder(str(chosen))
        else:
            classic._on_root_chosen(_Chose(chosen), None)  # type: ignore[arg-type]
    elif gesture == "remove":
        expected = (walls2, outside)
        if window == "new":
            access.remove_folder(str(walls))
        else:
            classic._make_root_remover(walls)(Gtk.Button())
    else:
        expected = (walls2, walls, outside)
        if window == "new":
            access.make_download_folder(str(walls2))
        else:
            classic._make_root_primary(walls2)(Gtk.Button())
    opened.wait("the gesture to settle")

    # Failed on 7bdc1a6: the other program's folder was gone from what was saved.
    assert config.load_strict().roots == expected
    assert application.settings.roots == expected, "and the app holds what was saved"
    assert application.requested_settings.roots == expected
    assert [folder.path for folder in access.folder_list()] == [str(root) for root in expected]
    spin_until(lambda: application._library_scan_future is None, 10, what="the rescan")


def test_choosing_the_first_folder_adds_it_to_what_another_program_saved(
    shown: Shown,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The first-run choice saved its folder as the whole list. It is an addition:
    folders saved by something else meanwhile are not replaced by it."""
    opened = shown.opened
    application = opened.application
    outside, chosen = tmp_path / "outside", tmp_path / "chosen"
    for folder in (outside, chosen):
        folder.mkdir()
    _another_program_saves(roots=(shown.walls, shown.walls2, outside))

    application._save_initial_library_root(chosen)
    opened.wait("the choice to settle")

    # Failed on 7bdc1a6: (chosen,), every other folder gone.
    assert config.load_strict().roots == (shown.walls, shown.walls2, outside, chosen)
    spin_until(lambda: application._library_scan_future is None, 10, what="the rescan")


def test_a_folder_gesture_is_judged_again_when_the_file_moves_on_while_it_is_judged(
    shown: Shown,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another program saves while the gesture's dry run is running, after the
    folders were read for it. What was judged is then not what is saved, so it is
    not written: the gesture is applied to the newly saved folders and judged
    again. Both the other program's folder and the gesture's are saved."""
    opened = shown.opened
    application = opened.application
    outside, chosen = tmp_path / "outside", tmp_path / "chosen"
    for folder in (outside, chosen):
        folder.mkdir()
    sources_check = application._sources_check
    judged: list[tuple[Path, ...]] = []

    def overtaken_once() -> object:
        check = sources_check()

        def judge(before: config.Settings, after: config.Settings) -> None:
            check(before, after)
            judged.append(after.roots)
            if len(judged) == 1:
                _another_program_saves(roots=(*before.roots, outside))

        return judge

    monkeypatch.setattr(application, "_sources_check", overtaken_once)
    access = opened.page.access
    assert access is not None
    access.add_folder(str(chosen))
    opened.wait("the gesture to settle")

    walls, walls2 = shown.walls, shown.walls2
    # Failed on 7bdc1a6: (walls, walls2, chosen), written over the other program's save.
    assert config.load_strict().roots == (walls, walls2, outside, chosen)
    assert judged == [(walls, walls2, chosen), (walls, walls2, outside, chosen)]
    spin_until(lambda: application._library_scan_future is None, 10, what="the rescan")


def test_a_folder_gesture_that_gives_up_says_so_once_and_names_no_class(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each caller puts "... was not saved; nothing changed: " before the reason,
    so the reason is only the reason, and it reaches a person as that sentence."""
    from wall_in_one.ui import app as application_module

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    attempts: list[object] = []

    def moved_on(changes: object, path: object = None, **_judged: object) -> config.Settings:
        attempts.append(changes)
        raise config.SettingsMovedOnError("settings.toml changed since it was judged")

    monkeypatch.setattr(config, "update", moved_on)
    with pytest.raises(config.SettingsBusyError) as gave_up:
        application_module._save_checked_sources(
            {}, (config.RootsEdit("add", tmp_path),), lambda _before, _after: None
        )

    reply = server.failed(gave_up.value)
    assert len(attempts) == application_module.SOURCE_SAVE_ATTEMPTS
    assert reply.kind == "busy"
    # On afa71e5: "ConfigError: settings.toml kept changing ...; nothing was changed".
    assert reply.message == "settings.toml kept changing while this change was being checked"


# -- AC-2: what names a playlist ------------------------------------------------------------


def _delete(shown: Shown, playlist: playlists.Playlist, via: str) -> Response | None:  # noqa: F811
    """Delete ``playlist`` as the new window, the classic one or ``ctl`` does."""
    opened = shown.opened
    application = opened.application
    reply: list[Response] = []
    if via == "new":
        editing = opened.window.state.playlist_editing
        assert isinstance(editing, RealPlaylists)
        editing.delete_playlist(playlist.id)
    elif via == "classic":
        application.delete_playlist_async(playlist.id, lambda _result, _failures: None)
    else:
        server.dispatch(
            server.build_verb_table(_Commands(application)),
            Request("playlist-delete", playlist.name).encode(),
            reply.append,
        )
    opened.wait("the deletion to settle", lambda: via != "ctl" or bool(reply))
    return reply[0] if reply else None


@pytest.mark.parametrize("via", ["new", "classic", "ctl"])
@pytest.mark.parametrize("saved_as", ["Evening", "evening"])
def test_deleting_a_playlist_clears_a_default_another_program_saved_by_name(
    shown: Shown,  # noqa: F811
    saved_as: str,
    via: str,
) -> None:
    """The window has no default. Another program then makes Evening the default,
    by its name (in either letter case: the compiler resolves both). Deleting
    Evening has to see the default that is saved, not the one the window
    remembers: it is cleared with the playlist, and the configuration still
    builds."""
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.dawn])
    _another_program_saves(active_playlist=saved_as)
    assert application.settings.active_playlist == "", "the window knows nothing of it"
    assert _builds_from_what_is_saved(shown), "the default it saved is valid"

    reply = _delete(shown, evening, via)

    assert reply is None or reply.ok, reply
    assert playlists.Store.open().get(evening.id) is None
    # Failed on 7bdc1a6: still "Evening", naming a playlist that is gone, and
    # the runtime configuration could no longer be built.
    assert config.load_strict().active_playlist == ""
    assert _builds_from_what_is_saved(shown)


def test_deleting_a_playlist_takes_the_rules_and_assignments_another_program_saved_by_name(
    shown: Shown,  # noqa: F811
) -> None:
    """The same for a schedule rule and a display's own playlist, each saved by
    another program under the playlist's name in another letter case."""
    opened = shown.opened
    evening = _playlist(shown, "Evening", [shown.dawn])
    _another_program_saves(rules=("evening",), assignments={"DP-1": "EVENING"})

    _delete(shown, evening, "new")

    # Failed on 7bdc1a6: both stayed, naming a playlist that is gone.
    assert schedules.Store.open().rules == ()
    assert displays.Store.open().all() == ()
    assert opened.application.session.schedules.rules == ()
    assert _builds_from_what_is_saved(shown)


@pytest.mark.parametrize("via", ["new", "ctl"])
def test_a_default_saved_by_name_in_read_only_settings_refuses_the_deletion(
    shown: Shown,  # noqa: F811
    via: str,
) -> None:
    """Another program saves the default by name and a key this version doesn't
    know, which makes the settings read-only. The default could not be cleared,
    so the whole deletion is refused, as it is for a default the window knew."""
    opened = shown.opened
    evening = _playlist(shown, "Evening", [shown.dawn])
    _another_program_saves(active_playlist="evening")
    settings = paths.settings_path()
    settings.write_bytes(settings.read_bytes() + b"future_setting = true\n")
    saved = settings.read_bytes()

    reply = _delete(shown, evening, via)

    # Failed on 7bdc1a6: deleted, leaving a default nothing could clear.
    assert playlists.Store.open().get(evening.id) is not None
    assert settings.read_bytes() == saved
    said = reply.message if reply is not None else opened.reports[-1]
    assert not (reply is not None and reply.ok)
    assert "doesn't recognize" in said or "does not recognize" in said, said


@pytest.mark.parametrize("window", ["new", "classic"])
def test_renaming_a_playlist_gives_the_id_to_what_another_program_saved_by_name(
    shown: Shown,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    """Renaming has the same question: what names this playlist by its name? The
    default, a rule and an assignment another program saved by name are given
    the id, so they name the same playlist after the rename."""
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.dawn])
    _another_program_saves(
        active_playlist="evening", rules=("Evening",), assignments={"DP-1": "EVENING"}
    )

    if window == "new":
        editing = opened.window.state.playlist_editing
        assert isinstance(editing, RealPlaylists)
        editing.rename_playlist(evening.id, "Night")
    else:
        monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
        classic = playlists_page.PlaylistsPage(application)
        classic._selected = evening.id
        classic.refresh(application.session)
        classic._rename("Night")
    opened.wait(
        "the rename", lambda: application.session.playlists.find(evening.id).name == "Night"
    )

    # Failed on 7bdc1a6: all three still said "evening", which named nothing.
    assert config.load_strict().active_playlist == evening.id
    assert [rule.playlist for rule in schedules.Store.open().rules] == [evening.id]
    assert displays.Store.open().playlist_for("DP-1") == evening.id
    assert application.settings.active_playlist == evening.id
    assert _builds_from_what_is_saved(shown)


_RELEASED_SAVES_A_DEFAULT = r"""
from dataclasses import replace

from wall_in_one import config

config.save(replace(config.load(), active_playlist=sys.argv[1]))
print(config.settings_path() if hasattr(config, "settings_path") else "saved", flush=True)
"""


def test_deleting_a_playlist_clears_a_by_name_default_the_released_version_saved(
    shown: Shown,  # noqa: F811
    released_source: Path,  # noqa: F811
) -> None:
    """Rule 4. The other program is 0.2.2 itself, saving ``settings.toml`` with a
    default by name, as its own windows and an older profile do. This checkout
    reads what 0.2.2 wrote, and deleting the playlist clears that default."""
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.dawn])
    done = released.run(released_source, _RELEASED_SAVES_A_DEFAULT, "Evening")
    assert done.returncode == 0, done.stderr
    assert config.load_strict().active_playlist == "Evening", "0.2.2 wrote it by name"
    assert config.load_strict().roots == application.settings.roots, "and kept the rest"

    _delete(shown, evening, "new")

    assert playlists.Store.open().get(evening.id) is None
    # Failed on 7bdc1a6: 0.2.2's default stayed, naming a playlist that is gone.
    assert config.load_strict().active_playlist == ""
    assert _builds_from_what_is_saved(shown)
    assert released.VERSION == "0.2.2"
