"""Astra's 0.3.0 review after the full review, Y-1, in the real application.

While a committed removal's own scan has failed and the fallback scan is
still running, the application knows its library is not an inventory: it
still names the removed file, on purpose. Yet "Duplicate" on All wallpapers,
and ``ctl playlist-add`` into an unused empty playlist, took that file from
it and saved a playlist naming it. Once the fallback scan landed, the
compiler refused that playlist and every later publication stopped until it
was deleted.

Codex's ordering, through the real application's authoring lane, removal
worker and scans (only the scans' timing and failure are injected, and the
runtime transport): the removal commits, its own scan fails, the fallback is
held; each admission is refused, saying to wait for the scan, and nothing is
saved; after the release the inventory is current, publication works, and a
later settings change reaches the runtime document. Each admission test
fails on fb78a74.

The audit's other paths judged by a library the application knows is stale
have a test each, also failing on fb78a74: Undo of an earlier removal and
Quick choice over ``ctl`` (the removed file again), and, while a folder
change's scan is held (the second stale state: the library belongs to the
old folders), removing a playlist's last entry that will still be there and
a video's still going back to none.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_dry_run import _file  # noqa: E402
from tests.test_ui_runtime import FakeWindow, _application, _attach, _close  # noqa: E402
from tests.whole_document import not_in_the_document  # noqa: E402
from wall_in_one import config, paths  # noqa: E402
from wall_in_one.control import client, server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import playlists, scan  # noqa: E402
from wall_in_one.session import RemovalResult  # noqa: E402
from wall_in_one.ui.app import Application, _Commands  # noqa: E402
from wall_in_one.ui.next.prefs import UiPrefsKeeper  # noqa: E402
from wall_in_one.ui.next.real_playlists import RealPlaylists  # noqa: E402
from wall_in_one.ui.next.real_state import RealAppState  # noqa: E402

#: How long anything here may take before it counts as stuck.
BOUND = 15.0
#: `runtime_config.LibraryNotCurrentError.kind`, spelled out so this imports on fb78a74.
NOT_CURRENT = "library-not-current"
WAIT = "wait for that scan to finish"


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@dataclass
class Scans:
    """Injected scan timing and failure; nothing else.

    After ``removed`` is gone: the removal's own scan fails, and the fallback
    refresh is held until released. While ``hold_refresh`` is set: the next
    library refresh (a folder change's) is held until released. A dry run is
    never held.
    """

    removed: Path
    hold_refresh: bool = False
    reached: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    postscan_used: bool = False
    held_used: bool = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = scan.scan

        def faulty(*arguments: Any, **keywords: Any) -> Any:
            refresh = threading.current_thread().name.startswith("library-scan")
            if refresh and not self.held_used and (self.hold_refresh or not self.removed.exists()):
                self.held_used = True
                self.reached.set()
                assert self.release.wait(BOUND * 2), "the test never released the scan"
            elif not refresh and not self.removed.exists() and not self.postscan_used:
                # The first scan off the refresh worker once it is gone: the removal's
                # own, after its commit (its dry run ran while the file was there).
                self.postscan_used = True
                raise OSError("injected: the removal's own scan failed")
            return real(*arguments, **keywords)

        monkeypatch.setattr(scan, "scan", faulty)


@dataclass
class Profile:
    application: Application
    window: FakeWindow
    root: Path  # dawn.png, noon.png, rain.mp4 (no still of its own)
    other: Path  # dusk.png
    scans: Scans

    @property
    def dawn(self) -> Path:
        return self.root / "dawn.png"

    @property
    def noon(self) -> Path:
        return self.root / "noon.png"

    @property
    def rain(self) -> Path:
        return self.root / "rain.mp4"

    @property
    def dusk(self) -> Path:
        return self.other / "dusk.png"


def _settled(application: Application) -> bool:
    return (
        not application._authoring_active
        and not application._authoring_queue
        and not application._settings_authoring_running
        and not application._runtime_compile_pending
        and application._library_scan_future is None
        and not application._runtime_action_pending
    )


def _lane_idle(application: Application) -> bool:
    return not application._authoring_active and not application._authoring_queue


@pytest.fixture
def profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Profile]]:
    made: list[Profile] = []

    def make(*, both_folders: bool = False) -> Profile:
        """``root`` (and ``other``, if asked) as library folders, a valid baseline published."""
        root, other = tmp_path / "lib", tmp_path / "other"
        _file(root / "dawn.png")
        _file(root / "noon.png")
        _file(root / "rain.mp4", b"a video no still was captured from")
        _file(other / "dusk.png")
        application = _application(tmp_path, monkeypatch)
        window = FakeWindow()
        _attach(application, window)
        monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
        monkeypatch.setattr(application, "_make_missing_stills", lambda: None)
        roots = (root, other) if both_folders else (root,)
        settings = replace(application.settings, roots=roots).validated()
        config.save(settings)
        application._settings = settings
        application._settings_requested = settings
        session = application.session
        session.update_settings(settings, rescan_library=False)
        session.refresh(mutate_removals=False)
        application._accepted_library_sources = (settings.roots, settings.scan_workshop)
        profile = Profile(application, window, root, other, Scans(root / "dawn.png"))
        _publish(profile)
        profile.scans.install(monkeypatch)
        made.append(profile)
        return profile

    yield make
    for profile in made:
        profile.scans.release.set()
        _close(profile.application)


def _publish(profile: Profile) -> None:
    application = profile.application
    assert application._publish_runtime_async()
    spin_until(lambda: _settled(application), BOUND, what="a publication")
    assert application.publication_problem == "" and application.left_out_notice == ""


def _remove_dawn(profile: Profile) -> None:
    """Trash dawn: it commits, its own scan fails, the fallback refresh is held."""
    application = profile.application
    item = application.session.library.find(profile.dawn)
    assert item is not None
    removed: list[RemovalResult] = []
    assert application.remove_item_async(item, trash=True, finish=removed.append)
    spin_until(profile.scans.reached.is_set, BOUND, what="the fallback refresh to start")
    spin_until(lambda: bool(removed) and _lane_idle(application), BOUND, what="the removal")
    assert removed[0].committed and not profile.dawn.exists()
    assert not application._inventory_valid
    assert application.session.library.find(profile.dawn) is not None, "stale, on purpose"


def _drop_other_folder(profile: Profile) -> None:
    """Take ``other`` out of the library folders; the scan for the change is held."""
    application = profile.application
    profile.scans.hold_refresh = True
    errors: list[str] = []
    assert application.update_settings_async(roots=(profile.root,), on_error=errors.append)
    spin_until(profile.scans.reached.is_set, BOUND, what="the folder change's scan to start")
    spin_until(lambda: _lane_idle(application), BOUND, what="the folder change")
    assert errors == [] and application.settings.roots == (profile.root,)
    assert application.session.library.find(profile.dusk) is not None, "stale, on purpose"


def _release(profile: Profile) -> None:
    """The held scan lands: the real inventory, and publication works again."""
    application = profile.application
    profile.scans.release.set()
    spin_until(
        lambda: _settled(application) and application.library_currency_refusal() == "",
        BOUND,
        what="the held scan to land",
    )
    _publishes(profile)


def _publishes(profile: Profile) -> None:
    """Nothing is wrong with what is saved: the document builds whole (nothing left out, no
    playlist dropped; `tests.whole_document`), and a later change reaches it."""
    application = profile.application
    assert not_in_the_document(application.settings, application.session) == []
    wanted = not application.settings.shuffle
    errors: list[str] = []
    assert application.update_settings_async(shuffle=wanted, on_error=errors.append)
    spin_until(
        lambda: _settled(application) and application.settings.shuffle == wanted,
        BOUND,
        what="a later settings change",
    )
    assert errors == [] and application.publication_problem == ""
    assert application.left_out_notice == ""
    document = paths.runtime_config_path().read_text(encoding="utf-8")
    assert f"shuffle = {str(wanted).lower()}" in document


def _ctl(application: Application, verb: str, argument: str) -> Response:
    replies: list[Response] = []
    server.dispatch(
        server.build_verb_table(_Commands(application)),
        Request(verb, argument).encode(),
        replies.append,
    )
    spin_until(lambda: bool(replies), BOUND, what=f"ctl {verb}")
    return replies[0]


def _refused(reply: Response) -> None:
    assert not reply.ok and reply.kind == NOT_CURRENT, reply
    assert WAIT in reply.message, reply.message


class Editing:
    """The new window's Playlists part over the real application, and its toasts."""

    def __init__(self, application: Application) -> None:
        self.keeper = UiPrefsKeeper(application.window_report)
        self.state = RealAppState(application, self.keeper)
        part = self.state.playlist_editing
        assert isinstance(part, RealPlaylists)
        self.part = part
        self.toasts: list[tuple[str, Any]] = []
        self.state.connect("toast", lambda _s, text, button: self.toasts.append((text, button)))

    def close(self) -> None:
        self.state.close()
        self.keeper.close(wait=True)


@pytest.fixture
def editing() -> Iterator[Callable[[Application], Editing]]:
    made: list[Editing] = []

    def make(application: Application) -> Editing:
        one = Editing(application)
        made.append(one)
        return one

    yield make
    for one in made:
        one.close()


def _said(profile: Profile, words: str) -> bool:
    return any(words in report for report in profile.window.reports)


def _names() -> list[str]:
    return [playlist.name for playlist in playlists.Store.open().all()]


def _entries(application: Application, pid: str) -> list[str]:
    return [entry.id for entry in application.session.playlists.find(pid).entries]


# -- Codex's two admissions, during the removal's scans --------------------------------------


def test_y1_duplicate_all_wallpapers_waits_for_the_scan(
    profiles: Callable[..., Profile], editing: Callable[[Application], Editing]
) -> None:
    profile = profiles()
    application = profile.application
    part = editing(application).part
    _remove_dawn(profile)

    part.duplicate_playlist("all-media")
    spin_until(lambda: _lane_idle(application), BOUND, what="the copy's task")
    assert application.session.playlists.all() == ()
    assert _names() == [], "no copy naming the removed file was saved"
    assert _said(profile, WAIT)

    # The control: making a playlist with it was refused already.
    part.create_playlist("Mine", [str(profile.dawn)])
    spin_until(lambda: _lane_idle(application), BOUND, what="the create's task")
    assert _names() == []

    _release(profile)
    part.duplicate_playlist("all-media")  # current again: copied as it is now
    spin_until(lambda: _settled(application), BOUND, what="the later copy")
    copy = application.session.playlists.by_name("All wallpapers (copy)")
    assert copy is not None
    assert {entry.path for entry in copy.entries} == {profile.noon, profile.rain}
    _publishes(profile)


def test_y1_ctl_playlist_add_waits_for_the_scan(profiles: Callable[..., Profile]) -> None:
    profile = profiles()
    application = profile.application
    empty = application.session.playlists.create("Empty")
    _remove_dawn(profile)

    _refused(_ctl(application, "playlist-add", f"{empty.id} {profile.dawn}"))
    assert _entries(application, empty.id) == []
    assert playlists.Store.open().find(empty.id).entries == ()

    _release(profile)
    assert _ctl(application, "playlist-add", f"{empty.id} {profile.noon}").ok
    spin_until(lambda: _settled(application), BOUND, what="the later addition")
    _publishes(profile)


# -- the audit's other paths: the removed file again ------------------------------------------


def test_y1_undo_of_an_earlier_removal_waits_for_the_scan(
    profiles: Callable[..., Profile], editing: Callable[[Application], Editing]
) -> None:
    profile = profiles()
    application = profile.application
    one = editing(application)
    lone = application.session.playlists.create(
        "Lone", entries=[playlists.Entry("dawn", str(profile.dawn))]
    )
    one.part.remove_entries(lone.id, ["dawn"])  # empty, and unused: allowed
    spin_until(lambda: _settled(application) and bool(one.toasts), BOUND, what="the removal")
    _text, (_label, undo) = one.toasts[-1]
    _remove_dawn(profile)

    undo()
    spin_until(lambda: _lane_idle(application), BOUND, what="Undo's task")
    assert _entries(application, lone.id) == [], "the removed file did not come back"
    assert _said(profile, WAIT)
    _release(profile)


def test_y1_quick_choice_over_ctl_waits_for_the_scan(profiles: Callable[..., Profile]) -> None:
    profile = profiles()
    application = profile.application
    _remove_dawn(profile)
    _refused(_ctl(application, "select", str(profile.dawn)))
    assert _names() == [], "no Your pick naming the removed file"
    _release(profile)


# -- the audit's other paths: a library of the old folders -------------------------------------


def test_y1_removing_entries_waits_for_a_folder_changes_scan(
    profiles: Callable[..., Profile], editing: Callable[[Application], Editing]
) -> None:
    """Judged by the old folders' library, taking noon out of [dusk, noon] looked fine;
    once the scan landed, the playlist held only dusk, which is gone."""
    profile = profiles(both_folders=True)
    application = profile.application
    part = editing(application).part
    pair = application.session.playlists.create(
        "Pair",
        entries=[
            playlists.Entry("dusk", str(profile.dusk)),
            playlists.Entry("noon", str(profile.noon)),
        ],
    )
    _drop_other_folder(profile)  # the dry run allows it: noon stays

    part.remove_entries(pair.id, ["noon"])
    spin_until(lambda: _lane_idle(application), BOUND, what="the removal's task")
    assert _entries(application, pair.id) == ["dusk", "noon"]
    assert _said(profile, WAIT)
    _refused(_ctl(application, "playlist-remove", f"{pair.id} noon"))
    assert _entries(application, pair.id) == ["dusk", "noon"]

    _release(profile)


def test_y1_a_videos_still_going_back_to_none_waits_for_a_folder_changes_scan(
    profiles: Callable[..., Profile],
) -> None:
    """[dusk, rain], rain shown through noon: judged by the old folders' library, rain losing
    its still looked fine (dusk was there); once the scan landed, nothing in it compiled."""
    profile = profiles(both_folders=True)
    application = profile.application
    session = application.session
    rain = session.library.find(profile.rain)
    assert rain is not None
    session.pairings.choose_still(rain, profile.noon)
    both = session.playlists.create(
        "Both",
        entries=[
            playlists.Entry("dusk", str(profile.dusk)),
            playlists.Entry("rain", str(profile.rain)),
        ],
    )
    _publish(profile)
    _drop_other_folder(profile)  # the dry run allows it: rain still compiles, through noon

    reply = _ctl(application, "still", f"{profile.rain} :: default")
    assert not reply.ok and reply.kind == NOT_CURRENT, reply
    assert WAIT in reply.message
    assert session.pairings.resolve_accepted(rain, session.library).still == profile.noon
    assert _entries(application, both.id) == ["dusk", "rain"]
    _release(profile)
