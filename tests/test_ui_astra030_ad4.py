"""Astra's seventh full review of 0.3.0, AD-4: an empty playlist under a left-out default's name.

``settings.toml`` is kept read-only and names a default, "Dusk", that no
playlist answers to: it is left out and "All media" plays. Somebody then
makes an **empty** playlist called "Dusk". The saved name now resolves, to a
playlist with nothing to play, and until r6 that stopped every later
publication ("default playlist refers to playlist 'Dusk', which has no
playable entries"), with the settings read-only so that nothing in the app
could put it right, and the playlist, being the default, not deletable
either.

The review asked for a guard on that creation. The owner changed the rule
instead: the build never refuses over a playlist that can't play. So this
is now a test and not a guard. The empty playlist is made, the default is
still left out, "All media" still plays, the notice says the new reason
("has nothing to play" where it said "doesn't exist"), and giving the
playlist a wallpaper makes it the default the settings always named.

End to end through the new window, the classic one and ``ctl``, with the
gesture's own publication, on settings kept read-only by real permission
bits. Each case fails on a5999e2.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_astra030_ac_saved import _verb  # noqa: E402
from tests.test_ui_codex030_round1c import (  # noqa: E402, F401
    Shown,
    shown,
    toolkit,
)
from tests.test_ui_left_out import (  # noqa: E402
    ALL_MEDIA,
    DEFAULT_HAS_NOTHING,
    DEFAULT_NAMES_NOTHING,
    _document,
    _publish,
)
from tests.test_ui_playlists_polish import QuietLoader  # noqa: E402
from wall_in_one import config, paths  # noqa: E402
from wall_in_one.library import playlists  # noqa: E402
from wall_in_one.ui import playlists_page  # noqa: E402
from wall_in_one.ui.next.real_playlists import RealPlaylists  # noqa: E402


def _without_pointer(text: str) -> str:
    """``text`` less the pointer a default in a file that can't be changed ends with.

    "The settings file can't be changed: <why>. To change the default, edit
    active_playlist in <path>." The pointer is asserted, then removed.
    """
    sentence, found, pointer = text.partition(" The settings file can\u2019t be changed: ")
    assert found, text
    assert pointer.endswith(
        f"To change the default, edit active_playlist in {paths.settings_path()}."
    )
    return sentence


@contextlib.contextmanager
def _settings_can_not_be_saved() -> Iterator[None]:
    """The folder ``settings.toml`` is in, read-only for as long as this lasts."""
    folder = paths.settings_path().parent
    folder.chmod(0o500)
    try:
        yield
    finally:
        folder.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to a read-only folder")
@pytest.mark.parametrize("via", ["the new window", "the classic window", "ctl"])
def test_ad4_an_empty_playlist_under_a_left_out_defaults_name_stops_nothing(
    shown: Shown,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    via: str,
) -> None:
    opened = shown.opened
    application = opened.application
    session = application.session
    # The gesture's own publication, as the running application makes it.
    monkeypatch.delattr(application, "_publish_runtime_for_context")
    config.update({"active_playlist": "Dusk"})  # a setup that names a playlist not made yet
    application._settings = config.load_strict()
    session.update_settings(application._settings, rescan_library=False)
    saved = paths.settings_path().read_bytes()

    def quiet() -> bool:
        return (
            not application._runtime_compile_pending and not application._runtime_publication_held
        )

    with _settings_can_not_be_saved():
        partial = application._repair_dangling_playlist_references()
        assert application._finish_playlist_reference_repair(partial).ok
        assert _publish(shown)["default_playlist"] == ALL_MEDIA
        assert [_without_pointer(text) for text in opened.reports] == [
            DEFAULT_NAMES_NOTHING.format("Dusk")
        ]

        if via == "the new window":
            editing = opened.window.state.playlist_editing
            assert isinstance(editing, RealPlaylists)
            assert editing.create_playlist("Dusk")
        elif via == "the classic window":
            monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
            classic = playlists_page.PlaylistsPage(application)
            classic.refresh(session)
            classic._new_name.set_text("Dusk")
            classic._create()
        else:
            reply = _verb(shown, "playlist-new", "Dusk")
            assert reply.ok, reply
        opened.wait(
            "the empty playlist and its publication",
            lambda: session.playlists.by_name("Dusk") is not None and quiet(),
        )
        dusk = playlists.Store.open().by_name("Dusk")
        assert dusk is not None and dusk.entries == (), "made, and empty"

        # Failed on a5999e2: "default playlist refers to playlist 'Dusk', which
        # has no playable entries", and every later publication with it.
        assert application.publication_problem == ""
        document = _document()
        assert document["default_playlist"] == ALL_MEDIA
        assert [playlist["name"] for playlist in document["playlists"]] == ["All media"]
        assert _without_pointer(application.left_out_notice) == DEFAULT_HAS_NOTHING.format("Dusk")
        assert [_without_pointer(text) for text in opened.reports] == [
            DEFAULT_NAMES_NOTHING.format("Dusk"),
            DEFAULT_HAS_NOTHING.format("Dusk"),
        ], "said again, because why it is left out changed"

        # A later change still reaches playback: the playlist is given
        # something to play, and is the default the settings always named.
        reply = _verb(shown, "playlist-add", f"{dusk.id} {shown.dawn.path}")
        assert reply.ok, reply
        opened.wait("the wallpaper and its publication", quiet)
        document = _document()
        assert document["default_playlist"] == dusk.id
        assert application.left_out_notice == "" and application.publication_problem == ""
        assert len(opened.reports) == 2, "good news is not a toast"
    assert paths.settings_path().read_bytes() == saved, "settings.toml was never touched"
